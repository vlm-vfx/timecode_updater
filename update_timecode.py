from flask import Flask, request, jsonify, render_template_string
import os
import re
import requests
from base64 import b64encode
from shotgun_api3 import Shotgun

app = Flask(__name__)

# --- CONFIG (from environment) ---
SG_URL = os.environ.get("SG_URL")
SG_SCRIPT_NAME = os.environ.get("SG_SCRIPT_NAME")
SG_SCRIPT_KEY = os.environ.get("SG_SCRIPT_KEY")

FMP_SERVER = os.environ.get("FMP_SERVER")
FMP_DB = os.environ.get("FMP_DB")
FMP_USERNAME = os.environ.get("FMP_USERNAME")
FMP_PASSWORD = os.environ.get("FMP_PASSWORD")

# --- CONNECT TO SHOTGRID ---
sg = Shotgun(SG_URL, script_name=SG_SCRIPT_NAME, api_key=SG_SCRIPT_KEY)


# -------------------------------------------------------
# FILEMAKER HELPERS
# -------------------------------------------------------
def fmp_login():
    """Authenticate to FileMaker Data API and return session token"""
    url = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/sessions"
    auth_string = f"{FMP_USERNAME}:{FMP_PASSWORD}"
    auth_base64 = b64encode(auth_string.encode("utf-8")).decode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth_base64}",
    }
    r = requests.post(url, headers=headers)
    if r.status_code == 200:
        return r.json()["response"]["token"]
    raise Exception(f"❌ FMP Login failed: {r.text}")


def fmp_update_timecode_and_cut(token, shot_code, timecode):
    """Find and update a record in FileMaker where Shot Code matches"""
    try:
        # --- Find matching record ---
        url_find = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/layouts/status_update/_find"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        query = {"query": [{"Shot Code": str(shot_code)}]}
        find_response = requests.post(url_find, headers=headers, json=query)

        if find_response.status_code != 200:
            return {"success": False, "error": find_response.text}

        data = find_response.json().get("response", {}).get("data", [])
        if not data:
            return {"success": False, "error": f"No FMP record found for Shot Code={shot_code}"}

        record_id = data[0]["recordId"]

        # --- Update Timecode and Cut fields ---
        url_update = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/layouts/status_update/records/{record_id}"
        update_data = {"fieldData": {"Timecode": timecode}, {"Cut Version": cut_version}}
        update_response = requests.patch(url_update, headers=headers, json=update_data)

        if update_response.status_code == 200:
            return {"success": True}
        else:
            return {"success": False, "error": update_response.text}

    except Exception as e:
        return {"success": False, "error": str(e)}


# -------------------------------------------------------
# MAIN EDL → SG → FMP UPDATE LOGIC
# -------------------------------------------------------
@app.route("/update_timecode", methods=["POST"])
def upload_edl():
    """
    Upload an EDL file:
      1. Parse title, event + LOC lines to extract (Shot Code, record-in timecode, Cut Name)
      2. Update sg_timecode and sg_from_cut in ShotGrid
      3. Update Timecode and Cut Version field in FileMaker where Shot Code matches
    """
    try:
        edl_file = request.files.get("edl")
        if not edl_file:
            return jsonify({"error": "No EDL uploaded"}), 400

        edl_text = edl_file.read().decode("utf-8", errors="ignore")

        # --- Parse TITLE line for cut version ---
        title_match = re.search(r"^TITLE:\s*(.*)$", edl_text, re.MULTILINE | re.IGNORECASE)
        cut_version = title_match.group(1).strip() if title_match else "Unknown"

        lines = edl_text.splitlines()

        # --- Regex patterns ---
        event_line_re = re.compile(r"^\s*(\d{1,})\s+")
        shot_code_re = re.compile(r"([A-Z]{3}_[0-9]{3}_[A-Z0-9]{3}_[0-9]{3})", re.IGNORECASE)
        loc_line_re = re.compile(r"^\s*\*\s*LOC\s*:?", re.IGNORECASE)
        timecode_re = re.compile(r"^\d{2}:\d{2}:\d{2}:\d{2}$")

        # --- Pass 1: collect events (line index + rec_in timecode) ---
        events = []
        for idx, raw in enumerate(lines):
            if event_line_re.match(raw):
                parts = re.split(r"\s+", raw.strip())
                timecodes = [p for p in parts if timecode_re.match(p)]
                if len(timecodes) >= 2:
                    rec_in = timecodes[-2]
                elif len(timecodes) == 1:
                    rec_in = timecodes[0]
                else:
                    rec_in = None
                events.append((idx, rec_in, raw))

        parsed_pairs = []
        parse_errors = []

        # --- Pass 2: find LOC lines for each event ---
        for i, (evt_idx, rec_in, evt_line) in enumerate(events):
            end_idx = events[i + 1][0] if i + 1 < len(events) else len(lines)

            if not rec_in:
                parse_errors.append({
                    "event_index": evt_idx,
                    "reason": "no_rec_in_found",
                    "event_line": evt_line
                })
                continue

            found_loc = False
            for j in range(evt_idx + 1, end_idx):
                loc_raw = lines[j]
                if loc_line_re.search(loc_raw):
                    m = shot_code_re.search(loc_raw)
                    if m:
                        shot_code = m.group(1).strip()
                        parsed_pairs.append({
                            "event_index": evt_idx,
                            "rec_in": rec_in,
                            "shot_code": shot_code
                        })
                    else:
                        parse_errors.append({
                            "event_index": evt_idx,
                            "reason": "loc_found_but_no_shot_code",
                            "line": loc_raw.strip()
                        })
                    found_loc = True
                    break
            if not found_loc:
                parse_errors.append({
                    "event_index": evt_idx,
                    "reason": "no_loc_found_between_events"
                })

        # --- If nothing parsed, return that early (makes debugging easier) ---
        if not parsed_pairs:
            response = {
                "parsed_count": 0,
                "parsed": [],
                "parse_errors": parse_errors,
                "updated_sg": 0,
                "updated_fmp": 0,
                "skipped": 0,
                "sg_errors": [],
                "fmp_errors": [],
                "cut_version": cut_version
            }
            # return HTML if browser, otherwise JSON
            if "text/html" in request.accept_mimetypes:
                html = render_template_string("""
                    <html><body style="font-family:sans-serif;padding:30px;">
                      <h2>No shots parsed</h2>
                      <p>Cut Version: {{cut_version}}</p>
                      <pre>{{response}}</pre>
                      <a href="/">Upload another EDL</a>
                    </body></html>
                """, cut_version=cut_version, response=response)
                return html
            return jsonify(response), 200
        
        # --- Connect to FileMaker ---
        fmp_token = fmp_login()

        updated_sg = 0
        updated_fmp = 0
        skipped = 0
        fmp_errors = []
        sg_errors = []

        # --- Update both SG + FMP ---
        for p in parsed_pairs:
            shot_code = p["shot_code"]
            rec_in = p["rec_in"]

            try:
                # ShotGrid
                shot = sg.find_one("Shot", [["code", "is", shot_code]], ["id"])
                if shot:
                    sg.update("Shot", shot["id"], {"sg_timecode": rec_in, "sg_from_cut": cut_version})
                    updated_sg += 1

                    # FileMaker
                    fmp_result = fmp_update_timecode_and_cut(fmp_token, shot_code, rec_in, cut_version)
                    if fmp_result["success"]:
                        updated_fmp += 1
                    else:
                        fmp_errors.append({
                            "shot_code": shot_code,
                            "error": fmp_result["error"]
                        })
                else:
                    skipped += 1
            except Exception as e:
                sg_errors.append({"shot_code": shot_code, "error": str(e)})

        # --- Summary JSON ---
        result = {
            "cut_version": cut_version,
            "parsed_count": len(parsed_pairs),
            "updated_sg": updated_sg,
            "updated_fmp": updated_fmp,
            "skipped": skipped,
            "parse_errors": parse_errors,
            "sg_errors": sg_errors,
            "fmp_errors": fmp_errors,
            "message": f"✅ Updated {updated_sg} shots in SG and {updated_fmp} in FMP. Skipped {skipped}."
        }
        return jsonify(result), 200

        # HTML summary for browser users
        if "text/html" in request.accept_mimetypes:
            html = render_template_string("""
                <html><body style="font-family:sans-serif;padding:30px;">
                  <h2>✅ EDL Upload Summary</h2>
                  <p><b>Cut Version:</b> {{result.cut_version}}</p>
                  <p><b>Parsed:</b> {{result.parsed_count}} &nbsp; <b>Updated (SG):</b> {{result.updated_sg}} &nbsp; <b>Updated (FMP):</b> {{result.updated_fmp}} &nbsp; <b>Skipped:</b> {{result.skipped}}</p>
                  <h3>Parsed Shots</h3>
                  <ul>
                    {% for p in result.parsed %}
                      <li>{{p.shot_code}} — {{p.rec_in}}</li>
                    {% endfor %}
                  </ul>
                  {% if result.parse_errors %}
                    <h3>Parse Errors</h3>
                    <ul>{% for e in result.parse_errors %}<li>{{e}}</li>{% endfor %}</ul>
                  {% endif %}
                  {% if result.sg_errors %}
                    <h3>ShotGrid Errors</h3>
                    <ul>{% for e in result.sg_errors %}<li>{{e}}</li>{% endfor %}</ul>
                  {% endif %}
                  {% if result.fmp_errors %}
                    <h3>FileMaker Errors</h3>
                    <ul>{% for e in result.fmp_errors %}<li>{{e}}</li>{% endfor %}</ul>
                  {% endif %}
                  <p><a href="/">← Upload another EDL</a></p>
                </body></html>
            """, result=result)
            return html, 200

        # default JSON response
        return jsonify(result), 200
    
    except Exception as e:
        return jsonify({"fatal_error": str(e)}), 500


# -------------------------------------------------------
# SIMPLE UPLOAD FORM (for your VFX editor)
# -------------------------------------------------------
@app.route("/")
def index():
    return '''
    <html>
    <head>
        <title>EDL Timecode Uploader</title>
        <style>
            body { font-family: sans-serif; background: #111; color: #eee;
                   display: flex; flex-direction: column; align-items: center;
                   justify-content: center; height: 100vh; }
            form { background: #222; padding: 2em; border-radius: 12px;
                   box-shadow: 0 0 10px #000; }
            input[type=file], button { margin-top: 1em; width: 100%; }
            button { padding: 0.5em; border: none; border-radius: 8px;
                     background: #4caf50; color: white; font-weight: bold;
                     cursor: pointer; }
            button:hover { background: #43a047; }
        </style>
    </head>
    <body>
        <h2>EDL Timecode Uploader</h2>
        <form method="POST" action="/update_timecode" enctype="multipart/form-data">
            <input type="file" name="edl" accept=".edl" required>
            <button type="submit">Upload and Sync SG + FMP</button>
        </form>
    </body>
    </html>
    '''


# -------------------------------------------------------
# MAIN
# -------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False)
