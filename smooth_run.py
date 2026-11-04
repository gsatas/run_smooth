#!/usr/bin/env python3
"""Smooth a running TCX file and write a corrected TCX for re-upload.

    .venv/bin/python smooth_run.py example_files/40716350.TCX --map
    .venv/bin/python smooth_run.py run.tcx -o fixed.tcx --plot fixed.png --cadence

--map snaps the run onto the OpenStreetMap street/path network (fetched once per area via the
Overpass API, cached in cache/) and measures distance along the matched route.
"""
import argparse
import os

import numpy as np

from smoothrun.tcx import read_tcx, write_tcx
from smoothrun.smoother import Config, smooth, to_latlon
from smoothrun import report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="corrected TCX (default: output/<name>_smoothed.tcx)")
    ap.add_argument("--plot", help="diagnostic PNG (default: next to output)")
    ap.add_argument("--cadence", action="store_true",
                    help="also use cadence (experimental; off by default since it can be unreliable)")
    ap.add_argument("--map", action="store_true", help="match to the OSM street/path network")
    ap.add_argument("--units", choices=["mi", "km", "both"], default="both")
    args = ap.parse_args()

    base = os.path.splitext(os.path.basename(args.input))[0]
    out = args.output or os.path.join("output", base + "_smoothed.tcx")
    png = args.plot or os.path.splitext(out)[0] + ".png"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    tr = read_tcx(args.input)
    res_nocad = smooth(tr.t, tr.lat, tr.lon, None, Config(use_cadence=False))
    # With --map, cadence is used only along the route (where it validated well); the 2D
    # cadence smoother is the older experimental path, kept for --cadence without --map.
    use_2d_cad = args.cadence and not args.map
    res = smooth(tr.t, tr.lat, tr.lon, tr.cadence, Config()) if use_2d_cad else res_nocad

    zx, zy = res.info["zx"], res.info["zy"]
    raw = report.raw_distance(tr.t, zx, zy)
    recorded = float(np.nanmax(tr.dist))
    dur = tr.t[-1] - tr.t[0]
    print("%s  (%s, %d points, %d:%02d elapsed)" % (base, tr.start.strftime("%Y-%m-%d %H:%M"),
                                                     len(tr.t), dur // 60, dur % 60))
    rows = [("raw GPS", raw), ("recorded", recorded), ("smooth no-cad", res_nocad.dist[-1])]
    if use_2d_cad:
        rows.append(("smooth +cad", res.dist[-1]))
    mm = None
    if args.map:
        from smoothrun import osm, mapmatch
        net = osm.network_for_track(tr.lat, tr.lon, res.info["lat0"], res.info["lon0"])
        mm = mapmatch.match(net, tr.t, res.x, res.y, res.dist)
        from smoothrun import along
        ares, mx, my = along.run(mm, tr.t, zx, zy, res.info["v_max"],
                                 cadence=tr.cadence if args.cadence else None,
                                 track_xy=(res.x, res.y))
        mm = ares.match
        map_dist = ares.s - ares.s[0]
        map_speed = ares.speed
        mlat, mlon = to_latlon(mx, my, res.info["lat0"], res.info["lon0"])
        rows.append(("on route" + (" +cad" if ares.stride is not None else ""), map_dist[-1]))
        if ares.stride is not None:
            print("  (cadence used on %d%% of the run; stride %.2f m)" % (
                100 * np.mean(ares.cad_used), np.median(ares.stride)))
    for lab, d in rows:
        print("  %-14s %7.0f m  %5.2f mi" % (lab, d, d / report.MILE))
    print("  pace ceiling: %.2f m/s (%s /mi); iterations %d" % (res.info["v_max"], report.fmt_pace(report.MILE / res.info["v_max"]), res.iterations))
    print("  GPS points down-weighted (<0.3): %d of %d" % (np.sum(res.gps_weight < 0.3),
                                                         np.sum(np.isfinite(res.gps_weight))))
    if res.stride is not None:
        print("  learned stride: %.2f m (range %.2f-%.2f); cadence samples ignored (<0.3): %d" % (
            np.nanmedian(res.stride), np.nanmin(res.stride), np.nanmax(res.stride),
            np.nansum(res.cad_weight < 0.3)))

    rec_d = np.where(np.isfinite(tr.dist), tr.dist, np.nan)
    rec_d = np.maximum.accumulate(np.nan_to_num(rec_d, nan=0.0))
    cols = [("recorded", rec_d), ("smooth no-cad", res_nocad.dist)]
    if use_2d_cad:
        cols.append(("smooth +cad", res.dist))
    if mm is not None:
        cols.append(("on route" + (" +cad" if ares.stride is not None else ""), map_dist))
    units = [("mi", report.MILE), ("km", 1000.0)]
    for name, m in units:
        if args.units in (name, "both"):
            print()
            report.print_splits(tr.t, cols, m, name)

    if mm is not None:
        write_tcx(tr, mlat, mlon, map_dist, map_speed, out)
    else:
        write_tcx(tr, res.lat, res.lon, res.dist, res.point_speed, out)
    report.plot(png, tr, res, res_nocad if use_2d_cad else None, title=base)
    print("\n  wrote %s\n  wrote %s" % (out, png))
    if mm is not None:
        mpng = os.path.splitext(out)[0] + "_map.png"
        report.plot_map(mpng, net, res, mm, title=base)
        print("  wrote %s" % mpng)


if __name__ == "__main__":
    main()
