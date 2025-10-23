from flask import Flask, request, jsonify
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


def fmp_update_timecode(token, shot_code, timecode):
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

        # --- Update Timecode field ---
        url_update = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/layouts/status_update/records/{record_id}"
        update_data = {"fieldData": {"Timecode": timecode}}
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
@app.route('/update_timecode', methods=['POST'])
def upload_edl():
    edl_file = request.files.get('edl')
    if not edl_file:
        return jsonify({"error": "No EDL uploaded"}), 400

    edl_text = edl_file.read().decode('utf-8')

    # --- Parse TITLE line ---
    title_match = re.search(r"^TITLE:\s*(.*)$", edl_text, re.MULTILINE)
    cut_version = title_match.group(1).strip() if title_match else "Unknown"

    lines = edl_text.splitlines()
    parsed = []
    errors = []

    for i, line in enumerate(lines):
        event_match = re.match(
            r"^\s*\d+\s+\S+\s+\S+\s+\S+\s+\S+\s+(?P<rec_in>\d{2}:\d{2}:\d{2}:\d{2})\s+\S+",
            line
        )
        if event_match:
            rec_in = event_match.group("rec_in")

            # find the next * LOC line after this event
            for j in range(i + 1, min(i + 6, len(lines))):
                if lines[j].startswith("* LOC:"):
                    loc_match = re.match(
                        r"\* LOC:\s+\S+\s+\S+\s+(?P<shot_code>[A-Z0-9_]+)",
                        lines[j]
                    )
                    if loc_match:
                        shot_code = loc_match.group("shot_code")
                        parsed.append({
                            "rec_in": rec_in,
                            "shot_code": shot_code
                        })
                    break

    updated, skipped, update_errors = 0, 0, []

    for entry in parsed:
        try:
            shot = sg.find_one("Shot", [["code", "is", entry["shot_code"]]], ["id"])
            if shot:
                sg.update("Shot", shot["id"], {
                    "sg_timecode": entry["rec_in"],
                    "sg_from_cut": cut_version
                })
                # --- Push to FileMaker ---
                try:
                    fmp_url = os.environ.get("FMP_URL")
                    fmp_auth = (os.environ.get("FMP_USER"), os.environ.get("FMP_PASS"))
                    payload = {
                        "layout": "status_update",
                        "query": [{"Shot Code": entry["shot_code"]}],
                        "fieldData": {
                            "Timecode": entry["rec_in"],
                            "Cut Version": cut_version
                        }
                    }
                    r = requests.post(
                        f"{fmp_url}/record/update",
                        auth=fmp_auth,
                        json=payload,
                        timeout=10
                    )
                    if not r.ok:
                        raise Exception(f"FMP update failed ({r.status_code})")
                except Exception as fmp_err:
                    update_errors.append(f"FMP: {fmp_err}")
                updated += 1
            else:
                skipped += 1
        except Exception as e:
            update_errors.append(str(e))

    # --- HTML Summary ---
    html_summary = f"""
    <html>
      <head><title>EDL Update Summary</title></head>
      <body style="font-family:sans-serif; line-height:1.5;">
        <h2>✅ EDL Upload Summary</h2>
        <p><b>Cut Version:</b> {cut_version}</p>
        <p><b>Updated:</b> {updated} shots<br>
           <b>Skipped:</b> {skipped}<br>
           <b>Errors:</b> {len(update_errors)}</p>
        <hr>
        <h3>Parsed Shots</h3>
        <ul>
          {''.join([f"<li>{p['shot_code']} – {p['rec_in']}</li>" for p in parsed])}
        </ul>
        {'<hr><h3>Errors</h3><ul>' + ''.join(f'<li>{e}</li>' for e in update_errors) + '</ul>' if update_errors else ''}
      </body>
    </html>
    """

    return html_summary, 200, {"Content-Type": "text/html"}

# -------------------------------------------------------
# SIMPLE UPLOAD FORM (for your VFX editor)
# -------------------------------------------------------
@app.route("/")
def index():
    return '''
    <html>
    <head>
        <title>Update Cut Data</title>
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
        <h2>EDL Uploader</h2>
        <form method="POST" action="/update_timecode" enctype="multipart/form-data">
            <input type="file" name="edl" accept=".edl" required>
            <button type="submit">Update SG & FMP</button>
        </form>
    </body>
    </html>
    '''


# -------------------------------------------------------
# MAIN
# -------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False)
