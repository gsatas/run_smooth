"""Checks smoothrun/mirror.py against a fake Smashrun: python test_mirror.py"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoothrun import mirror
def act(i, day, upd="u1", ext=""):
    return {"activityId": i, "startDateTimeLocal": "2026-%s T07:00:00-05:00".replace(" ", "") % day, "distance": 5.0,
            "duration": 1800.0, "dateUpdatedUTC": upd, "externalId": ext, "hasDetailsGPS": 1,
            "recordingKeys": ["clock"], "recordingValues": [[0, 1]]}
class Fake:
    def __init__(s, runs): s.runs, s.gets, s.calls = runs, [], []
    def _call(s, m, path):
        if path == "/my/userinfo": return {"userName": "runner"}
        s.calls.append(path); page = int(path.split("page=")[1])
        rows = sorted(s.runs.values(), key=lambda a: a["startDateTimeLocal"], reverse=True)
        return [{k: v for k, v in a.items() if not k.startswith("recording")} for a in rows][page*100:(page+1)*100]
    def get(s, aid): s.gets.append(aid); return s.runs[aid]
root = tempfile.mkdtemp()
runs = {1: act(1, "01-05"), 2: act(2, "11-03"), 9: {**act(9, "12-31"), "startDateTimeLocal": "2025-12-31T07:00:00-05:00"}}
f = Fake(runs); r = mirror.sync(f, "2026-01-01", root, log=lambda m: None)
assert r["new"] == [2, 1] and sorted(f.gets) == [1, 2], (r, f.gets)          # 2025 run filtered out
f.gets.clear(); r = mirror.sync(f, "2026-01-01", root, log=lambda m: None)
assert not f.gets and not r["new"] and not r["updated"], r                   # nothing to fetch
runs[2] = act(2, "11-03", upd="u2", ext="smoothrun-2-20261104T000000")      # replaced under same id
del runs[1]; runs[3] = act(3, "11-04")
f.gets.clear(); r = mirror.sync(f, "2026-01-01", root, log=lambda m: None)
assert r["updated"] == [2] and r["new"] == [3] and r["removed"] == [1], r
idx = mirror.load(root)
assert idx["user_name"] == "runner"
assert [x["id"] for x in idx["runs"]] == [3, 2, 1]
assert idx["runs"][1]["smoothed"] and idx["runs"][1]["smoothed_from"] == 2
assert "removed" in idx["runs"][2] and abs(idx["runs"][0]["pace_s_per_km"] - 360) < 1e-9
runs[1] = act(1, "01-05"); r = mirror.sync(f, "2026-01-01", root, log=lambda m: None)  # it came back
assert "removed" not in mirror.load(root)["runs"][2] and not mirror.load(root)["removed"]
for i in range(100, 260): runs[i] = act(i, "06-%02d" % (i % 28 + 1))        # paging + fetch cap
f.calls.clear(); r = mirror.sync(f, "2026-01-01", root, log=lambda m: None)
assert len(f.calls) == 2 and r["listed"] == 163 and len(r["new"]) == 100 and r["not_fetched"] == 60, r
json.dump({**json.load(open(os.path.join(root, "index.json"))), "version": 0}, open(os.path.join(root, "index.json"), "w"))
assert mirror.load(root)["version"] == mirror.SUMMARY_VERSION and len(mirror.load(root)["runs"]) == 103
# import: older runs from a folder; newer, already-saved and broken files are left alone
src = tempfile.mkdtemp()
old = {**act(500, "01-01"), "startDateTimeLocal": "2019-05-01T07:00:00-04:00"}
for name, a in (("500.json", old), ("1.json", {**act(1, "01-05"), "distance": 99.0}), ("2.json", {**runs[2], "activityId": 2}),
                ("600.json", {**act(600, "01-01"), "startDateTimeLocal": "2018-01-01T07:00:00-05:00", "activityId": 1})):
    json.dump(a, open(os.path.join(src, name), "w"))
open(os.path.join(src, "junk.json"), "w").write("{not json")
json.dump({**old, "activityId": 501, "recordingKeys": None, "recordingValues": None}, open(os.path.join(src, "501.json"), "w"))
c = mirror.import_folder(src, "2026-01-01", root, log=lambda m: None)
assert c == {"added": 2, "already": 1, "too_new": 2, "bad": 1}, c     # 501 is a manual entry, no recording     # 600.json is run 1 again: already saved
idx = mirror.load(root)
assert {x["id"] for x in idx["runs"][-2:]} == {500, 501} and "removed" not in idx["runs"][-1] and len(idx["runs"]) == 105
r = mirror.sync(f, "2026-01-01", root, log=lambda m: None)
assert 500 not in r["removed"] and "removed" not in mirror.load(root)["runs"][-1]   # syncs don't touch old runs
# swap: after a replace, an old run (outside the synced range) is retired and its new version saved
runs[700] = {**old, "activityId": 700, "distance": 4.9}
assert mirror.swap(f, 500, 700, root, log=lambda m: None)
idx = mirror.load(root)
by = {x["id"]: x for x in idx["runs"]}
assert "removed" in by[500] and "removed" not in by[700] and by[700]["distance_m"] == 4900
r = mirror.sync(f, "2026-01-01", root, log=lambda m: None)
assert "removed" in {x["id"]: x for x in mirror.load(root)["runs"]}[500]          # a sync keeps it retired
assert not mirror.swap(f, 500, None, root, log=lambda m: None)
print("all mirror tests passed")
