#!/usr/bin/env python3
"""HTTP service behind Mile Marker's Smooth tab.

Preview only for now: it fetches a Smashrun run, backs it up, runs the pipeline and returns the
before/after numbers and plots. It never writes to Smashrun.

    POST /preview          {"link": "<smashrun link or id>"}  -> 202 {"id": "<job id>", ...}
                           or {"latest": true}: your most recent run with a GPS track
    GET  /jobs/<id>        job status, and the result when it's done
    GET  /jobs/<id>/pace.png, /jobs/<id>/map.png
    GET  /healthz

Jobs run one at a time on a background thread (the pipeline takes ~1 s with the local map, but
the Overpass fallback can take minutes). Only the most recent jobs are kept, in memory.
Listens on the Docker network only; Mile Marker forwards requests to it.
"""
import json
import math
import os
import queue
import re
import secrets
import shutil
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import numpy as np

from smoothrun import pipeline, report
from smoothrun import smashrun as S

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
TOKEN_FILE = os.environ.get("TOKEN_FILE", S.DEFAULT_TOKEN)
CACHE_DIR = os.environ.get("CACHE_DIR", "cache")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join("output", "web"))
KEEP_JOBS = 20
MAX_BODY = 4096
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
PLOTS = {"pace.png", "map.png"}

jobs = {}                 # id -> dict, insertion-ordered
jobs_lock = threading.Lock()
work = queue.Queue()


def clean(v):
    """JSON-safe: numpy scalars to Python, NaN/inf to None."""
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        v = float(v)
        return v if math.isfinite(v) else None
    return v


def split_rows(t, dist, unit_m):
    return [{"frac": frac, "time_s": dt, "pace_s": pace} for frac, dt, pace in report.splits(t, dist, unit_m)]


def latest_gps_run(client):
    """ID of the most recent run that has a GPS track (skips treadmill and manual entries)."""
    runs = [a for a in client.recent(10) or [] if a.get("hasDetailsGPS") and not a.get("isTreadmill")]
    if not runs:
        raise S.SmashrunError("none of your 10 most recent runs on Smashrun has a GPS track")
    return max(runs, key=lambda a: datetime.fromisoformat(a["startDateTimeLocal"]))["activityId"]


def preview(job):
    """Fetch, back up and smooth one run. Returns the result dict shown in the Smooth tab."""
    client = S.Client(TOKEN_FILE)
    if job.get("latest"):
        job["step"] = "Finding your latest run"
        aid = int(latest_gps_run(client))
    else:
        aid = S.activity_id(job["link"])
    job["activity_id"] = aid
    job["step"] = "Fetching the run from Smashrun"
    original = client.get(aid)
    bpath = S.backup(original, BACKUP_DIR)
    warnings = S.warnings_for(original)

    job["step"] = "Smoothing the GPS track"
    run = S.to_run(original)
    out = pipeline.run(run.t, run.lat, run.lon, run.cadence, cache_dir=CACHE_DIR)
    rec = np.maximum.accumulate(np.nan_to_num(run.dist_km * 1000.0))
    # What Smashrun would end up with after a replace (pauses settled, distance non-decreasing).
    new = S.smoothed_activity(original, out.lat, out.lon, out.dist)
    new_track = np.array([np.nan if x is None else x for x in
                          new["recordingValues"][new["recordingKeys"].index("distance")]], float) * 1000 \
        if "distance" in new["recordingKeys"] else np.asarray(out.dist)

    job["step"] = "Drawing the plots"
    folder = os.path.join(OUTPUT_DIR, job["id"])
    os.makedirs(folder, exist_ok=True)
    rec_v = np.r_[0, np.diff(rec) / np.maximum(np.diff(run.t), 1e-3)]
    title = "Smashrun %d" % aid
    report.plot(os.path.join(folder, "pace.png"),
                SimpleNamespace(t=run.t, speed=rec_v, cadence=run.cadence), out.smoothed, None, title=title)
    plots = ["pace"]
    if out.match is not None:
        report.plot_map(os.path.join(folder, "map.png"), out.net, out.smoothed, out.match, title=title)
        plots.insert(0, "map")

    t = run.t
    old_pauses = original.get("pauseIndexes") or []
    return {
        "activity_id": aid,
        "start": original.get("startDateTimeLocal"),
        "elapsed_s": float(t[-1] - t[0]),
        "points": int(len(t)),
        "is_race": bool(original.get("isRace")),
        "warnings": warnings,
        "backup": os.path.basename(bpath),
        "distance": {
            "smashrun_m": float(original.get("distance") or 0) * 1000.0,
            "recorded_m": float(rec[-1]),
            "smoothed_m": float(out.smoothed.dist[-1]),
            "route_m": out.distance if out.match is not None else None,
            "new_m": float(new["distance"]) * 1000.0,
        },
        "map_matched": out.match is not None,
        "cadence_pct": 100.0 * float(np.mean(out.along.cad_used)) if out.used_cadence else None,
        "offmap_stretches": len(out.match.splices or []) if out.match is not None else 0,
        "pauses": {"recorded": len(old_pauses), "kept": len(new["pauseIndexes"])},
        "moving_s": float(new["duration"]),
        "splits": {
            "mi": {"recorded": split_rows(t, rec, report.MILE), "smoothed": split_rows(t, new_track, report.MILE)},
            "km": {"recorded": split_rows(t, rec, 1000.0), "smoothed": split_rows(t, new_track, 1000.0)},
        },
        "plots": plots,
    }


def worker():
    while True:
        job = work.get()
        job["status"] = "running"
        job["started"] = time.time()
        try:
            job["result"] = clean(preview(job))
            job["status"] = "done"
        except S.SmashrunError as e:
            job["status"], job["error"] = "error", str(e)
        except FileNotFoundError as e:
            if e.filename == TOKEN_FILE:
                job["status"], job["error"] = "error", "The Smashrun token file is missing on the server."
            else:
                traceback.print_exc()
                job["status"], job["error"] = "error", "A file the smoother needs is missing: %s" % e.filename
        except Exception as e:
            traceback.print_exc()
            job["status"], job["error"] = "error", "Smoothing failed: %s" % e
        finally:
            job["finished"] = time.time()
            job.pop("step", None)
            work.task_done()


def add_job(link=None, latest=False):
    job = {"id": secrets.token_urlsafe(12), "link": link, "latest": latest, "status": "queued",
           "created": time.time()}
    with jobs_lock:
        jobs[job["id"]] = job
        while len(jobs) > KEEP_JOBS:
            old_id, old = next(iter(jobs.items()))
            if old["status"] in ("queued", "running"):
                break
            del jobs[old_id]
            shutil.rmtree(os.path.join(OUTPUT_DIR, old_id), ignore_errors=True)
    work.put(job)
    return job


def public(job):
    keys = ("id", "status", "step", "activity_id", "error", "result", "created", "finished")
    return {k: job[k] for k in keys if k in job}


class Handler(BaseHTTPRequestHandler):
    server_version = "run-smooth"

    def log_message(self, fmt, *args):  # one short line per request, never bodies
        print("%s %s" % (self.command, self.path.split("?")[0]), flush=True)

    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self.send_json(200, {"ok": True, "token": os.path.isfile(TOKEN_FILE),
                                        "mapstore": os.path.isfile(os.path.join(CACHE_DIR, "mapstore.sqlite"))})
        m = re.match(r"^/jobs/([^/]+)(?:/([a-z]+\.png))?$", path)
        if not m or not JOB_ID.match(m.group(1)):
            return self.send_json(404, {"error": "Not found."})
        with jobs_lock:
            job = jobs.get(m.group(1))
        if not job:
            return self.send_json(404, {"error": "That preview has expired. Run it again."})
        if not m.group(2):
            return self.send_json(200, public(job))
        if m.group(2) not in PLOTS:
            return self.send_json(404, {"error": "Not found."})
        try:
            with open(os.path.join(OUTPUT_DIR, job["id"], m.group(2)), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self.send_json(404, {"error": "Not found."})
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.split("?")[0] != "/preview":
            return self.send_json(404, {"error": "Not found."})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.send_json(415, {"error": "Send JSON."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.send_json(413, {"error": "Request too large."})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            link = str(body.get("link") or "").strip()
            latest = body.get("latest") is True
        except (ValueError, AttributeError):
            return self.send_json(400, {"error": "The request body must be a JSON object."})
        if latest:
            return self.send_json(202, public(add_job(latest=True)))
        if not link:
            return self.send_json(400, {"error": "Paste a Smashrun link or activity ID."})
        try:
            S.activity_id(link)  # reject bad links straight away rather than in the job
        except S.SmashrunError as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(202, public(add_job(link=link)))


def main():
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)  # jobs live in memory, so old plots are orphans
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("run-smooth listening on http://%s:%d (preview only; token %s)"
          % (HOST, PORT, "found" if os.path.isfile(TOKEN_FILE) else "MISSING"), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
