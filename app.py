"""Local web UI: pick a location, get a GreenGate3D lead .xlsx.

Runs are executed in a background thread; the browser polls /progress/<id>
for per-company status, then downloads the finished file from /download/<id>.
"""
import hmac
import io
import os
import re
import datetime
import threading
import uuid
import webbrowser
from flask import Flask, render_template, request, send_file, jsonify

import config
import history
import scraper
import excel_out

app = Flask(__name__)

JOBS = {}          # job_id -> state dict
JOBS_LOCK = threading.Lock()
MAX_JOBS = 20      # keep memory bounded


@app.route("/")
def index():
    return render_template(
        "index.html",
        default_target=config.DEFAULT_TARGET,
        mock=config.MOCK_MODE,
        has_keys=bool(config.GOOGLE_PLACES_API_KEY and config.ANTHROPIC_API_KEY),
        hosted=config.HOSTED,
        password_enabled=bool(config.APP_PASSWORD),
    )


def _resolve_keys(form):
    """Keys for one run: the visitor's own, else the server's when permitted.

    Locally the .env keys are always usable. On a hosted copy they are only
    usable with APP_PASSWORD; otherwise strangers could spend them.
    Returns (keys, error_message).
    """
    password = form.get("password") or ""
    if password and not (config.APP_PASSWORD and hmac.compare_digest(
            password.encode(), config.APP_PASSWORD.encode())):
        return None, "Wrong password."
    server_ok = not config.HOSTED or bool(password)
    keys = {}
    for name, field in (("GOOGLE_PLACES_API_KEY", "google_key"),
                        ("ANTHROPIC_API_KEY", "anthropic_key")):
        keys[name] = ((form.get(field) or "").strip()
                      or (getattr(config, name) if server_ok else ""))
    return keys, None


def _run_job(job_id, location, target, max_results, depth, keys, exclude, ns):
    job = JOBS[job_id]

    def progress(i, n, name):
        job["done"], job["total"], job["current"] = i, n, name

    try:
        # Keys stay in this thread's scope only; they are never written to
        # JOBS, so /progress can't echo them back.
        with config.use_keys(keys):
            leads = scraper.collect(location, target, max_results=max_results,
                                    progress=progress, depth=depth,
                                    exclude=exclude)
        # Recorded even when every place was dropped: re-crawling them next
        # time would cost the same and drop them again.
        if ns and not config.MOCK_MODE:
            history.record(ns, getattr(leads, "processed", ()), location)
        if not leads:
            seen = getattr(leads, "seen_before", 0)
            job["status"] = "error"
            if getattr(leads, "dropped", 0):
                job["error"] = (f"Found {leads.dropped} new businesses, but none showed any 3D "
                                "printers or equipment. Try a different business type, or "
                                "set DROP_EMPTY_LEADS = False in config.py.")
            elif seen:
                job["error"] = (f"No new businesses: all {seen} found were in earlier runs. "
                                "Try a nearby location or a different business type.")
            else:
                job["error"] = "No businesses found for that location/target."
            return
        buf = io.BytesIO()
        excel_out.build(leads, buf)
        safe_loc = re.sub(r"[^A-Za-z0-9]+", "_", location).strip("_") or "results"
        stamp = datetime.date.today().isoformat()
        job["file"] = buf.getvalue()
        job["fname"] = f"{safe_loc}_{target}_GreenGate3D_{stamp}.xlsx"
        job["status"] = "done"
    except Exception as e:  # surface the real failure to the user, don't fake success
        job["status"] = "error"
        job["error"] = f"Lookup failed: {e}"


@app.route("/generate", methods=["POST"])
def generate():
    location = (request.form.get("location") or "").strip()
    target = (request.form.get("target") or config.DEFAULT_TARGET).strip()
    try:
        max_results = min(60, max(1, int(request.form.get("max_results", 20))))
    except ValueError:
        max_results = 20
    depth = (request.form.get("depth") or "deep").strip()
    if depth not in scraper.DEPTH_PRESETS:
        depth = "deep"

    if not location:
        return jsonify({"error": "Please enter a location."}), 400
    keys, err = _resolve_keys(request.form)
    if err:
        return jsonify({"error": err}), 403
    if not config.MOCK_MODE and not (keys["GOOGLE_PLACES_API_KEY"] and keys["ANTHROPIC_API_KEY"]):
        return jsonify({"error": ("Enter your Google Places and Anthropic API keys"
                                  + (", or the access password." if config.APP_PASSWORD else ".")
                                  if config.HOSTED else
                                  "Missing API keys. Add them to .env or set MOCK_MODE=1.")}), 400

    # Cross-run dedupe: the saved history plus any spreadsheets uploaded from
    # earlier runs (the only record that survives a free host's disk wipe).
    ns = history.namespace(keys["GOOGLE_PLACES_API_KEY"])
    skip_seen = request.form.get("skip_seen", "1") == "1"
    exclude = history.load(ns) if skip_seen else []
    for f in request.files.getlist("previous"):
        if not f or not f.filename:
            continue
        try:
            exclude += history.from_spreadsheet(io.BytesIO(f.read()))
        except Exception as e:
            return jsonify({"error": f"Couldn't read {f.filename}: {e}"}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        running = sum(1 for j in JOBS.values() if j["status"] == "running")
        if running >= config.MAX_CONCURRENT_JOBS:
            return jsonify({"error": "The server is busy with other searches. "
                                     "Try again in a few minutes."}), 429
        # Drop oldest finished jobs so memory stays bounded.
        while len(JOBS) >= MAX_JOBS:
            for old_id, old in list(JOBS.items()):
                if old["status"] in ("done", "error"):
                    del JOBS[old_id]
                    break
            else:
                break
        JOBS[job_id] = {"status": "running", "done": 0, "total": 0,
                        "current": "", "error": None, "file": None, "fname": None}
    threading.Thread(target=_run_job,
                     args=(job_id, location, target, max_results, depth, keys,
                           exclude, ns),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/history/clear", methods=["POST"])
def clear_history():
    """Forget earlier runs, so their places can be found again."""
    keys, err = _resolve_keys(request.form)
    if err:
        return jsonify({"error": err}), 403
    if config.HOSTED and not keys["GOOGLE_PLACES_API_KEY"]:
        return jsonify({"error": "Enter your Google key (or the password) first."}), 400
    history.clear(history.namespace(keys["GOOGLE_PLACES_API_KEY"]))
    return jsonify({"ok": True})


@app.route("/progress/<job_id>")
def progress(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    return jsonify({
        "status": job["status"], "done": job["done"], "total": job["total"],
        "current": job["current"], "error": job["error"], "fname": job["fname"],
    })


@app.route("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready."}), 404
    return send_file(
        io.BytesIO(job["file"]), as_attachment=True, download_name=job["fname"],
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _open_browser(port):
    webbrowser.open(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    # Defaults to 8000; honours PORT so it can run alongside another server.
    port = int(os.environ.get("PORT", "8000"))
    threading.Timer(1.0, _open_browser, args=(port,)).start()
    app.run(debug=False, port=port, threaded=True)
