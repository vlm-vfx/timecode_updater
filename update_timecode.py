from flask import Flask, request, jsonify
import os
import re
from shotgun_api3 import Shotgun

app = Flask(__name__)

# --- CONFIG ---
SG_URL = os.environ.get("SG_URL")
SG_SCRIPT_NAME = os.environ.get("SG_SCRIPT_NAME")
SG_SCRIPT_KEY = os.environ.get("SG_SCRIPT_KEY")

sg = Shotgun(SG_URL, script_name=SG_SCRIPT_NAME, api_key=SG_SCRIPT_KEY)

@app.route('/update_timecode', methods=['POST'])
def upload_edl():
    try:
        edl_file = request.files.get('edl')
        if not edl_file:
            return jsonify({"error": "No EDL uploaded"}), 400

        edl_text = edl_file.read().decode('utf-8', errors='ignore')
        lines = edl_text.splitlines()

        updated = 0
        skipped = 0
        errors = 0

        rec_in = None

        for line in lines:
            line = line.strip()

            # Match an edit event line
            # e.g. "000002  E003C0006_250624_X01519  V     C        09:54:55:07 09:54:56:08 01:00:01:01 01:00:02:02"
            if re.match(r'^\d{3,}\s+\S+', line):
                parts = line.split()
                if len(parts) >= 8:
                    rec_in = parts[6]  # The record-in timecode (timeline in)
                else:
                    rec_in = None
                continue

            # Match the locator line
            # e.g. "*LOC: 01:00:01:13 GREEN   BOB_200_000_080"
            loc_match = re.match(r'^\*LOC:\s+\S+\s+\S+\s+([A-Z]{3}_[0-9]{3}_[A-Z0-9]{3}_[0-9]{3})', line)
            if loc_match and rec_in:
                shot_code = loc_match.group(1).strip()

                try:
                    shot = sg.find_one("Shot", [["code", "is", shot_code]], ["id"])
                    if shot:
                        sg.update("Shot", shot["id"], {"sg_timecode": rec_in})
                        updated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    print(f"Error updating {shot_code}: {e}")
                    errors += 1

        return jsonify({
            "message": f"✅ Updated {updated} shots in ShotGrid. Skipped {skipped}. Errors {errors}.",
            "updated": updated,
            "skipped": skipped,
            "errors": errors
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Minimal HTML upload form for your VFX editor ---
@app.route('/')
def index():
    return '''
    <html>
        <head>
            <title>EDL Timecode Uploader</title>
            <style>
                body { font-family: sans-serif; background: #111; color: #eee; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; }
                form { background: #222; padding: 2em; border-radius: 12px; box-shadow: 0 0 10px #000; }
                input[type=file], button { margin-top: 1em; width: 100%; }
                button { padding: 0.5em; border: none; border-radius: 8px; background: #4caf50; color: white; font-weight: bold; cursor: pointer; }
                button:hover { background: #43a047; }
            </style>
        </head>
        <body>
            <h2>EDL Timecode Uploader</h2>
            <form method="POST" action="/update_timecode" enctype="multipart/form-data">
                <input type="file" name="edl" accept=".edl" required>
                <button type="submit">Upload and Update Shots</button>
            </form>
        </body>
    </html>
    '''


if __name__ == '__main__':
    app.run(debug=False)
