"""Checks smoothrun/places.py with made-up routes: python test_places.py

The bridge and Reservoir checks need cache/mapstore.sqlite and are skipped without it."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoothrun import places as P  # noqa: E402


def route(*pts, step=5.0):
    """A run through these (lat, lon) points: arrays of lat, lon and cumulative metres."""
    xy = P._densify(P._xy([p[0] for p in pts], [p[1] for p in pts]), step)
    d = np.r_[0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    return xy[:, 1] / P.KY + P.LAT0, xy[:, 0] / P.KX - 74.0, d


def go(*pts):
    return P.places(*route(*pts))


def cp(a, b):
    """A point in Central Park's frame (a: 59th → 110th, b: 5th Ave → CPW)."""
    o, n, w = (np.array(p) for p in (P.CP_SE, P.CP_NE, P.CP_SW))
    return tuple(o + a * (n - o) + b * (w - o))


# --- Central Park: the full loop goes round and reaches both ends; the lower loop doesn't count
full = go(cp(.03, .3), cp(.5, .2), cp(.97, .3), cp(.97, .7), cp(.5, .8), cp(.03, .7), cp(.03, .3))
assert full["cp_loop"] and full["central_park_m"] > 8000 and full["boroughs"] == ["Manhattan"], full
lower = go(cp(.03, .3), cp(.25, .2), cp(.25, .8), cp(.03, .7), cp(.03, .3))
assert not lower["cp_loop"] and lower["central_park_m"] > 2000
north = go(cp(.97, .3), cp(.5, .2), cp(.5, .8), cp(.97, .7), cp(.97, .3))   # the 102nd St-style loop
assert not north["cp_loop"]
fifth = go((40.7500, -73.9840), (40.7585, -73.9780))                           # 5th Ave, 42nd to 57th
assert fifth["central_park_m"] == 0 and fifth["waterfront_m"] == 0 and not fifth["times_square"]

# --- Times Square
assert go((40.7540, -73.9890), (40.7600, -73.9840))["times_square"]

# --- boroughs and the waterfront
greenway = go((40.7200, -74.0125), (40.7350, -74.0110), (40.7500, -74.0075))   # Hudson River Greenway
assert greenway["waterfront_m"] >= 1609 and greenway["boroughs"] == ["Manhattan"], greenway
inland = go((40.8700, -73.8650), (40.8950, -73.8600))                           # Bronx, up to Westchester
assert inland["waterfront_m"] < 300 and inland["boroughs"] == ["Bronx"], inland
nassau = go((40.7300, -73.7100), (40.7550, -73.7200))                           # Queens along the Nassau line
assert nassau["waterfront_m"] < 300 and "Queens" in nassau["boroughs"], nassau
assert go((40.6550, -73.9500), (40.6750, -73.9700))["boroughs"] == ["Brooklyn"]

if os.path.exists(os.path.join(P.CACHE_DIR, "mapstore.sqlite")):
    # --- the Reservoir: one lap of its running track counts, half a lap doesn't
    res = P._reservoir()
    order = np.argsort(np.arctan2(res[:, 1] - res[:, 1].mean(), res[:, 0] - res[:, 0].mean()))
    lap = res[order]
    lap = np.vstack([lap, lap[:1]])
    lat, lon = lap[:, 1] / P.KY + P.LAT0, lap[:, 0] / P.KX - 74.0
    got = go(*zip(lat, lon))
    assert got["reservoir_loop"] and not got["cp_loop"], got
    half = go(*list(zip(lat, lon))[: len(lat) // 2])
    assert not half["reservoir_loop"]

    # --- Brooklyn Bridge: end to end crosses it; halfway and back doesn't
    _, a, b = P._bridges()["Brooklyn Bridge"]
    ll = lambda p: (p[1] / P.KY + P.LAT0, p[0] / P.KX - 74.0)
    over = go(ll(a - (b - a) * .1), ll(a), ll(b), ll(b + (b - a) * .1))
    assert over["bridges"] == ["Brooklyn Bridge"] and set(over["boroughs"]) == {"Brooklyn", "Manhattan"}, over
    back = go(ll(a - (b - a) * .1), ll(a), ll(a + (b - a) * .5), ll(a))
    assert back["bridges"] == [], back
else:
    print("no mapstore: skipped the Reservoir and bridge checks")

print("all place tests passed")
