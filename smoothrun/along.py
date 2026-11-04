"""Step 3: smooth the run as a 1D problem along the matched route.

Once the route is known, position is just s = distance along the route. We solve for s at
every sample with the same robust least-squares machinery as step 1:

  * GPS term      s_i ~ (GPS point projected onto the route); looser when the point is far
                  off the route sideways (sideways error itself can't add distance any more)
  * motion prior  along-route acceleration ~ 0 (Student-t; cut at recording gaps)
  * stop prior    Student-t on speed centred at 0 (standing at lights -> exactly 0)
  * constraints   no going backwards, pace ceiling (active-set penalties)
"""
from dataclasses import dataclass

import numpy as np

from .smoother import _Rows, _t_weight


@dataclass
class AlongConfig:
    gps_sigma: float = 5.0         # m, along-route GPS error when behaving
    gps_nu: float = 3.0
    lateral_k: float = 1.0         # sigma grows by this x the sideways offset from the route
    accel_sigma: float = 0.35      # m/s^2
    accel_nu: float = 3.0
    gap_s: float = 5.0
    stop_sigma: float = 0.15       # m/s
    stop_nu: float = 1.0
    stop_strength: float = 0.3
    max_speed: float = 6.0         # m/s ceiling (caller passes the runner-specific one)
    hard_sigma: float = 0.05       # m/s, stiffness of the monotone / ceiling penalties
    window: float = 150.0          # m; GPS is projected onto the route within +-window of s_init
    # Cadence (optional): along-route speed = stride x cadence.
    cad_sigma: float = 0.25        # m/s
    cad_nu: float = 4.0
    cad_band: float = 0.15         # use cadence within +-15% of your running median only
    cad_trust_iters: int = 4       # trusted (non-robust) iterations before robust reweighting
    stride_drift: float = 0.004    # m / sqrt(s)
    stride_level_sigma: float = 0.003  # m; pins average stride to the no-cadence solution
    stop_cadence_frac: float = 0.3 # cadence below this x median counts as "maybe stopped"
    iters: int = 30
    tol: float = 0.01


@dataclass
class AlongResult:
    s: np.ndarray            # distance along route at each sample
    speed: np.ndarray        # per-sample speed, m/s
    g: np.ndarray            # GPS projected along route (NaN where missing)
    lateral: np.ndarray      # sideways offset of GPS from the route
    gps_weight: np.ndarray
    iterations: int
    stride: np.ndarray = None      # per segment, when cadence was used
    cad_used: np.ndarray = None    # per segment: cadence passed the band check


def project(route_xy, route_s, px, py, s_init, window):
    """Project each point onto the route, only considering route within +-window of s_init."""
    a, b = route_xy[:-1], route_xy[1:]
    s0 = route_s[:-1]
    ab = b - a
    L = np.hypot(*ab.T)
    good = L > 1e-6
    g = np.full(len(px), np.nan)
    lat = np.full(len(px), np.nan)
    for i in range(len(px)):
        if not np.isfinite(px[i]):
            continue
        m = good & (route_s[1:] >= s_init[i] - window) & (s0 <= s_init[i] + window)
        if not m.any():
            continue
        aa, abm, Lm = a[m], ab[m], L[m]
        p = np.array([px[i], py[i]])
        tau = np.clip(((p - aa) * abm).sum(1) / Lm ** 2, 0, 1)
        q = aa + tau[:, None] * abm
        d = np.hypot(*(q - p).T)
        j = np.argmin(d)
        g[i] = s0[m][j] + tau[j] * Lm[j]
        lat[i] = d[j]
    return g, lat


def smooth_along(t, g, lateral, s_init, cfg=AlongConfig(), cadence=None):
    from dataclasses import replace
    from .smoother import gap_threshold
    cfg = replace(cfg, gap_s=gap_threshold(t, cfg.gap_s))
    n = len(t)
    dt = np.maximum(np.diff(t), 1e-3)
    nseg = n - 1
    have = np.isfinite(g)
    gi = np.where(have)[0]
    sig_g = np.sqrt(cfg.gps_sigma ** 2 + (cfg.lateral_k * np.nan_to_num(lateral[gi])) ** 2)

    i = np.arange(1, n - 1)
    d0, d1 = dt[:-1], dt[1:]
    h = 0.5 * (d0 + d1)
    a_vals = np.stack([1 / (d0 * h), -(1 / d0 + 1 / d1) / h, 1 / (d1 * h)], axis=1)
    a_scale = np.sqrt(h) / cfg.accel_sigma
    a_scale = np.where((d0 > cfg.gap_s) | (d1 > cfg.gap_s), a_scale * 1e-3, a_scale)
    no_gap = dt <= cfg.gap_s
    k = np.arange(nseg)
    two = np.stack([k, k + 1], axis=1)
    dv = np.stack([-1 / dt, 1 / dt], axis=1)

    use_cad = cadence is not None
    if use_cad:
        c = np.asarray(cadence, float)[1:] / 60.0          # strides/s for segment k -> k+1
        moving_c = c[np.isfinite(c) & (c > 0.2)]
        c_med = float(np.median(moving_c)) if len(moving_c) else np.nan
        cad_ok = no_gap & np.isfinite(c) & (np.abs(c - c_med) <= cfg.cad_band * c_med)
        if cad_ok.sum() < 10:
            use_cad = False
    if use_cad:
        # Stage 1: no-cadence solution sets the average stride.
        base = smooth_along(t, g, lateral, s_init, cfg, None)
        v0 = np.diff(base.s) / dt
        m_ref = cad_ok & (v0 > 1.0)
        if m_ref.sum() < 10:
            use_cad = False
    if use_cad:
        cd = c[m_ref] * dt[m_ref]
        s_ref = float(np.sum(v0[m_ref] * dt[m_ref]) / np.sum(cd))
        lvl_idx, lvl_w = np.where(m_ref)[0], cd / cd.sum()
        stop_gate = no_gap & (~np.isfinite(c) | (c < cfg.stop_cadence_frac * c_med))
        s_init = base.s
        stride = np.full(nseg, s_ref)
        ist = n
        nvar = n + nseg
        w_c = np.ones(nseg)
    else:
        stop_gate = no_gap
        nvar = n

    s = np.asarray(s_init, float).copy()
    w_g = np.ones(len(gi))
    w_a = np.ones(len(i))
    w_s = np.ones(nseg)
    it, change = 0, np.inf
    robust_from = cfg.cad_trust_iters if use_cad else 0
    for it in range(cfg.iters + robust_from):
        rows = _Rows()
        rows.add(np.c_[gi], np.ones((len(gi), 1)), g[gi], np.sqrt(w_g) / sig_g)
        rows.add(np.stack([i - 1, i, i + 1], axis=1), a_vals, 0.0, np.sqrt(w_a) * a_scale)
        sk = np.where(stop_gate)[0]
        if len(sk):
            rows.add(two[sk], dv[sk], 0.0, np.sqrt(cfg.stop_strength * w_s[sk] * dt[sk]) / cfg.stop_sigma)
        v = np.diff(s) / dt
        back = np.where(v < 0)[0]
        if len(back):
            rows.add(two[back], dv[back], 0.0, np.sqrt(dt[back]) / cfg.hard_sigma)
        fast = np.where(v > cfg.max_speed)[0]
        if len(fast):
            rows.add(two[fast], dv[fast], cfg.max_speed, np.sqrt(dt[fast]) / cfg.hard_sigma)
        if use_cad:
            ck = np.where(cad_ok)[0]
            # (s[k+1]-s[k])/dt - c_k * stride_k = 0
            rows.add(np.stack([ck, ck + 1, ist + ck], axis=1),
                     np.stack([-1 / dt[ck], 1 / dt[ck], -c[ck]], axis=1), 0.0,
                     np.sqrt(w_c[ck] * dt[ck]) / cfg.cad_sigma)
            rows.add(np.stack([ist + k[:-1], ist + k[1:]], axis=1), np.tile([-1.0, 1.0], (nseg - 1, 1)),
                     0.0, 1.0 / (cfg.stride_drift * np.sqrt(dt[1:])))
            rows.add(np.c_[ist + lvl_idx], np.ones((len(lvl_idx), 1)), s_ref,
                     np.sqrt(lvl_w) / cfg.stride_level_sigma)
        sol = rows.solve(nvar)
        new = sol[:n]
        if use_cad:
            stride = sol[ist:]
        change = float(np.max(np.abs(new - s)))
        s = new

        r = s[gi] - g[gi]
        w_g = _t_weight(r ** 2, sig_g, cfg.gps_nu, 1)
        acc = a_vals[:, 0] * s[i - 1] + a_vals[:, 1] * s[i] + a_vals[:, 2] * s[i + 1]
        w_a = _t_weight(acc ** 2, cfg.accel_sigma, cfg.accel_nu, 1)
        v = np.diff(s) / dt
        w_s = _t_weight(v ** 2, cfg.stop_sigma, cfg.stop_nu, 1) / ((cfg.stop_nu + 1) / cfg.stop_nu)
        if use_cad and it + 1 >= robust_from:
            w_c = _t_weight((v - c * stride) ** 2, cfg.cad_sigma, cfg.cad_nu, 1)
        if it >= robust_from + 3 and change < cfg.tol:
            break

    seg_v = np.diff(s) / dt
    speed = np.r_[seg_v[0], 0.5 * (seg_v[:-1] + seg_v[1:]), seg_v[-1]]
    wg = np.full(n, np.nan)
    wg[gi] = w_g / ((cfg.gps_nu + 1) / cfg.gps_nu)
    return AlongResult(s, speed, g, lateral, wg, it + 1,
                       stride if use_cad else None, cad_ok if use_cad else None)


def run(m, t, zx, zy, max_speed, cfg=None, cadence=None, track_xy=None):
    """Full step 3 from a map match: project raw GPS, solve, return (result, x, y).
    With cadence and the step-1 track, stretches where you clearly ran off the mapped network
    (cadence says the route is too short/long, the track agrees) use the track's geometry;
    the updated match is available as result.match."""
    from .mapmatch import place_on_route, splice_off_network, MatchConfig
    cfg = cfg or AlongConfig()
    cfg.max_speed = max_speed

    def solve(mm, cad):
        s_init, _, _ = place_on_route(mm, t)
        g, lat = project(mm.route_xy, mm.route_s, zx, zy, s_init, cfg.window)
        return smooth_along(t, g, lat, s_init, cfg, cad)

    res = solve(m, cadence)
    if cadence is not None and track_xy is not None and res.stride is not None:
        dt = np.maximum(np.diff(t), 1e-3)
        c = np.asarray(cadence, float)[1:] / 60.0
        ok = np.r_[False, res.cad_used]
        v = np.diff(res.s) / dt
        short = np.r_[0.0, np.where(res.cad_used, (res.stride * c - v) * dt, 0.0)]
        m2 = splice_off_network(m, t, track_xy[0], track_xy[1], np.cumsum(short), ok, MatchConfig())
        if m2 is not m:
            m = m2
            res = solve(m, cadence)
    res.match = m
    x = np.interp(res.s, m.route_s, m.route_xy[:, 0])
    y = np.interp(res.s, m.route_s, m.route_xy[:, 1])
    return res, x, y
