"""New York places a run passes through, for the place achievements (smoothrun/achievements.py).

Two sources, both local:
  * achievements/places.json: borough outlines clipped to the shoreline, plus Westchester and
    Nassau (built by achievements/build_places.py). Outside every one of them is water.
  * cache/mapstore.sqlite, the street map the smoother uses: the bridges and the Reservoir's
    running track, found by name.

Central Park and Times Square are fixed shapes given here.
"""
import json
import math
import os
import sqlite3

import numpy as np
from matplotlib.path import Path
from scipy.spatial import cKDTree

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACES_FILE = os.path.join(HERE, "achievements", "places.json")
CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(HERE, "cache"))
PLACES_VERSION = 1

STEP = 20.0            # metres between resampled points along the run
MIN_BOROUGH_M = 200    # this much of a run in a borough counts as running there
SHORE_M = 150          # within this of open water is the waterfront
LAT0 = 40.75
KX, KY = 111320.0 * math.cos(math.radians(LAT0)), 110950.0

# Central Park's corners: 59th & 5th, 110th & 5th, 59th & Central Park West.
CP_SE, CP_NE, CP_SW = (40.76442, -73.97301), (40.79693, -73.94929), (40.76803, -73.98185)
CP_BUFFER_M = 40       # the sidewalk just outside the wall counts
# Times Square: Broadway and 7th Avenue from 42nd to 47th Street.
TIMES_SQ = ((40.75627, -73.98689), (40.75902, -73.98464))
TIMES_SQ_M = 50
RESERVOIR_TRACK = "Stephanie and Fred Shuman Reservoir Running Track"
BRIDGE_MIN_M = 300     # leaves out the small park and footbridges
BRIDGE_NEAR_M = 60
NYC_BOX = (40.49, -74.26, 40.92, -73.70)


def _xy(lat, lon):
    return np.c_[(np.asarray(lon) - (-74.0)) * KX, (np.asarray(lat) - LAT0) * KY]


# ---------------------------------------------------------------- outlines

class _Area:
    """Rings with an even-odd rule, so holes work."""

    def __init__(self, rings):
        self.paths = [Path(np.array(r)) for r in rings]
        self.boxes = np.array([[p.vertices[:, 0].min(), p.vertices[:, 1].min(),
                                p.vertices[:, 0].max(), p.vertices[:, 1].max()] for p in self.paths])

    def contains(self, lon, lat):
        pts = np.c_[lon, lat]
        inside = np.zeros(len(pts), bool)
        for p, (x0, y0, x1, y1) in zip(self.paths, self.boxes):
            m = (pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1)
            if m.any():
                inside[m] ^= p.contains_points(pts[m])
        return inside


_cache = {}


def _outlines():
    if "areas" not in _cache:
        with open(PLACES_FILE) as fh:
            doc = json.load(fh)
        _cache["areas"] = ({k: _Area(v) for k, v in doc["boroughs"].items()},
                           [_Area(v) for v in doc["neighbors"].values()])
    return _cache["areas"]


# ---------------------------------------------------------------- the street map

def _ways(names=None, bridges=False):
    path = os.path.join(CACHE_DIR, "mapstore.sqlite")
    if not os.path.exists(path):
        return []
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        rows = db.execute("SELECT w.tags, w.lat, w.lon FROM ways_idx i JOIN ways w ON w.id = i.id "
                          "WHERE i.maxlat >= ? AND i.minlat <= ? AND i.maxlon >= ? AND i.minlon <= ?",
                          (NYC_BOX[0], NYC_BOX[2], NYC_BOX[1], NYC_BOX[3])).fetchall()
    finally:
        db.close()
    out = []
    for tags, la, lo in rows:
        t = json.loads(tags)
        name = t.get("name") or ""
        if names is not None and name not in names:
            continue
        if bridges and not (t.get("bridge") not in (None, "no") and "Bridge" in name):
            continue
        out.append((name, np.frombuffer(la, dtype=np.int32) / 1e7, np.frombuffer(lo, dtype=np.int32) / 1e7))
    return out


def _densify(xy, step=10.0):
    out = [xy[:1]]
    for a, b in zip(xy[:-1], xy[1:]):
        n = max(1, int(np.hypot(*(b - a)) // step))
        out.append(a + (b - a) * (np.arange(1, n + 1) / n)[:, None])
    return np.vstack(out)


def bridge_name(name):
    for tail in (" Upper Level", " Lower Level", " Promenade", " Bicycle Path", " Bike Path", " Footpath"):
        name = name.replace(tail, "")
    return name.replace("3rd Avenue Bridge", "Third Avenue Bridge")


def _bridges():
    """{name: (points, end A, end B)} for New York's bridges of 300 m or more."""
    if "bridges" not in _cache:
        parts = {}
        for name, la, lo in _ways(bridges=True):
            if len(la) >= 2:
                parts.setdefault(bridge_name(name), []).append(_densify(_xy(la, lo)))
        out = {}
        for name, segs in parts.items():
            pts = np.vstack(segs)
            length = sum(float(np.hypot(*np.diff(s, axis=0).T).sum()) for s in segs)
            lon = pts[:, 0].mean() / KX - 74.0
            lat = pts[:, 1].mean() / KY + LAT0
            # New Jersey's bridges are west of the Hudson, apart from Staten Island's.
            if length < BRIDGE_MIN_M or (lon < -74.03 and lat > 40.65):
                continue
            hull = pts[::max(1, len(pts) // 400)]
            dd = np.hypot(hull[:, None, 0] - hull[None, :, 0], hull[:, None, 1] - hull[None, :, 1])
            i, j = np.unravel_index(np.argmax(dd), dd.shape)
            out[name] = (cKDTree(pts), hull[i], hull[j])
        _cache["bridges"] = out
    return _cache["bridges"]


def _reservoir():
    if "reservoir" not in _cache:
        segs = [_densify(_xy(la, lo)) for _, la, lo in _ways(names={RESERVOIR_TRACK}) if len(la) >= 2]
        _cache["reservoir"] = np.vstack(segs) if segs else None
    return _cache["reservoir"]


# ---------------------------------------------------------------- per run

def _resample(lat, lon, d):
    ok = np.isfinite(lat) & np.isfinite(lon)
    if ok.sum() < 2:
        return None
    d, lat, lon = d[ok], lat[ok], lon[ok]
    grid = np.arange(0, d[-1] + 1e-9, STEP)
    return np.interp(grid, d, lat), np.interp(grid, d, lon)


def _central_park(xy):
    """(a, b) of each point in the park's own frame: a runs 0 at 59th to 1 at 110th, b 0 at 5th
    Avenue to 1 at Central Park West."""
    o, n, w = _xy(*CP_SE)[0], _xy(*CP_NE)[0], _xy(*CP_SW)[0]
    m = np.c_[n - o, w - o]
    ab = np.linalg.solve(m, (xy - o).T).T
    return ab, np.hypot(*(n - o)), np.hypot(*(w - o))


def _winding(ab, inside):
    """Turns around the park's centre, counting only steps made inside it."""
    ang = np.arctan2(ab[:, 1] - 0.5, (ab[:, 0] - 0.5) * 4)   # the park is about 4 times longer than wide
    step = np.diff(ang)
    step = (step + np.pi) % (2 * np.pi) - np.pi
    return abs(float(step[inside[:-1] & inside[1:]].sum())) / (2 * np.pi)


def _longest(flags, gap=5):
    """Longest stretch of True, bridging gaps of up to `gap` points."""
    best = run = miss = 0
    for f in flags:
        if f:
            run += miss + 1
            miss = 0
        elif run:
            miss += 1
            if miss > gap:
                run = miss = 0
        best = max(best, run)
    return best


def places(lat, lon, d):
    """Facts about where a run went, from its GPS track (lat, lon arrays and cumulative metres)."""
    got = _resample(np.asarray(lat, float), np.asarray(lon, float), np.asarray(d, float))
    if got is None:
        return None
    la, lo = got
    xy = _xy(la, lo)
    boroughs, neighbors = _outlines()

    where = np.full(len(la), "", dtype=object)
    for name, area in boroughs.items():
        where[area.contains(lo, la)] = name
    metres = {name: float((where == name).sum() * STEP) for name in boroughs}
    visited = sorted(k for k, v in metres.items() if v >= MIN_BOROUGH_M)
    start = next((w for w in where[:10] if w), None)

    # Waterfront: a point is on it when, looking 150 m in some direction, there's open water
    # (outside every borough and the neighbouring counties) and the point itself is in the city.
    ang = np.linspace(0, 2 * np.pi, 12, endpoint=False)
    plo = (lo[:, None] + SHORE_M * np.cos(ang)[None, :] / KX).ravel()
    pla = (la[:, None] + SHORE_M * np.sin(ang)[None, :] / KY).ravel()
    land = np.zeros(len(plo), bool)
    for area in list(boroughs.values()) + neighbors:
        land |= area.contains(plo, pla)
    in_city = (where != "") | land.reshape(len(la), -1).any(axis=1)
    shore = in_city & (~land).reshape(len(la), -1).any(axis=1)
    if not (where != "").any():
        shore[:] = False   # nowhere near New York

    ab, L, W = _central_park(xy)
    pad_a, pad_b = CP_BUFFER_M / L, CP_BUFFER_M / W
    in_cp = (ab[:, 0] >= -pad_a) & (ab[:, 0] <= 1 + pad_a) & (ab[:, 1] >= -pad_b) & (ab[:, 1] <= 1 + pad_b)
    cp_loop = bool(in_cp.any() and _winding(ab, in_cp) >= 0.95
                   and ab[in_cp, 0].min() <= 0.08 and ab[in_cp, 0].max() >= 0.92)

    tree = cKDTree(xy)
    res = _reservoir()
    reservoir_loop = bool(res is not None and in_cp.any()
                          and np.mean(tree.query(res, distance_upper_bound=30)[0] <= 30) >= 0.9)

    a, b = _xy(*TIMES_SQ[0])[0], _xy(*TIMES_SQ[1])[0]
    t = np.clip(((xy - a) @ (b - a)) / ((b - a) @ (b - a)), 0, 1)
    times_square = bool((np.hypot(*(xy - (a + t[:, None] * (b - a))).T) <= TIMES_SQ_M).any())

    crossed = []
    for name, (btree, end_a, end_b) in _bridges().items():
        near = btree.query(xy, distance_upper_bound=BRIDGE_NEAR_M)[0] <= BRIDGE_NEAR_M
        if not near.any():
            continue
        da, db = np.hypot(*(xy - end_a).T), np.hypot(*(xy - end_b).T)
        i = 0
        while i < len(near):   # each stretch on the bridge: did it reach both ends?
            if not near[i]:
                i += 1
                continue
            j = i
            while j < len(near) and near[j]:
                j += 1
            if da[i:j].min() <= BRIDGE_NEAR_M and db[i:j].min() <= BRIDGE_NEAR_M:
                crossed.append(name)
                break
            i = j
    return {"v": PLACES_VERSION, "boroughs": visited, "borough_m": {k: round(v) for k, v in metres.items() if v},
            "start_borough": start, "waterfront_m": float(_longest(shore) * STEP),
            "central_park_m": float(in_cp.sum() * STEP), "cp_loop": cp_loop, "reservoir_loop": reservoir_loop,
            "times_square": times_square, "bridges": sorted(crossed)}
