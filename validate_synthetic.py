#!/usr/bin/env python3
"""Check the smoother against simulated Manhattan-style runs where the true distance is known.

Each trial: a grid route (274 m x 80 m blocks, 90-degree corners), ~2.4 m/s with pace drift,
stops at some corners, 1 Hz GPS with white noise + slowly wandering correlated error +
occasional along-street drift episodes (like urban-canyon multipath), and cadence with noise,
occasional dropouts, and a slowly changing stride.

    .venv/bin/python validate_synthetic.py [--trials 20] [--cad-glitch 0.05]
"""
import argparse

import numpy as np

from smoothrun.smoother import Config, smooth, to_latlon

LAT0, LON0 = 40.78, -73.97


def simulate(rng, cad_glitch=0.03, duration=2100, run_walk=False):
    # Route: random walk on a Manhattan grid.
    pts = [np.zeros(2)]
    heading = rng.integers(4)
    total = 0.0
    while total < duration * 3.2:
        heading = (heading + rng.choice([-1, 0, 0, 1])) % 4
        d = 274.0 if heading % 2 == 0 else 80.0
        d *= rng.integers(1, 4)
        step = np.array([[1, 0], [0, 1], [-1, 0], [0, -1]][heading]) * d
        pts.append(pts[-1] + step)
        total += d
    pts = np.array(pts)
    seg = np.hypot(*np.diff(pts, axis=0).T)
    cum = np.concatenate([[0], np.cumsum(seg)])

    # Speed profile: slow pace drift, stops at ~30% of corners.
    t = np.arange(duration, dtype=float)
    base = 2.4 + 0.25 * np.sin(2 * np.pi * t / rng.uniform(400, 900) + rng.uniform(0, 6))
    walking = np.zeros(duration, bool)
    if run_walk:  # last third: run 4 min / walk 1 min, walking ~1.4 m/s
        k0 = int(duration * 2 / 3)
        walking[k0:] = ((t[k0:] - t[k0]) % 300) >= 240
        base = np.where(walking, 1.4 + 0.1 * rng.standard_normal(duration).cumsum() * 0, base)
    stop_at = set(cum[1:-1][rng.random(len(cum) - 2) < 0.3])
    s, v = 0.0, np.zeros(duration)
    stopped_until = -1.0
    paused = np.zeros(duration, bool)   # watch auto-paused (no samples recorded)
    pos_s = np.zeros(duration)
    pending = sorted(stop_at)
    for k in range(duration):
        if k < stopped_until:
            v[k] = 0.0
        else:
            v[k] = base[k]
            if pending and s + v[k] >= pending[0]:
                v[k] = pending[0] - s
                stopped_until = k + rng.uniform(8, 40)
                if rng.random() < 0.3:   # auto-pause is usually off; sometimes it catches a stop
                    paused[k + 2:int(stopped_until)] = True
                pending.pop(0)
        s += v[k]
        pos_s[k] = s
    # Unreliable auto-pause also drops samples while you're running.
    for _ in range(rng.integers(0, 3)):
        k0 = rng.integers(60, duration - 120)
        paused[k0:k0 + rng.integers(10, 60)] = True
    pos_s = np.concatenate([[0], pos_s[:-1]])
    pos_s = np.minimum(pos_s, cum[-1])
    true_x = np.interp(pos_s, cum, pts[:, 0])
    true_y = np.interp(pos_s, cum, pts[:, 1])
    true_dist = pos_s[-1]

    # GPS error: white + AR(1) wander + along-street drift episodes.
    # (Calibrated so raw 1 s vs 10 s path length ratio ~1.06, matching real watch output.)
    white = rng.normal(0, 0.4, (duration, 2))
    ar = np.zeros((duration, 2))
    a = np.exp(-1 / 60.0)
    for k in range(1, duration):
        ar[k] = a * ar[k - 1] + rng.normal(0, 4.0 * np.sqrt(1 - a * a), 2)
    kern = np.ones(8) / 8  # watch output is itself filtered, so the wander is smooth
    ar = np.stack([np.convolve(ar[:, j], kern, mode="same") for j in range(2)], axis=1)
    drift = np.zeros((duration, 2))
    for _ in range(rng.integers(2, 6)):
        k0, L = rng.integers(0, duration - 80), rng.integers(20, 60)
        amp = rng.uniform(20, 70)
        k1 = min(k0 + L, duration - 1)
        dirv = np.array([true_x[k1] - true_x[k0], true_y[k1] - true_y[k0]])
        dirv = dirv / (np.linalg.norm(dirv) + 1e-9)
        shape = np.sin(np.linspace(0, np.pi, k1 - k0))  # lead then catch up
        drift[k0:k1] += amp * shape[:, None] * dirv
    gx = true_x + white[:, 0] + ar[:, 0] + drift[:, 0]
    gy = true_y + white[:, 1] + ar[:, 1] + drift[:, 1]
    lat, lon = to_latlon(gx, gy, LAT0, LON0)

    # Cadence: strides/min = speed / stride; stride drifts; noise, quantised; walking/stopped -> low.
    stride = 1.95 + 0.1 * np.sin(2 * np.pi * t / rng.uniform(600, 1500))
    stride = np.where(walking, 1.45, stride)   # walking: shorter stride, ~58 strides/min
    cad = np.where(v > 0.5, v / stride * 60, rng.uniform(0, 20, duration))
    cad = np.round(cad + rng.normal(0, 1.0, duration))
    glitch = rng.random(duration) < cad_glitch
    cad[glitch] = rng.uniform(20, 55, glitch.sum())
    for _ in range(rng.integers(0, 4)):   # sustained wonky stretches, as seen in real data
        k0 = rng.integers(0, duration - 60)
        cad[k0:k0 + rng.integers(15, 45)] = rng.uniform(32, 46)
    cad[0] = 0
    keep = ~paused
    return (t[keep], lat[keep], lon[keep], cad[keep], true_dist,
            (true_x[keep], true_y[keep], pos_s[keep]))


def grid_network(tx, ty, lat0, lon0, margin=3):
    """A fake OSM street grid (274 m x 80 m blocks) around the simulated route, in the same
    Overpass JSON shape the real pipeline consumes."""
    from smoothrun import osm
    i0, i1 = int(np.floor(tx.min() / 274)) - margin, int(np.ceil(tx.max() / 274)) + margin
    j0, j1 = int(np.floor(ty.min() / 80)) - margin, int(np.ceil(ty.max() / 80)) + margin
    nid = lambda i, j: (i - i0) * 100000 + (j - j0) + 1
    els = []
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            la, lo = to_latlon(np.array([i * 274.0]), np.array([j * 80.0]), LAT0, LON0)
            els.append({"type": "node", "id": nid(i, j), "lat": float(la[0]), "lon": float(lo[0])})
    wid = 1
    for i in range(i0, i1 + 1):
        els.append({"type": "way", "id": wid, "nodes": [nid(i, j) for j in range(j0, j1 + 1)],
                    "tags": {"highway": "secondary", "name": "Avenue %d" % i}})
        wid += 1
    for j in range(j0, j1 + 1):
        els.append({"type": "way", "id": wid, "nodes": [nid(i, j) for i in range(i0, i1 + 1)],
                    "tags": {"highway": "residential", "name": "Street %d" % j}})
        wid += 1
    return osm.build({"elements": els}, lat0, lon0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--cad-glitch", type=float, default=0.03)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--cadence", action="store_true", help="also evaluate the 2D cadence smoother")
    ap.add_argument("--no-map", action="store_true", help="skip map matching / along-route steps")
    ap.add_argument("--run-walk", action="store_true", help="add run/walk intervals in the last third")
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    from smoothrun.smoother import to_local
    from smoothrun import mapmatch, along

    errs, split_err, pace_err = {}, {}, {}
    for _ in range(args.trials):
        t, lat, lon, cad, true_d, (tx, ty, true_s) = simulate(rng, args.cad_glitch, run_walk=args.run_walk)
        r0 = smooth(t, lat, lon, None, Config(use_cadence=False))
        zx, zy = r0.info["zx"], r0.info["zy"]
        methods = {"raw": np.concatenate([[0], np.cumsum(np.hypot(np.diff(zx), np.diff(zy)))]),
                   "smoothed": r0.dist}
        if args.cadence:
            methods["smoothed+cad"] = smooth(t, lat, lon, cad, Config()).dist
        if not args.no_map:
            tlat, tlon = to_latlon(tx, ty, LAT0, LON0)
            tx2, ty2 = to_local(tlat, tlon, r0.info["lat0"], r0.info["lon0"])
            net = grid_network(tx, ty, r0.info["lat0"], r0.info["lon0"])
            m = mapmatch.match(net, t, r0.x, r0.y, r0.dist)
            s_map, _, _ = mapmatch.place_on_route(m, t)
            methods["map"] = s_map - s_map[0]
            res, _, _ = along.run(m, t, zx, zy, r0.info["v_max"])
            methods["along-route"] = res.s - res.s[0]
            rc, _, _ = along.run(m, t, zx, zy, r0.info["v_max"], cadence=cad, track_xy=(r0.x, r0.y))
            methods["along+cad"] = rc.s - rc.s[0]
        marks = np.arange(1609.344, true_d, 1609.344)
        t_true = np.interp(marks, true_s, t)
        # 30 s average pace error (m/s), on a regular time grid
        tg = np.arange(t[0], t[-1] - 30, 5.0)
        true_v30 = (np.interp(tg + 30, t, true_s) - np.interp(tg, t, true_s)) / 30
        for k, d in methods.items():
            errs.setdefault(k, []).append(100 * (d[-1] - true_d) / true_d)
            v30 = (np.interp(tg + 30, t, d) - np.interp(tg, t, d)) / 30
            pace_err.setdefault(k, []).extend(np.abs(v30 - true_v30))
            mk = marks[marks < d[-1]]
            t_est = np.interp(mk, d, t)
            se = np.abs(np.diff(np.r_[0, t_est]) - np.diff(np.r_[0, t_true[:len(mk)]]))
            split_err.setdefault(k, []).extend(se)

    print("over %d simulated runs:" % args.trials)
    print("  %-13s %21s %22s %22s" % ("", "distance error", "mile-split error (s)",
                                       "30s pace error (m/s)"))
    print("  %-13s %7s %6s %7s %8s %8s %10s %8s" % ("method", "mean", "sd", "worst", "median", "90th",
                                                  "median", "95th"))
    for k, v in errs.items():
        v = np.array(v)
        se = np.array(split_err[k])
        pe = np.array(pace_err[k])
        print("  %-13s %+6.1f%% %5.1f%% %+6.1f%% %8.1f %8.1f %10.2f %8.2f" % (
            k, v.mean(), v.std(), v[np.argmax(np.abs(v))], np.median(se), np.percentile(se, 90),
            np.median(pe), np.percentile(pe, 95)))


if __name__ == "__main__":
    main()
