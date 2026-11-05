#!/usr/bin/env python3
"""HTTP service behind Mile Marker's Smooth tab.

A preview fetches a Smashrun run, backs it up, runs the pipeline and returns the before/after
numbers and plots; it never changes Smashrun. Replace and restore do, and need the passphrase.

    POST /preview          {"link": "<smashrun link or id>"}  -> 202 {"id": "<job id>", ...}
                           or {"latest": true}: your most recent run with a GPS track
    POST /replace          {"job": "<preview job id>", "passphrase": "...", "force": false}
    POST /restore          {"activity_id": 123, "passphrase": "..."}
    POST /token            {"token": "...", "passphrase": "..."}: check a new Smashrun token and
                           save it. Never returned or logged.
    GET  /jobs/<id>        job status, and the result when it's done
    GET  /jobs/<id>/pace.png, /jobs/<id>/map.png
    GET  /healthz

The local copy of your runs (smoothrun/mirror.py), for Mile Marker's stats. Sync only reads
from Smashrun:
    POST /runs/sync        {} -> 202 the sync job (the one already queued or running, if any)
    GET  /runs/sync        the latest sync job, or {"status": "never"}
    GET  /runs             the index: one summary row per run, newest first
    GET  /runs/<id>        the run's full Smashrun activity JSON, as saved
    GET  /achievements     every achievement with what you've earned and your progress
                           (smoothrun/achievements.py; recomputed after each sync)

Jobs, writes included, run one at a time on a background thread (the pipeline takes ~1 s with
the local map, but the Overpass fallback can take minutes). Only recent jobs are kept, in memory.
Writes are refused without a passphrase file on the server.
Listens on the Docker network only; Mile Marker forwards requests to it.
"""
import hashlib
import hmac
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

from smoothrun import achievements, mirror, pipeline, report, weather
from smoothrun import smashrun as S

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
TOKEN_FILE = os.environ.get("TOKEN_FILE", S.DEFAULT_TOKEN)
PASSPHRASE_FILE = os.environ.get("PASSPHRASE_FILE",
                                 os.path.join(os.path.dirname(S.DEFAULT_TOKEN), "smooth_passphrase"))
CACHE_DIR = os.environ.get("CACHE_DIR", "cache")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join("output", "web"))
KEEP_JOBS = 20
MAX_BODY = 4096
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
PLOTS = {"pace.png", "map.png"}

jobs = {}                 # id -> dict, insertion-ordered
jobs_lock = threading.RLock()   # reentrant: post_replace calls add_job while holding it
work = queue.Queue()
failures = []             # times of recent wrong passphrases
failures_lock = threading.Lock()
MAX_FAILURES, FAILURE_WINDOW = 5, 600


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


_user = {}


def run_link(client, aid):
    """The run's page on Smashrun, by ID (https://smashrun.com/<user>/run/<id>)."""
    if not aid:
        return None
    if not _user.get("name"):
        try:
            _user["name"] = (client._call("GET", "/my/userinfo") or {}).get("userName")
        except S.SmashrunError as e:   # a missing link mustn't fail the preview or a write
            log("couldn't get the Smashrun username: %s" % e)
            return None
    return "https://smashrun.com/%s/run/%d" % (_user["name"], aid) if _user.get("name") else None


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
    orig_id = S.original_id_of(original)
    restorable = orig_id is not None and os.path.exists(os.path.join(BACKUP_DIR, "%d_original.json" % orig_id))

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

    # Kept for a replace: what to upload, and what the run looked like when previewed.
    job["_new"], job["_bpath"] = new, bpath
    job["_seen"] = {k: original.get(k) for k in SEEN_KEYS}

    t = run.t
    old_pauses = original.get("pauseIndexes") or []
    return {
        "activity_id": aid,
        "start": original.get("startDateTimeLocal"),
        "elapsed_s": float(t[-1] - t[0]),
        "points": int(len(t)),
        "is_race": bool(original.get("isRace")),
        "link": run_link(client, aid),
        "duration_s": float(original.get("duration") or 0),
        "smoothed": orig_id is not None,
        "warnings": warnings,
        "backup": os.path.basename(bpath),
        "restorable": restorable,
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


SEEN_KEYS = ("startDateTimeLocal", "distance", "duration", "externalId")


def log(msg):
    print("  " + msg, flush=True)


def replace(job):
    """Apply a finished preview: swap the run on Smashrun for its smoothed version."""
    p = job["preview"]
    aid = p["activity_id"]
    client = S.Client(TOKEN_FILE)
    job["step"] = "Checking the run hasn't changed since the preview"
    current = client.get(aid)
    if any(current.get(k) != v for k, v in p["_seen"].items()):
        p.pop("applied", None)   # nothing was changed, so this preview may be tried again later
        raise S.SmashrunError("the run changed on Smashrun since the preview. Preview it again first")
    job["step"] = "Replacing the run on Smashrun"
    info = {}
    log("replace %d (preview %s)" % (aid, p["id"]))
    new_id = S.replace(client, aid, p["_new"], p["_bpath"], log=log, info=info)
    _update_copy(client, aid, new_id)
    return {"action": "replace", "old_id": aid, "new_id": new_id,
            "link": run_link(client, new_id or aid), "duration_s": float(p["_new"]["duration"]),
            "distance_m": float(p["_new"]["distance"]) * 1000.0,
            "is_race": bool(current.get("isRace")), "validations": info.get("validations") or {}}


def _update_copy(client, old_id, new_id):
    """Keep the local run copy and achievements in step with a replace or restore. Smashrun has
    already changed by now, so a problem here is logged rather than failing the job."""
    try:
        if mirror.swap(client, old_id, new_id, log=log):
            achievements.compute()
    except Exception:
        traceback.print_exc()


def restore(job):
    """Put the backed-up original back in place of a run this tool wrote."""
    aid = job["activity_id"]
    client = S.Client(TOKEN_FILE)
    job["step"] = "Restoring the original on Smashrun"
    info = {}
    log("restore %d" % aid)
    new_id = S.restore(client, aid, BACKUP_DIR, log=log, info=info)
    _update_copy(client, aid, new_id)
    return {"action": "restore", "old_id": aid, "new_id": new_id, "original_id": info.get("original_id"),
            "link": run_link(client, new_id or aid), "duration_s": float(info.get("duration") or 0),
            "distance_m": float(info.get("distance_km") or 0) * 1000.0,
            "validations": info.get("validations") or {}}


def sync_runs(job):
    """Update the local copy of your runs from Smashrun."""
    result = mirror.sync(S.Client(TOKEN_FILE), step=lambda s: job.__setitem__("step", s), log=log)
    job["step"] = "Checking achievements"
    try:   # a rule bug mustn't fail the sync; the runs are already saved
        achievements.compute()
    except Exception:
        traceback.print_exc()
    return result


RUNNERS = {"preview": (preview, "Smoothing failed"), "replace": (replace, "The replace failed"),
           "restore": (restore, "The restore failed"), "sync": (sync_runs, "The sync failed")}


def worker():
    while True:
        job = work.get()
        job["status"] = "running"
        job["started"] = time.time()
        run, failed = RUNNERS[job["kind"]]
        try:
            job["result"] = clean(run(job))
            job["status"] = "done"
        except S.TokenError:
            job["status"], job["token_expired"] = "error", True
            job["error"] = "Smashrun rejected the token. Personal tokens expire about every 60 days."
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
            job["status"], job["error"] = "error", "%s: %s" % (failed, e)
        finally:
            job["finished"] = time.time()
            job.pop("step", None)
            work.task_done()


def add_job(kind="preview", **fields):
    job = dict(fields, id=secrets.token_urlsafe(12), kind=kind, status="queued", created=time.time())
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


def latest_sync():
    with jobs_lock:
        return next((j for j in reversed(list(jobs.values())) if j["kind"] == "sync"), None)


def public(job):
    keys = ("id", "kind", "status", "step", "activity_id", "error", "token_expired", "result", "created", "finished")
    return {k: job[k] for k in keys if k in job}


def passphrase_problem(given):
    """None if `given` is the passphrase, else (status, message). Wrong guesses are slowed and
    limited to MAX_FAILURES per FAILURE_WINDOW seconds."""
    try:
        with open(PASSPHRASE_FILE) as f:
            expected = f.read().strip()
    except FileNotFoundError:
        expected = ""
    if not expected:
        return 503, "Changing runs is turned off: there's no passphrase set on the server."
    now = time.time()
    with failures_lock:
        failures[:] = [t for t in failures if now - t < FAILURE_WINDOW]
        if len(failures) >= MAX_FAILURES:
            return 429, "Too many wrong passphrases. Try again in 10 minutes."
    digest = lambda x: hashlib.sha256(x.encode()).digest()
    if hmac.compare_digest(digest(str(given or "")), digest(expected)):
        return None
    with failures_lock:
        failures.append(now)
    time.sleep(1)
    return 403, "That passphrase isn't right."


def save_token(token):
    """Write the token in place. The file is its own read-write bind mount in Docker (the folder
    around it is read-only), so it can't be swapped by rename; it's a few bytes, written at once."""
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(TOKEN_FILE, 0o600)


def writes_enabled():
    try:
        with open(PASSPHRASE_FILE) as f:
            return bool(f.read().strip())
    except OSError:
        return False


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
            return self.send_json(200, {"ok": True, "token": os.path.isfile(TOKEN_FILE), "writes": writes_enabled(),
                                        "mapstore": os.path.isfile(os.path.join(CACHE_DIR, "mapstore.sqlite"))})
        if path == "/runs" or path.startswith("/runs/"):
            return self.get_runs(path)
        if path == "/achievements":
            try:
                return self.send_json(200, achievements.load())
            except Exception:
                traceback.print_exc()
                return self.send_json(500, {"error": "The achievements couldn’t be worked out."})
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

    def get_runs(self, path):
        if path == "/runs":
            index = mirror.load()
            wx = weather.saved()   # Open-Meteo's hours, for runs Smashrun has no weather for
            for row in index.get("runs", []):
                if wx.get(str(row["id"])):
                    row["wx"] = wx[str(row["id"])]
            return self.send_json(200, index)
        if path == "/runs/sync":
            job = latest_sync()
            return self.send_json(200, public(job) if job else {"status": "never"})
        m = re.match(r"^/runs/(\d{1,12})$", path)
        if not m:
            return self.send_json(404, {"error": "Not found."})
        try:
            with open(os.path.join(mirror.MIRROR_DIR, "activities", "%d.json" % int(m.group(1))), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self.send_json(404, {"error": "That run isn't in the local copy. Sync first."})
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/preview", "/replace", "/restore", "/token", "/runs/sync"):
            return self.send_json(404, {"error": "Not found."})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.send_json(415, {"error": "Send JSON."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.send_json(413, {"error": "Request too large."})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send_json(400, {"error": "The request body must be a JSON object."})
        if path == "/replace":
            return self.post_replace(body)
        if path == "/restore":
            return self.post_restore(body)
        if path == "/token":
            return self.post_token(body)
        if path == "/runs/sync":
            with jobs_lock:   # one sync at a time; a second tap gets the same job
                job = latest_sync()
                if not job or job["status"] not in ("queued", "running"):
                    job = add_job("sync")
            return self.send_json(202, public(job))
        link = str(body.get("link") or "").strip()
        latest = body.get("latest") is True
        if latest:
            return self.send_json(202, public(add_job(latest=True)))
        if not link:
            return self.send_json(400, {"error": "Paste a Smashrun link or activity ID."})
        try:
            S.activity_id(link)  # reject bad links straight away rather than in the job
        except S.SmashrunError as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(202, public(add_job(link=link)))

    def post_replace(self, body):
        with jobs_lock:
            p = jobs.get(str(body.get("job") or ""))
        if not p or p["kind"] != "preview" or p["status"] != "done":
            return self.send_json(404, {"error": "That preview has expired. Run it again."})
        problem = passphrase_problem(body.get("passphrase"))
        if problem:
            return self.send_json(problem[0], {"error": problem[1]})
        if p["result"]["warnings"] and body.get("force") is not True:
            return self.send_json(409, {"error": "This run looks already processed. Tick Replace anyway "
                                                 "if you're sure."})
        with jobs_lock:   # one replace per preview: a second tap must not delete the new run
            if p.get("applied"):
                return self.send_json(409, {"error": "This preview was already used. Preview the run again."})
            job = add_job("replace", preview=p, activity_id=p["activity_id"])
            p["applied"] = job["id"]
        return self.send_json(202, public(job))

    def post_token(self, body):
        problem = passphrase_problem(body.get("passphrase"))
        if problem:
            return self.send_json(problem[0], {"error": problem[1]})
        token = str(body.get("token") or "").strip()
        if not token or len(token) > 500 or any(c.isspace() or not c.isprintable() for c in token):
            return self.send_json(400, {"error": "Paste the token by itself, with no spaces."})
        try:   # only save a token Smashrun accepts, so a typo can't break one that works
            user = (S.Client(token=token)._call("GET", "/my/userinfo") or {}).get("userName")
        except S.TokenError:
            return self.send_json(400, {"error": "Smashrun didn't accept that token. Check you copied all of it."})
        except S.SmashrunError:
            return self.send_json(502, {"error": "Couldn't reach Smashrun to check the token. Try again."})
        save_token(token)
        _user.clear()   # the new token may belong to another account
        log("saved a new Smashrun token")
        return self.send_json(200, {"ok": True, "user": user})

    def post_restore(self, body):
        try:
            aid = int(body.get("activity_id"))
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "Give the run's activity ID."})
        problem = passphrase_problem(body.get("passphrase"))
        if problem:
            return self.send_json(problem[0], {"error": problem[1]})
        return self.send_json(202, public(add_job("restore", activity_id=aid)))


def main():
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)  # jobs live in memory, so old plots are orphans
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("run-smooth listening on http://%s:%d (token %s; replacing runs %s)"
          % (HOST, PORT, "found" if os.path.isfile(TOKEN_FILE) else "MISSING",
             "on" if writes_enabled() else "off: no passphrase file"), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
