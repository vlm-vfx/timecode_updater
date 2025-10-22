from flask import Flask, request, jsonify, render_template_string
from shotgun_api3 import Shotgun
import os
import re
import requests

app = Flask(__name__)

# --- CONFIG ---
SG_URL = os.environ.get("SG_URL")
SG_SCRIPT_NAME = os.environ.get("SG_SCRIPT_NAME")
SG_SCRIPT_KEY = os.environ.get("SG_SCRIPT_KEY")
FMP_SYNC_URL = os.environ.get("FMP_SYNC_URL")  # optional
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

sg = Shotgun(SG_URL, script_name=SG_SCRIPT_NAME, api_key=SG_SCRIPT_KEY)

UPLOAD_FORM = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>EDL Timecode Uploader</title>
  <style>
    body { font-family: system-ui, sans-serif; padding: 40px; background: #f5f5f5; }
    form { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 0 10px rgba(0,0,0,0.1); width: 450px; margin: auto; }
    input[type=file] { margin-bottom: 10px; }
    label { display: block; margin-top: 10px; }
    button { background: #0078d4; color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; }
    button:hover { background: #005ea1; }
  </style>
</head>
<body>
  <form action="/update_timecode" method="post" enctype="multipart/form-data">
    <h2>Upload EDL to Update Timecodes</h2>
    <input type="file" name="edl" accept=".edl" required><br>
    <label><input type="checkbox" name="debug"> Debug Mode (simulate only)</label>
    <button type="submit">Upload</button>
  </form>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(UPLOAD_FORM)


@app.route('/update_timecode', methods=['POST'])
def upload_edl():
    edl_file = request.files.get('edl')
    if not edl_file:
        return jsonify({"error": "No EDL uploaded"}), 400

    edl_text = edl_file.read().decode('utf-8', errors='ignore')

    # --- detect debug mode from form or env var ---
    debug = DEBUG_MODE or ("debug" in request.form)
    debug_label = " (debug mode)" if debug else ""

    # Pattern to match an event line followed by its LOC line
    event_pattern = re.compile(
        r"(?P<event>\d+)\s+\S+\s+\S+\s+(?P<src_in>\d{2}:\d{2}:\d{2}:\d{2})\s+\d{2}:\d{2}:\d{2}:\d{2}\s+(?P<rec_in>\d{2}:\d{2}:\d{2}:\d{2})\s+\d{2}:\d{2}:\d{2}:\d{2})"
        r"[\s\S]*?\* LOC:\s+\S+\s+\S+\s+(?P<code>[A-Z]{3}_[0-9]{3}_[A-Z0-9]{3}_[0-9]{3})"
    )

    updated = 0
    skipped = 0
    parsed = []
    errors = []

    for match in event_pattern.finditer(edl_text):
        shot_code = match.group("code").strip()
        rec_in = match.group("rec_in").strip()
        parsed.append((shot_code, rec_in))

        try:
            shot = sg.find_one("Shot", [["code", "is", shot_code]], ["id"])
            if not shot:
                skipped += 1
                continue

            if not debug:
                sg.update("Shot", shot["id"], {"sg_timecode": rec_in})
            updated += 1

        except Exception as e:
            errors.append(f"{shot_code}: {e}")
            skipped += 1

    # Trigger FMP sync (only if not in debug mode)
    sync_status = None
    if FMP_SYNC_URL and not debug:
        try:
            r = requests.post(FMP_SYNC_URL, json={"source": "edl_timecode"})
            if r.status_code == 200:
                sync_status = "✅ FMP sync triggered successfully."
            else:
                sync_status = f"⚠️ FMP sync returned status {r.status_code}."
        except Exception as e:
            sync_status = f"❌ FMP sync failed: {e}"
            errors.append(sync_status)
    elif debug:
        sync_status = "🔍 Debug mode — no FMP sync triggered."

    result = {
        "message": f"✅ Parsed {len(parsed)} shots; updated {updated} (skipped {skipped}) in ShotGrid{debug_label}.",
        "updated": updated,
        "skipped": skipped,
        "debug": debug,
        "fmp_sync": sync_status or "No FMP sync configured.",
        "errors": errors,
        "parsed": parsed
    }

    # If uploaded via browser, render as HTML
    if "text/html" in request.accept_mimetypes:
        rows = "".join(f"<tr><td>{s}</td><td>{t}</td></tr>" for s, t in parsed)
        html = f"""
        <html><body style='font-family:sans-serif; padding:40px;'>
          <h2>EDL Timecode Update Results{debug_label}</h2>
          <p><b>{result["message"]}</b></p>
          <p>{result["fmp_sync"]}</p>
          <table border="1" cellspacing="0" cellpadding="4">
            <tr><th>Shot Code</th><th>Timecode</th></tr>
            {rows}
          </table>
          <p>Errors: {len(errors)}</p>
          <ul>{''.join(f'<li>{e}</li>' for e in errors)}</ul>
          <a href="/">← Upload another EDL</a>
        </body></html>
        """
        return html

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
