"""Robust running-track smoother.

We solve for the whole path at once (offline, using past and future samples) as a
sparse least-squares problem, iteratively reweighted so that bad data gets
down-weighted instead of dragging the answer around:

  * GPS term        position_i ~ gps_i                      (Student-t: multipath jumps are outliers)
  * motion prior    acceleration ~ 0                         (Student-t: allows corners and stops at lights)
  * cadence term    along-track speed ~ stride_i * cadence_i (Student-t: wonky cadence gets ignored)
  * stride prior    stride drifts slowly over the run

The cadence and GPS complement each other: GPS fixes the long-run scale
(so the stride length is learned from GPS, not assumed), while cadence
fixes the short-term shape of the pace (which GPS gets wrong in urban canyons).
"""
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import spsolve

R_EARTH = 6371008.8


@dataclass
class Config:
    gps_sigma: float = 4.0         # m, typical GPS error when it's behaving
    gps_nu: float = 3.0            # Student-t dof; lower = more willing to call something an outlier
    accel_sigma: float = 0.35      # m/s^2, how hard a runner's velocity changes when cruising
    accel_nu: float = 3.0
    use_cadence: bool = True
    cad_sigma: float = 0.25        # m/s, speed error from cadence * stride
    cad_nu: float = 4.0
    cad_band: float = 0.15         # cadence within ±15% of your running median counts as "running"
    cad_trust_iters: int = 4       # iterations where valid cadence is trusted before robust reweighting
    max_speed: Optional[float] = None   # m/s ceiling; None = max_speed_factor x median moving speed
    max_speed_factor: float = 1.8
    max_speed_sigma: float = 0.05  # how hard the ceiling is (m/s)
    # Stopped state: a prior that snaps slow stretches to exactly zero speed (standing at a light,
    # GPS wandering) but is ~flat at running speed. Applied where cadence says "not running"
    # (or there's no cadence), never across recording gaps (auto-pause can fire mid-run).
    stop_sigma: float = 0.15       # m/s
    stop_nu: float = 1.0
    stop_strength: float = 3.0
    stop_strength_no_cadence: float = 0.3
    stop_cadence_frac: float = 0.3  # cadence below 30% of your running median counts as "maybe stopped"   # weaker when we can't gate it with cadence
    stride_drift: float = 0.004    # m / sqrt(s): stride may wander ~3 cm per minute
    stride_level_sigma: float = 0.003  # m; how tightly the run's average stride is pinned to the
                                       # value implied by the GPS-only smoothed path
    min_speed_for_dir: float = 0.5    # m/s; below this the travel direction is unknown
    heading_window: float = 6.0    # s each side; cadence acts along this smoothed heading only
    heading_update: bool = False   # re-estimate the heading from the current path every iteration
    max_cad_gap: float = 5.0       # s; don't use cadence across recording gaps
    iters: int = 25
    tol: float = 0.01              # m


@dataclass
class Result:
    x: np.ndarray                 # local east, m
    y: np.ndarray                 # local north, m
    lat: np.ndarray
    lon: np.ndarray
    seg_speed: np.ndarray         # speed on segment i -> i+1, m/s
    point_speed: np.ndarray       # speed at each point, m/s
    dist: np.ndarray              # cumulative distance at each point, m
    stride: Optional[np.ndarray]  # m per stride on each segment (None without cadence)
    gps_weight: np.ndarray        # final robust weight per point (1 = trusted, ~0 = outlier)
    cad_weight: Optional[np.ndarray]
    iterations: int
    info: dict = field(default_factory=dict)


def gap_threshold(t, floor=5.0, factor=3.0):
    """A recording gap is an interval much longer than this file's normal sampling interval
    (phones log every 1 s; older watches with 'smart recording' every ~8 s)."""
    dt = np.diff(np.asarray(t, float))
    dt = dt[dt > 0]
    return max(floor, factor * float(np.median(dt))) if len(dt) else floor


def to_local(lat, lon, lat0, lon0):
    x = np.radians(lon - lon0) * R_EARTH * np.cos(np.radians(lat0))
    y = np.radians(lat - lat0) * R_EARTH
    return x, y


def to_latlon(x, y, lat0, lon0):
    lat = lat0 + np.degrees(y / R_EARTH)
    lon = lon0 + np.degrees(x / (R_EARTH * np.cos(np.radians(lat0))))
    return lat, lon


def _t_weight(r2, sigma, nu, dim):
    """Student-t IRLS weight for squared residual norm r2."""
    return (nu + dim) / (nu + r2 / sigma ** 2)


class _Rows:
    """Accumulates weighted sparse least-squares rows: w * (A x - b)."""

    def __init__(self):
        self.r, self.c, self.v, self.b, self.w = [], [], [], [], []
        self.n = 0

    def add(self, cols, vals, rhs, w):
        """cols/vals: (k, m) arrays for k rows with m nonzeros each."""
        cols, vals = np.atleast_2d(cols), np.atleast_2d(vals)
        k = cols.shape[0]
        rows = np.arange(self.n, self.n + k)
        self.r.append(np.repeat(rows, cols.shape[1]))
        self.c.append(cols.ravel())
        self.v.append(vals.ravel())
        self.b.append(np.broadcast_to(rhs, (k,)).astype(float))
        self.w.append(np.broadcast_to(w, (k,)).astype(float))
        self.n += k

    def solve(self, nvar):
        A = sp.csr_matrix((np.concatenate(self.v), (np.concatenate(self.r), np.concatenate(self.c))),
                          shape=(self.n, nvar))
        w = np.concatenate(self.w)
        b = np.concatenate(self.b)
        W = sp.diags(w ** 2)
        AtW = A.T @ W
        N = (AtW @ A + sp.identity(nvar) * 1e-9).tocsc()
        return spsolve(N, AtW @ b)


def smooth(t, lat, lon, cadence=None, cfg: Config = Config()) -> Result:
    """Smooth a track. Samples sharing a timestamp are merged, and the result is
    expanded back so every output array has one entry per input sample."""
    t = np.asarray(t, float)
    ut, inv = np.unique(t, return_inverse=True)
    if len(ut) == len(t):
        return _smooth(t, np.asarray(lat, float), np.asarray(lon, float), cadence, cfg)
    if np.any(np.diff(t) < 0):
        raise ValueError("timestamps go backwards")

    def merge(a):
        a = np.asarray(a, float)
        out = np.full(len(ut), np.nan)
        for k in range(len(ut)):
            v = a[inv == k]
            v = v[np.isfinite(v)]
            if len(v):
                out[k] = v.mean()
        return out

    r = _smooth(ut, merge(lat), merge(lon), None if cadence is None else merge(cadence), cfg)
    # Duplicate samples sit at the same place; segment arrays are padded with a zero-length segment.
    info = dict(r.info)
    info["zx"], info["zy"] = to_local(np.asarray(lat, float), np.asarray(lon, float), info["lat0"], info["lon0"])

    def expand_seg(a):
        if a is None:
            return None
        # sample j -> j+1: same unique index means zero-length segment.
        k0, k1 = inv[:-1], inv[1:]
        return np.where(k1 > k0, a[np.minimum(k0, len(a) - 1)], np.nan)

    gw = np.full(len(t), np.nan)
    gw[np.isfinite(lat)] = r.gps_weight[inv][np.isfinite(lat)]
    seg_speed = np.nan_to_num(expand_seg(r.seg_speed))
    return Result(r.x[inv], r.y[inv], r.lat[inv], r.lon[inv], seg_speed, r.point_speed[inv],
                  r.dist[inv], expand_seg(r.stride), gw, expand_seg(r.cad_weight), r.iterations, info)


def _smooth(t, lat, lon, cadence, cfg: Config) -> Result:
    from dataclasses import replace as _replace
    cfg = _replace(cfg, max_cad_gap=gap_threshold(t, cfg.max_cad_gap))
    n = len(t)
    have = np.isfinite(lat) & np.isfinite(lon)
    if have.sum() < 3:
        raise ValueError("need at least 3 GPS points")
    lat0, lon0 = float(np.nanmean(lat)), float(np.nanmean(lon))
    zx, zy = to_local(lat, lon, lat0, lon0)
    gi = np.where(have)[0]

    dt = np.diff(t)
    if np.any(dt <= 0):
        raise ValueError("timestamps must be strictly increasing")

    use_cad = cfg.use_cadence and cadence is not None and np.isfinite(cadence).sum() > 10
    # Cadence on point i+1 describes the interval i -> i+1 (strides/min -> strides/s).
    if use_cad:
        c_seg = np.asarray(cadence, float)[1:] / 60.0
        # Judge cadence on its own terms: only values near your normal running cadence count.
        # (Wonky readings, walking and slowing for lights fall outside the band and are ignored.)
        moving_c = c_seg[np.isfinite(c_seg) & (c_seg > 0.5)]
        c_med = float(np.median(moving_c)) if len(moving_c) else np.nan
        cad_ok = (np.isfinite(c_seg) & (dt <= cfg.max_cad_gap)
                  & (np.abs(c_seg - c_med) <= cfg.cad_band * c_med))
        use_cad = cad_ok.sum() > 10
    if use_cad:
        # Stage 1: GPS-only smooth. Its distance while moving is nearly unbiased, unlike raw GPS,
        # so it sets the run's average stride; cadence then shapes pace within the run.
        from dataclasses import replace
        ref = _smooth(t, lat, lon, None, replace(cfg, use_cadence=False))
        m_ref = cad_ok & (ref.seg_speed > 1.0)
        use_cad = m_ref.sum() > 10
    if use_cad:
        cd = c_seg[m_ref] * dt[m_ref]
        s_ref = float(np.sum(ref.seg_speed[m_ref] * dt[m_ref]) / np.sum(cd))
        level_idx = np.where(m_ref)[0]
        level_w = cd / cd.sum()
    # Where the stopped-state prior may act.
    no_gap = dt <= cfg.max_cad_gap
    if use_cad:
        # Only near-zero cadence suggests standing still; wonky-low cadence while running doesn't.
        not_running = ~np.isfinite(c_seg) | (c_seg < cfg.stop_cadence_frac * c_med)
        stop_gate, stop_lam = no_gap & not_running, cfg.stop_strength
    else:
        stop_gate, stop_lam = no_gap, cfg.stop_strength_no_cadence
    w_stop = np.ones(n - 1)
    nseg = n - 1
    ix, iy, is_ = 0, n, 2 * n               # variable offsets
    nvar = 2 * n + (nseg if use_cad else 0)

    # Initial state: interpolate GPS onto all timestamps.
    x = np.interp(t, t[gi], zx[gi])
    y = np.interp(t, t[gi], zy[gi])
    s = None

    w_gps = np.ones(len(gi))
    w_acc = np.ones(max(n - 2, 0))
    w_cad = np.ones(nseg) if use_cad else None
    heading = None

    # Second-difference (acceleration) stencil, shared by every iteration.
    i = np.arange(1, n - 1)
    d0, d1 = dt[:-1], dt[1:]
    h = 0.5 * (d0 + d1)
    a_vals = np.stack([1 / (d0 * h), -(1 / d0 + 1 / d1) / h, 1 / (d1 * h)], axis=1)
    a_scale = np.sqrt(h) / cfg.accel_sigma   # white-noise-acceleration weighting
    # Don't tie velocities together across recording gaps: you may have stopped, or kept running.
    a_scale = np.where((d0 > cfg.max_cad_gap) | (d1 > cfg.max_cad_gap), a_scale * 1e-3, a_scale)

    # Pace ceiling: by default relative to your median moving speed on the raw GPS.
    v_max = cfg.max_speed
    if v_max is None:
        v0 = np.hypot(np.diff(x), np.diff(y)) / dt
        v_max = cfg.max_speed_factor * float(np.median(v0[v0 > 1.0])) if np.any(v0 > 1.0) else 6.0

    # Iteration schedule: GPS-only warm-up, then cadence fully trusted, then everything robust.
    cad_start = 2
    robust_cad_from = cad_start + cfg.cad_trust_iters
    it = 0
    for it in range(cfg.iters + robust_cad_from):
        cad_on = use_cad and it >= cad_start
        rows = _Rows()

        # GPS
        sw = np.sqrt(w_gps) / cfg.gps_sigma
        rows.add(np.c_[ix + gi], np.ones((len(gi), 1)), zx[gi], sw)
        rows.add(np.c_[iy + gi], np.ones((len(gi), 1)), zy[gi], sw)

        # Motion prior
        sw = np.sqrt(w_acc) * a_scale
        for off in (ix, iy):
            rows.add(np.stack([off + i - 1, off + i, off + i + 1], axis=1), a_vals, 0.0, sw)

        # Pace ceiling (active set): wherever the current path is too fast, pull it to v_max.
        vx, vy = np.diff(x) / dt, np.diff(y) / dt
        vn = np.hypot(vx, vy)
        fast = np.where(vn > v_max)[0]
        if len(fast):
            ux, uy, dk = vx[fast] / vn[fast], vy[fast] / vn[fast], dt[fast]
            cols = np.stack([ix + fast, ix + fast + 1, iy + fast, iy + fast + 1], axis=1)
            vals = np.stack([-ux / dk, ux / dk, -uy / dk, uy / dk], axis=1)
            rows.add(cols, vals, v_max, np.sqrt(dk) / cfg.max_speed_sigma)

        # Stopped state (IRLS form of a Student-t on velocity centred at zero).
        sk = np.where(stop_gate)[0]
        if len(sk):
            sw = np.sqrt(stop_lam * w_stop[sk] * dt[sk]) / cfg.stop_sigma
            ones = np.ones(len(sk))
            for off in (ix, iy):
                rows.add(np.stack([off + sk, off + sk + 1], axis=1),
                         np.stack([-ones / dt[sk], ones / dt[sk]], axis=1), 0.0, sw)

        if use_cad:
            k = np.arange(nseg)
            if s is None:
                s = np.full(nseg, s_ref)
            if cad_on and (heading is None or cfg.heading_update):
                # Fix the direction of travel once, from the GPS-only path smoothed over a window.
                # Cadence then only says how fast you progress along it; sideways wiggles
                # can't be used to "spend" cadence distance.
                lo = np.searchsorted(t, t[:-1] - cfg.heading_window)
                hi = np.searchsorted(t, t[1:] + cfg.heading_window, side="right") - 1
                hx, hy = x[hi] - x[lo], y[hi] - y[lo]
                hn = np.hypot(hx, hy)
                span_t = t[hi] - t[lo]
                h_ok = hn > cfg.min_speed_for_dir * np.maximum(span_t, 1e-3)
                heading = (np.where(h_ok, hx / np.maximum(hn, 1e-9), 0.0),
                           np.where(h_ok, hy / np.maximum(hn, 1e-9), 0.0), h_ok)
            if cad_on:
                ok = cad_ok & heading[2]
                ux, uy = heading[0][ok], heading[1][ok]
                kk, dk = k[ok], dt[ok]
                # u . (p[k+1]-p[k]) / dt - c * s_k = 0
                cols = np.stack([ix + kk, ix + kk + 1, iy + kk, iy + kk + 1, is_ + kk], axis=1)
                vals = np.stack([-ux / dk, ux / dk, -uy / dk, uy / dk, -c_seg[ok]], axis=1)
                rows.add(cols, vals, 0.0, np.sqrt(w_cad[ok] * dk) / cfg.cad_sigma)
            # Stride: slow random walk, with its cadence-weighted average pinned to s_ref.
            if nseg > 1:
                ds = dt[1:]
                rows.add(np.stack([is_ + k[:-1], is_ + k[1:]], axis=1),
                         np.tile([-1.0, 1.0], (nseg - 1, 1)), 0.0,
                         1.0 / (cfg.stride_drift * np.sqrt(np.maximum(ds, 1e-3))))
            # (Sparse form: a per-sample pull toward s_ref whose total weight pins the average.)
            rows.add(np.c_[is_ + level_idx], np.ones((len(level_idx), 1)), s_ref,
                     np.sqrt(level_w) / cfg.stride_level_sigma)

        sol = rows.solve(nvar)
        nx, ny = sol[ix:ix + n], sol[iy:iy + n]
        if use_cad:
            s = sol[is_:is_ + nseg]
        change = float(np.max(np.hypot(nx - x, ny - y)))
        x, y = nx, ny

        # Robust reweighting from the current residuals (first solve above used uniform weights).
        rg2 = (x[gi] - zx[gi]) ** 2 + (y[gi] - zy[gi]) ** 2
        w_gps = _t_weight(rg2, cfg.gps_sigma, cfg.gps_nu, 2)
        ax = a_vals[:, 0] * x[i - 1] + a_vals[:, 1] * x[i] + a_vals[:, 2] * x[i + 1]
        ay = a_vals[:, 0] * y[i - 1] + a_vals[:, 1] * y[i] + a_vals[:, 2] * y[i + 1]
        w_acc = _t_weight(ax ** 2 + ay ** 2, cfg.accel_sigma, cfg.accel_nu, 2)
        v2 = (np.diff(x) ** 2 + np.diff(y) ** 2) / dt ** 2
        w_stop = _t_weight(v2, cfg.stop_sigma, cfg.stop_nu, 2) / ((cfg.stop_nu + 2) / cfg.stop_nu)
        if use_cad and it + 1 >= robust_cad_from:
            v_along = (np.diff(x) * heading[0] + np.diff(y) * heading[1]) / dt
            rc = np.where(cad_ok & heading[2], v_along - s * c_seg, 0.0)
            w_cad = _t_weight(rc ** 2, cfg.cad_sigma, cfg.cad_nu, 1)

        if it > robust_cad_from and change < cfg.tol:
            break

    seg_speed = np.hypot(np.diff(x), np.diff(y)) / dt
    dist = np.concatenate([[0.0], np.cumsum(seg_speed * dt)])
    point_speed = np.empty(n)
    point_speed[0], point_speed[-1] = seg_speed[0], seg_speed[-1]
    point_speed[1:-1] = 0.5 * (seg_speed[:-1] + seg_speed[1:])
    rlat, rlon = to_latlon(x, y, lat0, lon0)

    wg_full = np.full(n, np.nan)
    wg_full[gi] = w_gps / ((cfg.gps_nu + 2) / cfg.gps_nu)   # normalise so max is 1
    wc = None
    if use_cad:
        wc = np.where(cad_ok, w_cad / ((cfg.cad_nu + 1) / cfg.cad_nu), np.nan)
    return Result(x, y, rlat, rlon, seg_speed, point_speed, dist,
                  s if use_cad else None, wg_full, wc, it + 1,
                  info={"lat0": lat0, "lon0": lon0, "zx": zx, "zy": zy, "v_max": v_max,
                        "change": change})
