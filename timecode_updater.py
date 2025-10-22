from flask import Flask, request, jsonify, render_template_string
from shotgun_api3 import Shotgun
import os
import re

app = Flask(__name__)

# --- CONFIG ---
SG_URL = os.environ.get("SG_URL")
SG_SCRIPT_NAME = os.environ.get("SG_SCRIPT_NAME")
SG_SCRIPT_KEY = os.environ.get("SG_SCRIPT_KEY")

sg = Shotgun(SG_URL, script_name=SG_SCRIPT_NAME, api_key=SG_SCRIPT_KEY)

# --- Simple upload page ---
UPLOAD_FORM = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>EDL Timecode Uploader</title>
  <style>
    body { font-family: system-ui, sans-serif; padding: 40px; background: #f5f5f5; }
    form { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 0 10px rgba(0,0,0,0.1); width: 400px; margin: auto; }
    input[type=file] { margin-bottom: 10px; }
    button { background: #0078d4; color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; }
    button:hover { background: #005ea1; }
    .msg { margin-top: 20px; text-align: center; }
  </style>
</head>
<body>
  <form action="/update_timecode" method="post" enctype="multipart/form-data">
    <h2>Upload EDL to Update Timecodes</h2>
    <input type="file" name="edl" accept=".edl" required><br>
    <button type="submit">Upload</button>
  </form>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(UPLOAD_FORM)

# --- Main update endpoint ---
@app.route('/update_timecode', methods=['POST'])
def upload_edl():
    edl_file = request.files.get('edl')
    if not edl_file:
        return jsonify({"error": "No EDL uploaded"}), 400

    edl_text = edl_file.read().decode('utf-8', errors='ignore')

    # Pattern matches locator line: * LOC: 01:00:06:06 GREEN BOB_200_999_490
    locator_pattern = re.compile(
        r"\* LOC:\s+(?P<tc>\d{2}:\d{2}:\d{2}:\d{2})\s+\S+\s+(?P<code>[A-Z]{3}_[0-9]{3}_[A-Z0-9]{3}_[0-9]{3})"
    )

    updated = 0
    skipped = 0
    errors = []

    for match in locator_pattern.finditer(edl_text):
        shot_code = match.group("code").strip()
        timecode = match.group("tc").strip()

        try:
            shot = sg.find_one("Shot", [["code", "is", shot_code]], ["id"])
            if shot:
                sg.update("Shot", shot["id"], {"sg_timecode": timecode})
                updated += 1
            else:
                skipped += 1
        except Exception as e:
            errors.append(f"{shot_code}: {e}")
            skipped += 1

    result = {
        "message": f"✅ Updated {updated} shots in ShotGrid. Skipped {skipped}.",
        "updated": updated,
        "skipped": skipped,
        "errors": errors
    }

    # If uploaded from the HTML form, return a simple web page instead of JSON
    if "text/html" in request.accept_mimetypes:
        html = f"""
        <html><body style='font-family:sans-serif; padding:40px;'>
          <h2>EDL Timecode Update Results</h2>
          <p><b>{result["message"]}</b></p>
          <p>Errors: {len(errors)}</p>
          <ul>{''.join(f'<li>{e}</li>' for e in errors)}</ul>
          <a href="/">← Upload another EDL</a>
        </body></html>
        """
        return html

    return jsonify(result)
    

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
