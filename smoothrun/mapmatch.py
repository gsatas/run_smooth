"""HMM map matching of a smoothed track onto the walkable network (Newson & Krumm 2009 style).

Observations are the step-1 smoothed positions, sampled every few seconds.
  emission:   how far the candidate is from the smoothed position (Gaussian)
  transition: |network route length - smoothed path length| between observations (exponential),
              loosened across recording gaps
Viterbi picks the most likely sequence; the route between picks is the network shortest path.
"""
from dataclasses import dataclass

import numpy as np
from scipy.sparse.csgraph import dijkstra


@dataclass
class MatchConfig:
    obs_every: float = 4.0       # s between HMM observations
    radius: float = 100.0        # m search radius (urban-canyon drift of 50-80 m happens)
    k_max: int = 14
    emission_sigma: float = 20.0  # m; wide because canyon offsets are large and persist for minutes
    emission_weight: float = 0.3  # <1 because GPS errors are correlated over ~30-60 s, so samples
                                  # every few seconds aren't independent evidence
    beta: float = 6.0            # m; tolerance on route vs smoothed-path length
    gap_s: float = 5.0           # s; a time step longer than this is a recording gap
    gap_beta_frac: float = 0.5   # across gaps, tolerance = this fraction of the smoothed length
    route_slack: float = 2.0     # search routes up to slack * expected + 60 m
    pace_window: float = 30.0    # s each side for the robust pace behind expected distances
    despike_m: float = 60.0      # remove back-and-forth spurs that retrace less than this
                                 # (jitter / standing still); real turnarounds have long legs
    splice_off_m: float = 20.0   # track this far off the route ...
    splice_min_s: float = 8.0    # ... for this long is an "off-network" candidate stretch
    splice_margin_m: float = 15.0  # splice only if the track's length fits cadence this much better
    splice_extend_s: float = 10.0  # max seconds a stretch is extended at each end
    splice_min_rate: float = 0.3   # m/s: cadence shortfall must be concentrated, not a slow drift
    splice_max_s: float = 90.0     # longer "off-network" stretches are GPS drift, not real
    turn_cost: float = 3.0       # log-prob cost of switching to a differently named street
                                 # (runners, races especially, don't zig-zag between streets)


@dataclass
class Match:
    obs_idx: np.ndarray        # sample index of each observation used
    cand: list                 # chosen candidate per observation (edge, frac, qx, qy, dist)
    route_xy: np.ndarray       # matched route polyline (M, 2)
    route_s: np.ndarray        # cumulative distance along route (M,)
    obs_s: np.ndarray          # along-route distance of each observation
    breaks: int                # times the HMM had to restart (no connecting route)
    length: float
    progress: np.ndarray       # robust cumulative distance used for expectations / pace shape
    splices: list = None       # off-network stretches where the track geometry was used


def _street(net, c):
    """Street name for the turn prior; None for unnamed ways (park paths, connectors)."""
    w = net.eway[c[0]]
    return net.way_tags[w].get("name") if w >= 0 else None


def _ends(net, c):
    e, tau = c[0], c[1]
    L = net.elen[e]
    return (net.ea[e], tau * L), (net.eb[e], (1 - tau) * L)


def _choose_obs(t, step_dist, every, gap_s):
    """Sample indices every `every` seconds, always keeping both sides of a recording gap."""
    keep = [0]
    for i in range(1, len(t)):
        if t[i] - t[keep[-1]] >= every or (t[i] - t[i - 1] > gap_s) or (
                i + 1 < len(t) and t[i + 1] - t[i] > gap_s):
            keep.append(i)
    if keep[-1] != len(t) - 1:
        keep.append(len(t) - 1)
    return np.array(keep)


def robust_progress(t, cum_dist, gap_s, window):
    """Cumulative distance from a running median of speed. Unlike the smoothed path's own
    length, this isn't inflated when GPS drift drags the path a block sideways and back."""
    dt = np.diff(t)
    v = np.diff(cum_dist) / np.maximum(dt, 1e-3)
    tm = 0.5 * (t[1:] + t[:-1])
    moving = dt <= gap_s
    vr = v.copy()
    lo = np.searchsorted(tm, tm - window)
    hi = np.searchsorted(tm, tm + window, side="right")
    for k in np.where(moving)[0]:
        w = v[lo[k]:hi[k]][moving[lo[k]:hi[k]]]
        vr[k] = np.median(w)
    return np.concatenate([[0.0], np.cumsum(vr * dt)])


def match(net, t, x, y, cum_dist, cfg=MatchConfig()):
    """cum_dist: the step-1 smoothed cumulative distance (used only through its robust pace)."""
    from dataclasses import replace
    from .smoother import gap_threshold
    cfg = replace(cfg, gap_s=gap_threshold(t, cfg.gap_s))
    cum_dist = robust_progress(t, cum_dist, cfg.gap_s, cfg.pace_window)
    obs = _choose_obs(t, None, cfg.obs_every, cfg.gap_s)
    P = np.c_[x, y]
    cands, keep = [], []
    for i in obs:
        c = net.candidates(P[i], cfg.radius, cfg.k_max)
        if c:
            cands.append(c)
            keep.append(i)
    obs = np.array(keep)
    n = len(obs)

    def emis(c):
        return -0.5 * cfg.emission_weight * (c[4] / cfg.emission_sigma) ** 2

    score = [np.array([emis(c) for c in cands[0]])]
    back = [None]
    breaks = 0
    for k in range(1, n):
        prev, cur = cands[k - 1], cands[k]
        i0, i1 = obs[k - 1], obs[k]
        expected = cum_dist[i1] - cum_dist[i0]
        gap = np.any(np.diff(t[i0:i1 + 1]) > cfg.gap_s)
        beta = max(cfg.beta, cfg.gap_beta_frac * expected) if gap else cfg.beta
        limit = cfg.route_slack * expected + 2 * cfg.radius + 60.0

        # Shortest paths from every endpoint node of the previous candidates.
        src = sorted({nd for c in prev for nd, _ in _ends(net, c)})
        D = dijkstra(net.adj, directed=False, indices=src, limit=limit)
        row = {nd: r for r, nd in enumerate(src)}
        trans = np.full((len(prev), len(cur)), -np.inf)
        for a, ca in enumerate(prev):
            ea_ = _ends(net, ca)
            for b, cb in enumerate(cur):
                if ca[0] == cb[0]:
                    route = abs(ca[1] - cb[1]) * net.elen[ca[0]]
                else:
                    route = min(oa + D[row[na], nb] + ob for na, oa in ea_ for nb, ob in _ends(net, cb))
                if np.isfinite(route):
                    trans[a, b] = -abs(route - expected) / beta
        if cfg.turn_cost:
            na_ = [_street(net, c) for c in prev]
            nb_ = [_street(net, c) for c in cur]
            sw = np.array([[(a is not None or b is not None) and a != b for b in nb_] for a in na_])
            trans = trans - cfg.turn_cost * sw
        tot = score[-1][:, None] + trans
        best = np.argmax(tot, axis=0)
        s = tot[best, np.arange(len(cur))] + np.array([emis(c) for c in cur])
        if not np.any(np.isfinite(s)):
            # No route connects these: restart the chain here.
            breaks += 1
            s = np.array([emis(c) for c in cur])
            best = None
        score.append(s)
        back.append(best)

    # Backtrack (segment by segment across breaks).
    choice = np.zeros(n, int)
    choice[-1] = int(np.argmax(score[-1]))
    for k in range(n - 1, 0, -1):
        if back[k] is None:
            choice[k - 1] = int(np.argmax(score[k - 1]))
        else:
            choice[k - 1] = int(back[k][choice[k]])
    chosen = [cands[k][choice[k]] for k in range(n)]

    raw_xy, obs_v = _route(net, chosen, t, obs, cum_dist, cfg)
    raw_s = _cum(raw_xy)
    route_xy = despike(raw_xy, cfg.despike_m) if cfg.despike_m else raw_xy
    route_s = _cum(route_xy)
    # Re-place each matched point on the cleaned route, never moving backwards.
    from .along import project
    q = np.array([c[2:4] for c in chosen])
    guess = raw_s[obs_v] * (route_s[-1] / max(raw_s[-1], 1e-9))
    g, _ = project(route_xy, route_s, q[:, 0], q[:, 1], guess, 150.0)
    g = np.where(np.isfinite(g), g, guess)
    obs_s = np.maximum.accumulate(g)
    m = Match(obs, chosen, route_xy, route_s, obs_s, breaks, float(route_s[-1]), cum_dist)
    return m


def splice_off_network(m, t, x, y, shortfall, valid, cfg=MatchConfig()):
    """Use the smoothed track's own geometry where you evidently ran off the mapped network
    (cut corners, unmapped paths, plazas). Evidence required:
      * the track sits well off the matched route for a while, and
      * the on-route solution with cadence fell short of (or exceeded) the cadence-implied
        distance around that stretch by about as much as the track and route lengths differ.
    GPS drifting alongside a correct street fails the second test: the on-route cadence
    solution absorbs it without any shortfall.
    shortfall: cumulative (cadence distance - solved distance) per sample; valid: per sample."""
    from .along import project
    s_guess, _, _ = place_on_route(m, t)
    gp, lat = project(m.route_xy, m.route_s, x, y, s_guess, 150.0)
    lat = np.nan_to_num(lat, nan=0.0)
    off = lat > cfg.splice_off_m
    seg_len = np.r_[0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
    splices, info = [], []
    i, n = 0, len(t)
    while i < n:
        if not off[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and off[j + 1]:
            j += 1
        if t[j] - t[i] >= cfg.splice_min_s:
            a, b = i, j
            while (a > 0 and lat[a] > 6.0 and t[i] - t[a - 1] <= cfg.splice_extend_s
                   and (not splices or a > splices[-1][1])):
                a -= 1
            while b < n - 1 and lat[b] > 6.0 and t[b + 1] - t[j] <= cfg.splice_extend_s:
                b += 1
            if (np.isfinite(gp[a]) and np.isfinite(gp[b]) and gp[b] > gp[a]
                    and valid[a:b + 1].mean() >= 0.8):
                d_route = gp[b] - gp[a]
                d_track = seg_len[b] - seg_len[a]
                pa = np.searchsorted(t, t[a] - cfg.splice_extend_s)
                pb = min(np.searchsorted(t, t[b] + cfg.splice_extend_s), n - 1)
                d_need = shortfall[pb] - shortfall[pa]
                d_fix = d_track - d_route
                span = t[pb] - t[pa]
                if (abs(d_need) >= cfg.splice_margin_m and np.sign(d_fix) == np.sign(d_need)
                        and abs(d_need) / max(span, 1.0) >= cfg.splice_min_rate
                        and t[b] - t[a] <= cfg.splice_max_s
                        and abs(d_fix - d_need) < 0.5 * abs(d_need)):
                    if not splices or (a > splices[-1][1] and gp[a] > splices[-1][3]):
                        splices.append((a, b, gp[a], gp[b]))
                        info.append(dict(t0=t[a], t1=t[b], route=d_route, track=d_track,
                                         shortfall=d_need, max_off=float(lat[a:b + 1].max())))
            i = b + 1
        else:
            i = j + 1
    if not splices:
        return m
    R, Rs = m.route_xy, m.route_s
    at = lambda sv: np.array([np.interp(sv, Rs, R[:, 0]), np.interp(sv, Rs, R[:, 1])])
    pieces, s_prev = [], 0.0
    for a, b, sa, sb in splices:
        inside = (Rs > s_prev) & (Rs < sa)
        pieces += [at(s_prev)] + list(R[inside]) + [at(sa)]
        pieces += list(np.c_[x[a + 1:b], y[a + 1:b]])
        s_prev = sb
    inside = Rs > s_prev
    pieces += [at(s_prev)] + list(R[inside])
    new_xy = np.array(pieces)
    new_s = _cum(new_xy)
    # Re-place observations on the new route.
    guess = m.obs_s * new_s[-1] / max(Rs[-1], 1e-9)
    gq, _ = project(new_xy, new_s, x[m.obs_idx], y[m.obs_idx], guess, 150.0)
    gq = np.where(np.isfinite(gq), gq, guess)
    return Match(m.obs_idx, m.cand, new_xy, new_s, np.maximum.accumulate(gq), m.breaks,
                 float(new_s[-1]), m.progress, info)


def _cum(P):
    seg = np.hypot(*np.diff(P, axis=0).T) if len(P) > 1 else np.zeros(0)
    return np.concatenate([[0], np.cumsum(seg)])


def despike(P, max_leg, tol=3.0, step=1.0):
    """Remove short back-and-forth spurs (matched-position jitter) from a polyline.

    At each reversal, measure how far the path retraces itself: walk the same arc length
    back along the incoming path and forward along the outgoing path, while the two stay
    within `tol`. Jitter retraces a few metres; a real out-and-back turnaround retraces the
    same road for far longer than `max_leg` and is kept."""
    P = np.asarray(P, float)
    for _ in range(5000):
        keep = np.r_[True, np.hypot(*np.diff(P, axis=0).T) > 0.05]
        P = P[keep]
        if len(P) < 3:
            break
        d = np.diff(P, axis=0)
        L = np.hypot(*d.T)
        S = np.r_[0, np.cumsum(L)]
        u = d / L[:, None]
        rev = np.where((u[:-1] * u[1:]).sum(1) < -0.8)[0] + 1
        at = lambda sv: np.c_[np.interp(sv, S, P[:, 0]), np.interp(sv, S, P[:, 1])]
        cut = None
        for j in rev:
            r = np.arange(step, max_leg + 2 * step, step)
            r = r[(S[j] - r >= 0) & (S[j] + r <= S[-1])]
            if len(r) == 0:
                continue
            apart = np.hypot(*(at(S[j] - r) - at(S[j] + r)).T) > tol
            r_stop = r[np.argmax(apart)] - step if apart.any() else (r[-1] if r[-1] >= max_leg else None)
            if r_stop is not None and 0 < r_stop < max_leg:
                cut = (S[j] - r_stop, S[j] + r_stop)
                break
        if cut is None:
            break
        a, b = cut
        P = np.vstack([P[S < a], at(a), at(b), P[S > b]])
    return P


def _route(net, chosen, t, obs, cum_dist, cfg):
    """Polyline through the chosen candidates along network shortest paths.
    Returns the polyline and, for each observation, its vertex index in the polyline."""
    pts = [np.array(chosen[0][2:4])]
    obs_vertex = [0]
    for k in range(1, len(chosen)):
        ca, cb = chosen[k - 1], chosen[k]
        qa, qb = np.array(ca[2:4]), np.array(cb[2:4])
        if ca[0] == cb[0]:
            pts.append(qb)
        else:
            expected = cum_dist[obs[k]] - cum_dist[obs[k - 1]]
            limit = cfg.route_slack * expected + 2 * cfg.radius + 60.0
            best = None
            for na, oa in _ends(net, ca):
                D, pred = dijkstra(net.adj, directed=False, indices=na, limit=limit,
                                   return_predecessors=True)
                for nb, ob in _ends(net, cb):
                    tot = oa + D[nb] + ob
                    if best is None or tot < best[0]:
                        best = (tot, na, nb, pred)
            if best is None or not np.isfinite(best[0]):
                pts.append(qb)  # disconnected (HMM break): straight line
            else:
                _, na, nb, pred = best
                path = [nb]
                while path[-1] != na:
                    path.append(pred[path[-1]])
                for nd in reversed(path):
                    pts.append(net.xy[nd])
                pts.append(qb)
        obs_vertex.append(len(pts) - 1)
    return np.array(pts), np.array(obs_vertex)


def place_on_route(m, t, cum_dist=None):
    """Along-route distance for every sample: route distance at the observations, with the
    robust pace shape in between. Returns (s, x, y)."""
    cum_dist = m.progress if cum_dist is None else cum_dist
    s_obs, i_obs = m.obs_s, m.obs_idx
    s = np.empty(len(t))
    s[:i_obs[0] + 1] = s_obs[0]
    s[i_obs[-1]:] = s_obs[-1]
    for k in range(len(i_obs) - 1):
        a, b = i_obs[k], i_obs[k + 1]
        d0, d1 = cum_dist[a], cum_dist[b]
        if d1 - d0 > 1e-6:
            f = (cum_dist[a:b + 1] - d0) / (d1 - d0)
        else:
            f = (t[a:b + 1] - t[a]) / max(t[b] - t[a], 1e-9)
        s[a:b + 1] = s_obs[k] + f * (s_obs[k + 1] - s_obs[k])
    rx = np.interp(s, m.route_s, m.route_xy[:, 0])
    ry = np.interp(s, m.route_s, m.route_xy[:, 1])
    return s, rx, ry
