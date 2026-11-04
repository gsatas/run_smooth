"""Smashrun API: fetch a run, convert it for the smoother, build the smoothed replacement.

The token is read from a file and only ever sent in the Authorization header.
Every fetched activity is saved untouched to the backups directory before anything else.
"""
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

API = "https://api.smashrun.com/v1"
DEFAULT_TOKEN = os.path.expanduser("~/.config/mile_marker/smashrun_token")
MARKER = "[smoothed]"      # legacy notes marker; runs this tool writes get externalId "smoothrun-..."


class SmashrunError(RuntimeError):
    pass


def activity_id(link_or_id):
    """Accept '47363104' or a link like https://smashrun.com/<user>/run/47363104/..."""
    s = str(link_or_id).strip()
    if s.isdigit():
        return int(s)
    # Smashrun also links runs by date (/run/2026/11/3), which would otherwise parse as id 2026.
    if re.search(r"/run/\d{4}/\d{1,2}/\d{1,2}(?:\D|$)", s):
        raise SmashrunError("that link has the run's date, not its ID. Use a link that ends in the "
                            "run's ID number, like .../run/47383521")
    m = re.search(r"/run/(\d+)", s) or re.search(r"(\d{6,})", s)
    if not m:
        raise SmashrunError("couldn't find an activity id in %r" % s)
    return int(m.group(1))


class Client:
    def __init__(self, token_file=DEFAULT_TOKEN):
        with open(token_file) as f:
            self._token = f.read().strip()
        if not self._token:
            raise SmashrunError("token file %s is empty" % token_file)

    def _call(self, method, path, body=None):
        # curl rather than urllib: macOS system Python's LibreSSL fails TLS with some hosts.
        # The token goes in via a header read from stdin (-H @-) so it never shows in `ps`.
        args = ["curl", "-sS", "-m", "120", "-X", method, "-w", "\n%{http_code}",
                "-H", "@-", "-H", "Accept: application/json"]
        headers = "Authorization: Bearer %s\n" % self._token
        if body is not None:
            args += ["-H", "Content-Type: application/json", "--data-binary", "@%s" % body]
        out = subprocess.run(args + [API + path], input=headers.encode(), capture_output=True)
        text = out.stdout.decode(errors="replace").replace(self._token, "<redacted>")
        payload, _, code = text.rpartition("\n")
        if out.returncode != 0 or not code.isdigit():
            raise SmashrunError("%s %s failed: %s" % (method, path, out.stderr.decode()[:300]))
        code = int(code)
        if code == 401:
            raise SmashrunError("Smashrun rejected the token (expired? personal tokens last ~60 "
                                "days). Get a new one and update the token file.")
        if code >= 400:
            raise SmashrunError("%s %s -> HTTP %d: %s" % (method, path, code, payload[:300]))
        return json.loads(payload) if payload.strip() else None

    def get(self, aid):
        return self._call("GET", "/my/activities/%d" % aid)

    def recent(self, count=10):
        return self._call("GET", "/my/activities/search?count=%d" % count)


def backup(activity, backup_dir="backups"):
    """Save the untouched original. Never overwrites an existing backup of the same id."""
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, "%d_original.json" % activity["activityId"])
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(activity, f)
    return path


@dataclass
class Run:
    activity: dict
    t: np.ndarray          # seconds since start (clock)
    lat: np.ndarray
    lon: np.ndarray
    cadence: np.ndarray    # strides/min (half of steps/min), NaN if missing
    dist_km: np.ndarray    # recorded cumulative distance
    start: datetime


def to_run(activity):
    keys = activity.get("recordingKeys") or []
    vals = activity.get("recordingValues") or []
    if "latitude" not in keys or "clock" not in keys:
        raise SmashrunError("activity %s has no GPS track" % activity.get("activityId"))
    series = {k: np.array([np.nan if x is None else x for x in v], float) for k, v in zip(keys, vals)}
    n = len(series["clock"])
    # Smashrun uses (-1, -1) for "no GPS fix yet"; treat that (and 0,0 / out-of-range) as missing.
    la, lo = series["latitude"], series["longitude"]
    bad = (((la == -1) & (lo == -1)) | ((la == 0) & (lo == 0)) | (np.abs(la) > 90)
           | (np.abs(lo) > 180))
    la[bad], lo[bad] = np.nan, np.nan
    cad = series.get("cadence", np.full(n, np.nan))
    # Smashrun reports steps/min (~150); the smoother works in strides/min (~75), like TCX.
    if np.nanmedian(np.where(cad > 0, cad, np.nan)) > 120:
        cad = cad / 2.0
    start = datetime.fromisoformat(activity["startDateTimeLocal"])
    return Run(activity, series["clock"], series["latitude"], series["longitude"], cad,
               series.get("distance", np.full(n, np.nan)), start)


def warnings_for(activity):
    """Reasons this run may already have been processed (by hand or by this tool)."""
    w = []
    if (activity.get("externalId") or "").startswith("smoothrun-") or MARKER in (activity.get("notes") or ""):
        w.append("already smoothed by this tool")
    src = (activity.get("source") or "").lower()
    if src in ("gpxfile", "tcxfile", "fitfile", "manual"):
        w.append("uploaded from a file (source=%s), not straight from your phone" % src)
    keys = activity.get("recordingKeys") or []
    if "distance" in keys:
        d = activity["recordingValues"][keys.index("distance")]
        track_km = max(x for x in d if x is not None)
        if abs(track_km - activity["distance"]) > 0.02 * max(track_km, 0.1):
            w.append("headline distance %.2f km doesn't match the track's %.2f km (edited by hand?)"
                     % (activity["distance"], track_km))
    return w


def settle_pauses(t, dist_m, candidates, stopped_speed=0.5):
    """Smashrun zeroes distance across every pause. Keep a pause only where the smoother says you
    were actually stopped; drop false auto-pauses (you kept moving) so their distance counts.
    Returns (pause indexes, distance series consistent with them, moving duration)."""
    t = np.asarray(t, float)
    # Smashrun rejects distance series that ever decrease (even by rounding), so enforce it.
    d = np.maximum.accumulate(np.nan_to_num(np.asarray(dist_m, float)))
    keep, removed = [], np.zeros(len(d))
    for i in sorted(set(int(i) for i in candidates if 0 < int(i) < len(t))):
        gap = t[i] - t[i - 1]
        step = d[i] - d[i - 1]
        if step <= stopped_speed * max(gap, 1.0):
            keep.append(i)
            removed[i] = step
    d = np.maximum.accumulate(d - np.cumsum(removed))
    moving = float(t[-1] - t[0]) - sum(float(t[i] - t[i - 1]) for i in keep)
    return keep, d, moving


def smoothed_activity(original, lat, lon, dist_m):
    """A new activity: the original with the track replaced and headline distance updated."""
    a = json.loads(json.dumps(original))
    keys = a["recordingKeys"]
    vals = [list(v) for v in a["recordingValues"]]

    def put(key, arr, nd):
        vals[keys.index(key)] = [None if not np.isfinite(x) else round(float(x), nd) for x in arr]

    t = np.array([np.nan if x is None else x for x in vals[keys.index("clock")]], float)
    pauses, dist_m, moving = settle_pauses(t, dist_m, original.get("pauseIndexes") or [])
    put("latitude", lat, 7)
    put("longitude", lon, 7)
    if "distance" in keys:
        put("distance", np.asarray(dist_m) / 1000.0, 5)
    a["recordingValues"] = vals
    a["pauseIndexes"] = pauses
    a["duration"] = round(moving, 2)
    a["distance"] = round(float(dist_m[-1]) / 1000.0, 5)
    return a


# ---------------------------------------------------------------------------------------------
# Writing back. Fields Smashrun computes itself (ids, weather, sun, elevation stats, location...)
# are left out of uploads; these are the ones we carry over from the run being replaced.
CARRY_OVER = ["activityType", "startDateTimeLocal", "duration", "notes", "howFelt", "terrain",
              "isRace", "isTreadmill", "isBadHR", "isBadElevation", "calories", "laps",
              "pauseIndexes", "recordingKeys", "recordingValues", "distance"]


def upload_body(activity, external_id):
    body = {k: activity[k] for k in CARRY_OVER if k in activity and activity[k] is not None}
    body["externalId"] = external_id
    return body


def activity_from_track(meta, start_local, t, lat, lon, dist_m, cadence=None, elevation=None,
                        pause_idx=None):
    """Build a Smashrun activity from arrays (e.g. an original TCX), taking user-entered
    metadata (notes, how-felt, race flag...) from `meta`, the activity being replaced."""
    if pause_idx is None:
        dt = np.diff(t)
        pause_idx = [int(i) + 1 for i in np.where(dt > max(5.0, 3 * np.median(dt)))[0]]
    pause_idx, dist_m, moving = settle_pauses(t, dist_m, pause_idx)
    keys, vals = ["clock", "latitude", "longitude", "distance"], []
    rnd = lambda arr, nd: [None if not np.isfinite(x) else round(float(x), nd) for x in arr]
    vals = [rnd(t, 2), rnd(lat, 7), rnd(lon, 7), rnd(np.asarray(dist_m) / 1000.0, 5)]
    if elevation is not None and np.any(np.isfinite(elevation)):
        keys.append("elevation")
        vals.append(rnd(elevation, 1))
    if cadence is not None and np.any(np.isfinite(cadence)):
        keys.append("cadence")
        vals.append(rnd(cadence, 0))
    a = {k: meta[k] for k in CARRY_OVER if k in meta and k not in
         ("recordingKeys", "recordingValues", "pauseIndexes", "distance", "startDateTimeLocal",
          "duration", "laps")}
    a.update(recordingKeys=keys, recordingValues=vals, pauseIndexes=list(pause_idx),
             startDateTimeLocal=start_local, distance=round(float(dist_m[-1]) / 1000.0, 5),
             duration=round(moving, 2))
    return a


def _write_tmp(obj):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    return path


def post(client, activity, external_id):
    path = _write_tmp(upload_body(activity, external_id))
    try:
        return client._call("POST", "/my/activities/", body=path)
    finally:
        os.remove(path)


def delete(client, aid):
    return client._call("DELETE", "/my/activities/%d" % aid)


def replace(client, old_id, new_activity, backup_path, log=print, external_id=None, info=None):
    """Delete `old_id` and upload `new_activity` in its place. If the upload fails, re-upload
    the activity saved at `backup_path` so the run isn't lost. Returns the new activity id.
    `info`, if given, receives Smashrun's "validations" notes for the upload."""
    if not os.path.exists(backup_path):
        raise SmashrunError("refusing to replace %d: no backup at %s" % (old_id, backup_path))
    with open(backup_path) as f:
        fallback = json.load(f)   # read before deleting, so a bad backup stops us early
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log("deleting %d (backup: %s)" % (old_id, backup_path))
    delete(client, old_id)
    try:
        resp = post(client, new_activity, external_id or "smoothrun-%d-%s" % (old_id, stamp))
        log("uploaded replacement: %s" % json.dumps(resp)[:200])
    except Exception as e:
        log("UPLOAD FAILED (%s) - putting back the backed-up version" % e)
        try:
            post(client, fallback, fallback.get("externalId") or "restore-%d-%s" % (old_id, stamp))
        except Exception as e2:
            raise SmashrunError("the upload failed (%s) and putting the run back also failed (%s). "
                                "Run %d is deleted on Smashrun; its saved copy is %s"
                                % (e, e2, old_id, os.path.basename(backup_path)))
        raise SmashrunError("the upload failed, so the run was put back as it was: %s" % e)
    if info is not None and isinstance(resp, dict):
        info["validations"] = resp.get("validations") or {}
    return _new_id(resp)


def original_id_of(activity):
    """For a run this tool wrote (externalId smoothrun-<old id>-<stamp>), the id it replaced."""
    m = re.match(r"smoothrun-(\d+)-", activity.get("externalId") or "")
    return int(m.group(1)) if m else None


def restore(client, aid, backup_dir="backups", log=print, info=None):
    """Put the original back in place of run `aid`, which this tool wrote. The current (smoothed)
    version is saved first, so a failed restore can fall back to it. Returns the new id."""
    current = client.get(aid)
    orig_id = original_id_of(current)
    if orig_id is None:
        raise SmashrunError("run %d wasn't written by this tool, so there's no original to restore" % aid)
    path = os.path.join(backup_dir, "%d_original.json" % orig_id)
    if not os.path.exists(path):
        raise SmashrunError("there's no backup of the original (%s)" % os.path.basename(path))
    with open(path) as f:
        original = json.load(f)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    snap = os.path.join(backup_dir, "%d_smoothed_%s.json" % (aid, stamp))
    with open(snap, "x") as f:
        json.dump(current, f)
    ext = original.get("externalId") or "restore-%d-%s" % (orig_id, stamp)
    new_id = replace(client, aid, original, snap, log=log, external_id=ext, info=info)
    if info is not None:
        info.update(original_id=orig_id, distance_km=original.get("distance"), duration=original.get("duration"))
    return new_id


def _new_id(resp):
    if isinstance(resp, list) and resp:
        resp = resp[0]
    if isinstance(resp, dict) and resp.get("activityIds"):
        return int(resp["activityIds"][0])
    if isinstance(resp, dict):
        for k in ("activityId", "id"):
            if k in resp:
                return int(resp[k])
    if isinstance(resp, (int, str)) and str(resp).isdigit():
        return int(resp)
    return None
