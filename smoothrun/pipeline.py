"""The full smoothing pipeline as one call, shared by the CLI and the web app."""
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from .smoother import Config, smooth, to_latlon


@dataclass
class Output:
    lat: np.ndarray
    lon: np.ndarray
    dist: np.ndarray        # cumulative metres from 0
    speed: np.ndarray       # m/s per sample
    smoothed: Any           # step-1 Result (2D)
    net: Any = None         # OSM network (when map matching ran)
    match: Any = None       # final route match (with any off-network splices)
    along: Any = None       # along-route result
    used_cadence: bool = False

    @property
    def distance(self):
        return float(self.dist[-1])


def run(t, lat, lon, cadence=None, use_map=True, use_cadence=True, cache_dir="cache"):
    """t: seconds; lat/lon: degrees (NaN where missing); cadence: strides/min or None."""
    res = smooth(t, lat, lon, None, Config(use_cadence=False))
    if not use_map:
        return Output(res.lat, res.lon, res.dist, res.point_speed, res)

    from . import osm, mapmatch, along
    net = osm.network_for_track(lat, lon, res.info["lat0"], res.info["lon0"], cache_dir=cache_dir)
    m = mapmatch.match(net, t, res.x, res.y, res.dist)
    cad = cadence if (use_cadence and cadence is not None and np.any(np.isfinite(cadence))) else None
    a, x, y = along.run(m, t, res.info["zx"], res.info["zy"], res.info["v_max"],
                        cadence=cad, track_xy=(res.x, res.y))
    mlat, mlon = to_latlon(x, y, res.info["lat0"], res.info["lon0"])
    return Output(mlat, mlon, a.s - a.s[0], a.speed, res, net, a.match, a,
                  used_cadence=a.stride is not None)
