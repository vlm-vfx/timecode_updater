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

        edl_text = edl_file.read().decode('utf-8')

        # EDL pattern: match events and capture record in/out, plus *LOC lines
        pattern = re.compile(
            r"(?P<event>\d+)\s+(?P<reel>\S+)\s+\S+\s+(?P<src_in>\S+)\s+(?P<src_out>\S+)\s+(?P<rec_in>\S+)\s+(?P<rec_out>\S+).*?"
            r"(?:\n\*.*?LOC:\s+(?P<loc_tc>\S+)\s+\S+\s+(?P<locator>.*))?",
            re.MULTILINE
        )

        updated = 0
        skipped = 0
        errors = 0

        for match in pattern.finditer(edl_text):
            locator = match.group("locator")
            rec_in = match.group("rec_in")  # the cut in timecode (timeline start)

            if not locator or not re.match(r"[A-Z]{3}_[0-9]{3}_[A-Z0-9]{3}_[0-9]{3}", locator):
                skipped += 1
                continue

            try:
                shot = sg.find_one("Shot", [["code", "is", locator]], ["id"])
                if shot:
                    sg.update("Shot", shot["id"], {"sg_timecode": rec_in})
                    updated += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"Error updating {locator}: {e}")
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
