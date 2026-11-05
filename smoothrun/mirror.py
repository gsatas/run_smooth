"""A local copy of your Smashrun runs, for the stats and plots in Mile Marker.

Each run's full activity JSON is saved untouched as <mirror>/activities/<id>.json. A sync lists
your runs since SYNC_SINCE, fetches any run that's new or has changed on Smashrun (its
dateUpdatedUTC differs from the saved copy), and marks runs that are no longer on Smashrun as
removed. Removed runs keep their file. Sync only reads from Smashrun.

index.json holds one summary row per run, rebuilt from the saved files whenever the summary
format changes, so adding a field never needs a re-fetch.

    python -m smoothrun.mirror sync [--since 2026-01-01]
    python -m smoothrun.mirror rebuild
"""
import json
import os
import re
import time
from datetime import date, datetime, timezone

from smoothrun import smashrun as S

MIRROR_DIR = os.environ.get("MIRROR_DIR", "mirror")
SYNC_SINCE = os.environ.get("SYNC_SINCE", "2026-01-01")
PAGE = 100
MAX_FETCH = 100     # detail requests per sync; Smashrun allows 250 requests an hour
SUMMARY_VERSION = 1
ACTIVITY_FILE = re.compile(r"^(\d{1,12})\.json$")


def _dir(*parts, root=None):
    return os.path.join(root or MIRROR_DIR, *parts)


def _write_atomic(path, obj):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def summary(a):
    """One row per run: the headline numbers, all distances in metres and times in seconds."""
    dist_m = (_num(a.get("distance")) or 0.0) * 1000.0
    dur = _num(a.get("duration")) or 0.0
    keys = a.get("recordingKeys") or []
    ext = a.get("externalId") or ""
    m = re.match(r"^smoothrun-(\d+)-", ext)
    return {
        "id": int(a["activityId"]),
        "start": a.get("startDateTimeLocal"),
        "date": (a.get("startDateTimeLocal") or "")[:10],
        "type": a.get("activityType"),
        "distance_m": dist_m,
        "duration_s": dur,
        "pace_s_per_km": dur / (dist_m / 1000.0) if dist_m > 0 and dur > 0 else None,
        "elevation_gain_m": _num(a.get("elevationGain")),
        "cadence_avg": _num(a.get("cadenceAverage")),
        "calories": _num(a.get("calories")),
        "temperature_c": _num(a.get("temperature")),
        "weather": a.get("weatherType"),
        "city": a.get("city"),
        "is_race": bool(a.get("isRace")),
        "is_treadmill": bool(a.get("isTreadmill")),
        "has_gps": bool(a.get("hasDetailsGPS")),
        "has_hr": "heartRate" in keys and not a.get("isBadHR"),
        "smoothed": bool(m),
        "smoothed_from": int(m.group(1)) if m else None,
        "source": a.get("source"),
        "notes": a.get("notes") or "",
        "updated": a.get("dateUpdatedUTC"),
    }


def _activities(root=None):
    folder = _dir("activities", root=root)
    if not os.path.isdir(folder):
        return {}
    out = {}
    for name in os.listdir(folder):
        m = ACTIVITY_FILE.match(name)
        if m:
            out[int(m.group(1))] = os.path.join(folder, name)
    return out


def rebuild(root=None, state=None):
    """Recompute index.json from the saved activities, keeping sync state (removed runs, times)."""
    old = _read(_dir("index.json", root=root), {}) if state is None else state
    removed = {str(k): v for k, v in (old.get("removed") or {}).items()}
    runs = []
    for aid, path in sorted(_activities(root).items()):
        try:
            row = summary(_read(path))
        except (ValueError, KeyError, TypeError) as e:
            print("  skipping unreadable run file %s: %s" % (path, e), flush=True)
            continue
        if str(aid) in removed:
            row["removed"] = removed[str(aid)]
        runs.append(row)
    runs.sort(key=lambda r: r["start"] or "", reverse=True)
    index = {"version": SUMMARY_VERSION, "since": old.get("since"), "synced_at": old.get("synced_at"),
             "last_sync": old.get("last_sync"), "removed": removed, "runs": runs}
    os.makedirs(_dir(root=root), exist_ok=True)
    _write_atomic(_dir("index.json", root=root), index)
    return index


def load(root=None):
    """The index, rebuilt first if it's missing or from an older summary format."""
    index = _read(_dir("index.json", root=root))
    if not index or index.get("version") != SUMMARY_VERSION:
        index = rebuild(root, state=index or {})
    return index


def _from_ts(since):
    """Unix time for Smashrun's fromDate: a day early, so no time zone can drop the first runs.
    Runs before `since` are filtered out by their local start date afterwards."""
    d = date.fromisoformat(since)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) - 86400


def list_since(client, since):
    rows, page = [], 0
    while True:
        batch = client._call("GET", "/my/activities/search?fromDate=%d&count=%d&page=%d"
                             % (_from_ts(since), PAGE, page)) or []
        rows += batch
        if len(batch) < PAGE:
            break
        page += 1
    return [r for r in rows if (r.get("startDateTimeLocal") or "")[:10] >= since]


def sync(client, since=None, root=None, step=None, log=print):
    """Bring the local copy up to date with Smashrun. Returns what changed."""
    since = since or SYNC_SINCE
    date.fromisoformat(since)   # fail early on a bad SYNC_SINCE
    step = step or (lambda s: None)
    os.makedirs(_dir("activities", root=root), exist_ok=True)
    state = _read(_dir("index.json", root=root), {})
    removed = {str(k): v for k, v in (state.get("removed") or {}).items()}

    step("Listing your runs on Smashrun")
    listed = {int(r["activityId"]): r for r in list_since(client, since)}
    saved = _activities(root)

    todo = []
    for aid, row in listed.items():
        if aid not in saved:
            todo.append((aid, "new"))
        else:
            have = _read(saved[aid], {})
            if have.get("dateUpdatedUTC") != row.get("dateUpdatedUTC"):
                todo.append((aid, "updated"))
    todo.sort(key=lambda x: listed[x[0]].get("startDateTimeLocal") or "", reverse=True)
    left = max(0, len(todo) - MAX_FETCH)
    todo = todo[:MAX_FETCH]

    added, updated = [], []
    for i, (aid, why) in enumerate(todo, 1):
        step("Fetching run %d of %d" % (i, len(todo)))
        activity = client.get(aid)
        _write_atomic(_dir("activities", "%d.json" % aid, root=root), activity)
        (added if why == "new" else updated).append(aid)
        removed.pop(str(aid), None)

    # Runs in the synced date range that Smashrun no longer has (deleted, or replaced under a
    # new ID). Their files stay; the stats leave them out.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    gone = []
    for aid, path in _activities(root).items():
        if aid in listed or str(aid) in removed:
            continue
        start = (_read(path, {}).get("startDateTimeLocal") or "")[:10]
        if start >= since:
            removed[str(aid)] = now
            gone.append(aid)
    for aid in listed:
        removed.pop(str(aid), None)

    result = {"since": since, "listed": len(listed), "new": added, "updated": updated,
              "removed": gone, "not_fetched": left}
    log("mirror sync: %d listed, %d new, %d updated, %d removed%s"
        % (len(listed), len(added), len(updated), len(gone), ", %d left for the next sync" % left if left else ""))
    rebuild(root, state={"since": since, "synced_at": now, "last_sync": result, "removed": removed})
    return result


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["sync", "rebuild"])
    ap.add_argument("--since", default=None, help="first date to copy (default SYNC_SINCE, %s)" % SYNC_SINCE)
    ap.add_argument("--dir", default=None, help="mirror folder (default MIRROR_DIR, %s)" % MIRROR_DIR)
    ap.add_argument("--token-file", default=os.environ.get("TOKEN_FILE", S.DEFAULT_TOKEN))
    args = ap.parse_args(argv)
    if args.command == "rebuild":
        index = rebuild(args.dir)
        print("rebuilt index: %d runs" % len(index["runs"]))
        return
    sync(S.Client(args.token_file), since=args.since, root=args.dir)


if __name__ == "__main__":
    main()
