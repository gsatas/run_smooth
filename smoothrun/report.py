"""Splits, summaries and diagnostic plots."""
import numpy as np

MILE = 1609.344


def fmt_pace(sec_per_unit):
    if not np.isfinite(sec_per_unit):
        return "  --  "
    m, s = divmod(int(round(sec_per_unit)), 60)
    return "%d:%02d" % (m, s)


def splits(t, dist, unit_m):
    """Time for each full unit of distance (interpolated), plus the partial last split."""
    total = dist[-1]
    marks = np.arange(unit_m, total, unit_m)
    tm = np.interp(marks, dist, t) if len(marks) else np.array([])
    edges_t = np.concatenate([[t[0]], tm, [t[-1]]])
    edges_d = np.concatenate([[0.0], marks, [total]])
    out = []
    for k in range(len(edges_t) - 1):
        dd = edges_d[k + 1] - edges_d[k]
        dtt = edges_t[k + 1] - edges_t[k]
        out.append((dd / unit_m, dtt, dtt / (dd / unit_m) if dd > 1 else np.nan))
    return out


def raw_distance(t, zx, zy):
    ok = np.isfinite(zx)
    return float(np.sum(np.hypot(np.diff(zx[ok]), np.diff(zy[ok]))))


def print_splits(t, columns, unit_m, unit_name):
    """columns: list of (label, cumulative distance array)."""
    tables = [(lab, splits(t, d, unit_m)) for lab, d in columns]
    nrow = max(len(s) for _, s in tables)
    print("  %-6s" % unit_name + "".join("%14s" % lab for lab, _ in tables))
    for r in range(nrow):
        line = "  %-6s" % (r + 1)
        for _, s in tables:
            if r < len(s):
                frac, _, pace = s[r]
                tag = "" if frac > 0.999 else "*"
                line += "%14s" % (fmt_pace(pace) + tag)
            else:
                line += "%14s" % ""
        print(line)
    print("  (* partial split, pace shown per full %s)" % unit_name)


def plot(path, track, res, res_nocad, title=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    zx, zy = res.info["zx"], res.info["zy"]
    t = track.t
    fig = plt.figure(figsize=(16, 11))
    gs = fig.add_gridspec(3, 2, width_ratios=[1, 1.4], height_ratios=[1.3, 1, 0.8])

    ax = fig.add_subplot(gs[:, 0])
    ax.plot(zx, zy, "-", color="0.75", lw=0.8, label="raw GPS")
    bad = res.gps_weight < 0.3
    ax.plot(zx[bad], zy[bad], ".", color="tab:red", ms=3, label="down-weighted GPS (<0.3)")
    if res_nocad is not None:
        ax.plot(res_nocad.x, res_nocad.y, "-", color="tab:orange", lw=1, label="smoothed, no cadence")
    ax.plot(res.x, res.y, "-", color="tab:blue", lw=1.4, label="smoothed")
    ax.set_aspect("equal")
    ax.set_xlabel("east (m)")
    ax.set_ylabel("north (m)")
    ax.legend(loc="best", fontsize=8)
    ax.set_title(title)

    tm = t / 60
    raw_v = np.full(len(t), np.nan)
    ok = np.isfinite(zx)
    gi = np.where(ok)[0]
    raw_v[gi[1:]] = np.hypot(np.diff(zx[gi]), np.diff(zy[gi])) / np.diff(t[gi])

    ax2 = fig.add_subplot(gs[0, 1])
    ax2.plot(tm, raw_v, color="0.8", lw=0.6, label="raw GPS point-to-point")
    ax2.plot(tm, track.speed, color="tab:green", lw=0.6, alpha=0.7, label="recorded (watch/Smashrun)")
    if res.stride is not None:
        cad_v = res.stride * track.cadence[1:] / 60
        ax2.plot(tm[1:], cad_v, color="tab:purple", lw=0.6, alpha=0.7, label="cadence x learned stride")
    if res_nocad is not None:
        ax2.plot(tm, res_nocad.point_speed, color="tab:orange", lw=1, label="smoothed, no cadence")
    ax2.plot(tm, res.point_speed, color="tab:blue", lw=1.3, label="smoothed")
    ax2.set_ylim(0, 6)
    ax2.set_ylabel("speed (m/s)")
    ax2.legend(loc="upper right", fontsize=7, ncol=2)
    ax2.grid(alpha=0.3)

    ax3 = fig.add_subplot(gs[1, 1], sharex=ax2)
    ax3.plot(tm, track.cadence, color="tab:purple", lw=0.7, label="cadence (strides/min)")
    ax3.set_ylabel("cadence")
    ax3.grid(alpha=0.3)
    if res.stride is not None:
        a = ax3.twinx()
        a.plot(tm[1:], res.stride, color="k", lw=1, label="learned stride (m)")
        a.set_ylabel("stride (m)")
        a.legend(loc="lower right", fontsize=7)
    ax3.legend(loc="lower left", fontsize=7)

    ax4 = fig.add_subplot(gs[2, 1], sharex=ax2)
    ax4.plot(tm, res.gps_weight, ".", ms=2, color="tab:red", label="GPS trust")
    if res.cad_weight is not None:
        ax4.plot(tm[1:], res.cad_weight, ".", ms=2, color="tab:purple", alpha=0.5, label="cadence trust")
    ax4.set_ylim(-0.05, 1.05)
    ax4.set_xlabel("time (min)")
    ax4.set_ylabel("robust weight")
    ax4.legend(loc="lower left", fontsize=7)
    ax4.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_map(path, net, res, m, title=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    fig, ax = plt.subplots(figsize=(10, 12))
    segs = np.stack([net.xy[net.ea], net.xy[net.eb]], 1)
    ax.add_collection(LineCollection(segs, colors="0.85", lw=0.5))
    ax.plot(res.info["zx"], res.info["zy"], "-", color="tab:red", lw=0.6, alpha=0.5, label="raw GPS")
    ax.plot(res.x, res.y, "-", color="tab:orange", lw=1, label="smoothed")
    ax.plot(m.route_xy[:, 0], m.route_xy[:, 1], "-", color="tab:blue", lw=1.6,
            label="matched route (%.0f m)" % m.length)
    pad = 150
    ax.set_xlim(res.x.min() - pad, res.x.max() + pad)
    ax.set_ylim(res.y.min() - pad, res.y.max() + pad)
    ax.set_aspect("equal")
    ax.set_title(title + "  (map data (c) OpenStreetMap contributors)")
    ax.legend(loc="best", fontsize=8)
    fig.savefig(path, dpi=100, bbox_inches="tight")
    plt.close(fig)
