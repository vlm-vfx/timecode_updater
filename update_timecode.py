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
    """
    Robust event+LOC parser:
    - Scans for event lines (lines starting with an event number).
    - Grabs the record-in (rec_in) timecode from that event.
    - Looks ahead from that event line until the next event line for a LOC line,
      and extracts the shot code from the LOC line wherever it appears.
    - Returns parsed pairs in JSON for immediate visibility, then attempts SG updates.
    """

    try:
        edl_file = request.files.get('edl')
        if not edl_file:
            return jsonify({"error": "No EDL uploaded"}), 400

        edl_text = edl_file.read().decode('utf-8', errors='ignore')
        lines = edl_text.splitlines()

        # regexes
        # event line: starts with digits, then many tokens; rec_in is usually the 7th token (0-based index 6)
        event_line_re = re.compile(r'^\s*(\d{1,})\s+')
        # shot code pattern anywhere on a LOC line (case-insensitive)
        shot_code_re = re.compile(r'([A-Z]{3}_[0-9]{3}_[A-Z0-9]{3}_[0-9]{3})', re.IGNORECASE)
        # LOC line marker (allow with or without space after the star)
        loc_line_re = re.compile(r'^\s*\*\s*LOC\s*:?', re.IGNORECASE)

        parsed_pairs = []   # list of dicts {rec_in, loc_line, shot_code, event_line_index, loc_line_index}
        parse_errors = []

        # First pass: find event lines indices and their rec_in timecodes
        events = []  # list of tuples (index, rec_in)
        for idx, raw in enumerate(lines):
            line = raw.rstrip("\n")
            if event_line_re.match(line):
                # split preserving that timecode tokens may be separated by multiple spaces
                parts = re.split(r'\s+', line.strip())
                # Defensive: EDL variants may have different token counts; guard index
                # Typical: [event, reel, <maybe multi tokens>, ... , src_in, src_out, rec_in, rec_out]
                # We'll attempt to find first token that matches timecode pattern \d{2}:\d{2}:\d{2}:\d{2}
                timecodes = [p for p in parts if re.match(r'^\d{2}:\d{2}:\d{2}:\d{2}$', p)]
                if len(timecodes) >= 1:
                    # rec_in is typically the third/fourth timecode in the event line; heuristics:
                    # prefer the third-to-last timecode if available, else use the first timecode after the src times.
                    # Simpler and safer: choose the penultimate timecode if >=2 timecodes, otherwise the first.
                    if len(timecodes) >= 2:
                        rec_in = timecodes[-2]  # the second-to-last timecode tends to be record-in
                    else:
                        rec_in = timecodes[0]
                    events.append((idx, rec_in, line))
                else:
                    events.append((idx, None, line))

        # Second pass: for each event, scan forward until next event to find LOC line
        for i, (evt_idx, rec_in, evt_line) in enumerate(events):
            # set scan end to next event index, or end of file
            if i + 1 < len(events):
                end_idx = events[i + 1][0]
            else:
                end_idx = len(lines)

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
                    # try to find shot code anywhere on the LOC line
                    m = shot_code_re.search(loc_raw)
                    if m:
                        shot_code = m.group(1).strip()
                        parsed_pairs.append({
                            "event_index": evt_idx,
                            "event_line": evt_line,
                            "loc_index": j,
                            "loc_line": loc_raw.strip(),
                            "rec_in": rec_in,
                            "shot_code": shot_code
                        })
                        found_loc = True
                        break
                    else:
                        # LOC found but no shot code pattern matched
                        parse_errors.append({
                            "event_index": evt_idx,
                            "loc_index": j,
                            "loc_line": loc_raw.strip(),
                            "reason": "loc_found_but_no_shot_code"
                        })
                        found_loc = True
                        break

            if not found_loc:
                parse_errors.append({
                    "event_index": evt_idx,
                    "event_line": evt_line,
                    "reason": "no_loc_found_between_events"
                })

        # Always return the parsed pairs so you can see what was detected
        result = {
            "parsed_count": len(parsed_pairs),
            "parsed": parsed_pairs,
            "parse_errors": parse_errors,
            "updated": 0,
            "skipped": 0,
            "update_errors": []
        }

        # Attempt to update ShotGrid for each parsed pair
        for p in parsed_pairs:
            shot_code = p["shot_code"]
            rec_in = p["rec_in"]
            try:
                shot = sg.find_one("Shot", [["code", "is", shot_code]], ["id"])
                if shot:
                    sg.update("Shot", shot["id"], {"sg_timecode": rec_in})
                    result["updated"] += 1
                else:
                    result["skipped"] += 1
            except Exception as e:
                result["update_errors"].append({
                    "shot_code": shot_code,
                    "rec_in": rec_in,
                    "error": str(e)
                })

        return jsonify(result), 200

    except Exception as e:
        # catch-all so we return useful info instead of a 500 with no details
        return jsonify({"fatal_error": str(e)}), 500

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
