"""Achievements, computed from the local run copy (smoothrun/mirror.py).

Every sync recomputes them from scratch: the runs since ACH_SINCE are replayed in date order and
each rule in achievements/catalog.json is checked, so an achievement's "earned" date and run come
straight from your history and changing a rule re-scores everything. Nothing is stored apart from
a cache of per-run facts (mirror/facts.json), which is rebuilt when a run or FACTS_VERSION changes.

Rules come in two kinds:
  * per-run: a test on one run. Levels count the runs that pass.
  * history: a value over all runs so far (a streak, a monthly total). These only ever grow as
    runs are added, so the run that first reached each level is found by binary search.

Distances are in miles and temperatures in °F, as in the catalog. Times are local to each run.
"""
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from smoothrun import mirror, places

MILE = 1609.344
FT = 0.3048
FACTS_VERSION = 5
RESULT_VERSION = 4
CATALOG = os.environ.get("ACH_CATALOG",
                         os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "achievements", "catalog.json"))
ACH_SINCE = os.environ.get("ACH_SINCE", mirror.SYNC_SINCE)
# Mile Marker's data folder, mounted read-only: settings.json (birthday, home) and plans/*.json.
MM_DATA = os.environ.get("MM_DATA", "/data/mile_marker")
TRAVEL_MILES = 100


# ---------------------------------------------------------------- astronomy (approximate)

def _jd(dt_utc):
    return dt_utc.timestamp() / 86400.0 + 2440587.5


def _from_jd(jd):
    return datetime.fromtimestamp((jd - 2440587.5) * 86400.0, tz=timezone.utc)


def moon_phase_time(k, full):
    """Meeus ch. 49, main terms: the new (full=False) or full moon of lunation k, as a UTC datetime.
    Good to within about an hour."""
    if full:
        k = math.floor(k) + 0.5
    T = k / 1236.85
    jde = 2451550.09766 + 29.530588861 * k + 0.00015437 * T * T
    E = 1 - 0.002516 * T
    M = math.radians((2.5534 + 29.10535670 * k) % 360)
    Mp = math.radians((201.5643 + 385.81693528 * k) % 360)
    F = math.radians((160.7108 + 390.67050284 * k) % 360)
    if full:
        c = (-0.40614 * math.sin(Mp) + 0.17302 * E * math.sin(M) + 0.01614 * math.sin(2 * Mp)
             + 0.01043 * math.sin(2 * F) + 0.00734 * E * math.sin(Mp - M) - 0.00515 * E * math.sin(Mp + M))
    else:
        c = (-0.40720 * math.sin(Mp) + 0.17241 * E * math.sin(M) + 0.01608 * math.sin(2 * Mp)
             + 0.01039 * math.sin(2 * F) + 0.00739 * E * math.sin(Mp - M) - 0.00514 * E * math.sin(Mp + M))
    return _from_jd(jde + c)


def nearest_moon(dt_utc, full):
    k0 = round((_jd(dt_utc) - 2451550.09766) / 29.530588861 - (0.5 if full else 0))
    times = [moon_phase_time(k, full) for k in (k0 - 1, k0, k0 + 1)]
    return min(times, key=lambda t: abs((t - dt_utc).total_seconds()))


def solstice(year, june):
    """Meeus ch. 27 mean solstice, UTC (within minutes; ΔT ignored)."""
    Y = (year - 2000) / 1000.0
    if june:
        jde = 2451716.56767 + 365241.62603 * Y + 0.00325 * Y ** 2 + 0.00888 * Y ** 3 - 0.00030 * Y ** 4
    else:
        jde = 2451900.05952 + 365242.74049 * Y - 0.06223 * Y ** 2 - 0.00823 * Y ** 3 + 0.00032 * Y ** 4
    return _from_jd(jde)


def sun_times(day, lat, lon, utc_offset):
    """Local sunrise and sunset (naive datetimes) for a date and place, NOAA's simple method.
    None where the sun doesn't rise or set."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl)) - math.tan(la) * math.tan(decl)
    if abs(cos_ha) > 1:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    tz_min = utc_offset.total_seconds() / 60
    rise = 720 - 4 * (lon + ha) - eqt + tz_min
    sett = 720 - 4 * (lon - ha) - eqt + tz_min
    base = datetime(day.year, day.month, day.day)
    return base + timedelta(minutes=rise), base + timedelta(minutes=sett)


def thanksgiving(year):
    d = date(year, 11, 1)
    return d + timedelta(days=(3 - d.weekday()) % 7 + 21)


# ---------------------------------------------------------------- per-run facts

def _haversine(la1, lo1, la2, lo2):
    p1, p2 = np.radians(la1), np.radians(la2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371000.0 * np.arcsin(np.sqrt(np.minimum(1.0, a)))


def _series(a):
    keys = a.get("recordingKeys") or []
    vals = a.get("recordingValues") or []
    get = lambda k: np.array([np.nan if v is None else v for v in vals[keys.index(k)]], float) if k in keys else None
    clock, dist = get("clock"), get("distance")
    if clock is None or dist is None or len(clock) < 2:
        return None
    clock = np.nan_to_num(clock, nan=0.0)
    pauses = set(a.get("pauseIndexes") or [])
    step = np.r_[0.0, np.maximum(0.0, np.diff(clock))]
    step[[i for i in pauses if 0 < i < len(step)]] = 0.0
    d = np.maximum.accumulate(np.nan_to_num(dist, nan=0.0) * 1000.0)
    s = {"t": np.cumsum(step), "d": d, "clock": clock}
    lat, lon = get("latitude"), get("longitude")
    if lat is not None and lon is not None:
        bad = (((lat == -1) & (lon == -1)) | ((lat == 0) & (lon == 0)) | (np.abs(lat) > 90) | (np.abs(lon) > 180))
        lat, lon = lat.copy(), lon.copy()
        lat[bad] = np.nan
        lon[bad] = np.nan
        if np.isfinite(lat).sum() >= 2:
            s["lat"], s["lon"] = lat, lon
    elev = get("elevation")
    if elev is not None and np.isfinite(elev).sum() >= 2 and not a.get("isBadElevation"):
        s["elev"] = elev
    return s


def _best_effort(s, length):
    """Fastest moving time over any stretch of `length` metres, interpolated."""
    d, t = s["d"], s["t"]
    if d[-1] < length:
        return None
    starts = d[d <= d[-1] - length]
    if not len(starts):
        return None
    times = np.interp(starts + length, d, t) - np.interp(starts, d, t)
    times = times[times > 0]
    return float(times.min()) if len(times) else None


def _splits(s, unit):
    d, t = s["d"], s["t"]
    n = int(d[-1] // unit)
    if n < 1:
        return []
    marks = np.interp(np.arange(0, n + 1) * unit, d, t)
    return [float(x) for x in np.diff(marks)]


def _climb(s):
    """True if some stretch of at least a mile climbs 300 ft or more with no drop of over 30 ft."""
    if "elev" not in s:
        return False
    d, e = s["d"], s["elev"]
    ok = np.isfinite(e)
    if ok.sum() < 10 or d[-1] < MILE:
        return False
    grid = np.arange(0, d[-1], 20.0)
    prof = np.interp(grid, d[ok], e[ok])
    prof = np.convolve(prof, np.ones(5) / 5, mode="same")
    for i in range(len(prof)):
        if i and prof[i] >= prof[i - 1]:
            continue   # start climbs only at a local low
        top = prof[i]
        for j in range(i + 1, len(prof)):
            top = max(top, prof[j])
            if top - prof[j] > 30 * FT:
                break
            if grid[j] - grid[i] >= MILE and prof[j] - prof[i] >= 300 * FT:
                return True
    return False


def _out_and_back(s):
    """Share of the way out that the way back retraces within 40 m (0 if not measurable)."""
    if "lat" not in s or s["d"][-1] < 800:
        return 0.0
    ok = np.isfinite(s["lat"])
    d, la, lo = s["d"][ok], s["lat"][ok], s["lon"][ok]
    grid = np.arange(0, d[-1], 25.0)
    gla, glo = np.interp(grid, d, la), np.interp(grid, d, lo)
    far = int(np.argmax(_haversine(gla[0], glo[0], gla, glo)))
    if far < 8 or len(grid) - far < 8:
        return 0.0
    out_la, out_lo, back_la, back_lo = gla[:far], glo[:far], gla[far:], glo[far:]
    near = [_haversine(y, x, back_la, back_lo).min() <= 40 for y, x in zip(out_la, out_lo)]
    return float(np.mean(near))


def facts(a):
    """Everything the rules look at for one run, as JSON-safe values."""
    start = datetime.fromisoformat(a["startDateTimeLocal"])
    local = start.replace(tzinfo=None)
    offset = start.utcoffset() or timedelta(0)
    s = _series(a)
    elapsed = float(s["clock"][-1] - s["clock"][0]) if s is not None else float(a.get("duration") or 0)
    dist_m = float(a.get("distance") or 0) * 1000.0
    f = {
        "id": int(a["activityId"]), "updated": a.get("dateUpdatedUTC"),
        "start": local.isoformat(), "utc": start.astimezone(timezone.utc).isoformat(),
        "offset_min": offset.total_seconds() / 60, "date": local.date().isoformat(),
        "finish": (local + timedelta(seconds=elapsed)).isoformat(),
        "miles": dist_m / MILE, "moving_s": float(a.get("duration") or 0), "elapsed_s": elapsed,
        "temp_f": a["temperature"] * 9 / 5 + 32 if a.get("temperature") is not None else None,
        "humidity": a.get("humidity"),
        # Smashrun keeps its other values in metric units, so wind is taken as km/h.
        "wind_mph": a["windSpeed"] / 1.609344 if a.get("windSpeed") is not None else None,
        "weather": (a.get("weatherType") or "").lower() or None,
        "gain_ft": a["elevationGain"] / FT if a.get("elevationGain") is not None else None,
        "loss_ft": a["elevationLoss"] / FT if a.get("elevationLoss") is not None else None,
        "race": bool(a.get("isRace")), "treadmill": bool(a.get("isTreadmill")),
        "city": a.get("city"), "state": a.get("state"), "country": a.get("country"),
        "country_code": (a.get("countryCode") or "").lower() or None,
    }
    if s is None:
        return f
    d = s["d"]
    f["track_mi"] = float(d[-1] / MILE)
    f["best"] = {k: _best_effort(s, m) for k, m in (("mile", MILE), ("5k", 5000.0), ("10k", 10000.0), ("half", 21097.5))}
    f["mile_splits"] = _splits(s, MILE)
    f["km_splits"] = _splits(s, 1000.0)
    f["quarters"] = [[float(x) for x in np.diff(np.interp(np.arange(k * 4, k * 4 + 5) * MILE / 4, d, s["t"]))]
                     for k in range(len(f["mile_splits"]))]
    f["fourths"] = [float(x) for x in np.diff(np.interp(np.linspace(0, d[-1], 5), d, s["t"]))]
    if d[-1] >= 2000:
        f["last_km_s"] = float(s["t"][-1] - np.interp(d[-1] - 1000, d, s["t"]))
        f["best_km_s"] = _best_effort(s, 1000.0)
    f["climb_mile"] = _climb(s)
    if "lat" in s:
        ok = np.isfinite(s["lat"])
        la, lo = s["lat"][ok], s["lon"][ok]
        f["start_ll"], f["end_ll"] = [float(la[0]), float(lo[0])], [float(la[-1]), float(lo[-1])]
        f["start_end_m"] = float(_haversine(la[0], lo[0], la[-1], lo[-1]))
        f["out_back"] = _out_and_back(s)
        rise, sett = sun_times(local.date(), float(la[0]), float(lo[0]), offset)
        f["sunrise"] = rise.isoformat() if rise else None
        f["sunset"] = sett.isoformat() if sett else None
        f["places"] = places.places(s["lat"], s["lon"], s["d"])
    return f


def load_facts(runs, root=None):
    """Facts for each summary row, cached in mirror/facts.json by run and update time."""
    path = os.path.join(root or mirror.MIRROR_DIR, "facts.json")
    try:
        with open(path) as fh:
            cache = json.load(fh)
    except (FileNotFoundError, ValueError):
        cache = {}
    if cache.get("version") != FACTS_VERSION:
        cache = {"version": FACTS_VERSION, "runs": {}}
    out, changed = [], False
    for row in runs:
        key = str(row["id"])
        hit = cache["runs"].get(key)
        if not hit or hit.get("updated") != row.get("updated"):
            try:
                with open(os.path.join(root or mirror.MIRROR_DIR, "activities", "%d.json" % row["id"])) as fh:
                    hit = facts(json.load(fh))
            except (OSError, ValueError, KeyError) as e:
                print("  achievements: skipping run %s: %s" % (key, e), flush=True)
                continue
            cache["runs"][key] = hit
            changed = True
        out.append(hit)
    if changed:
        mirror._write_atomic(path, cache)
    return out


# ---------------------------------------------------------------- helpers for rules

def _d(s):
    return date.fromisoformat(s)


def _dt(s):
    return datetime.fromisoformat(s)


def _mon(day):
    return day - timedelta(days=day.weekday())


def _near_clock(dt, hh, mm, tol=30, twelve=False):
    """Within tol seconds of hh:mm (either half of the day when twelve)."""
    secs = dt.hour * 3600 + dt.minute * 60 + dt.second
    targets = [hh * 3600 + mm * 60] + ([((hh + 12) % 24) * 3600 + mm * 60] if twelve else [])
    return any(min(abs(secs - x), 86400 - abs(secs - x)) <= tol for x in targets)


def _is_prime(n):
    return n >= 2 and all(n % k for k in range(2, int(n ** 0.5) + 1))


def _mi2(r):
    return round(r["miles"] + 1e-9, 2)


def _near_mi(r, x, tol=0.02):
    return abs(_mi2(r) - x) <= tol + 1e-9


def _night(r):
    """True if any part of the run is before sunrise or after sunset."""
    if not r.get("sunrise") or not r.get("sunset"):
        return False
    return _dt(r["start"]) < _dt(r["sunrise"]) or _dt(r["finish"]) > _dt(r["sunset"])


def _rain(r):
    return bool(r.get("weather")) and any(w in r["weather"] for w in ("rain", "drizzle", "shower", "sleet"))


def _snow(r):
    return bool(r.get("weather")) and "snow" in r["weather"]


def _perfect(r):
    return (r.get("temp_f") is not None and 55 <= r["temp_f"] <= 65 and r.get("humidity") is not None
            and r["humidity"] < 70 and r.get("weather") is not None and not _rain(r) and not _snow(r))


def _season(day):
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer"}.get(day.month, "fall")


def _days(rs):
    return sorted({_d(r["date"]) for r in rs})


def _longest_run(flags):
    best = cur = 0
    for ok in flags:
        cur = cur + 1 if ok else 0
        best = max(best, cur)
    return best


def _day_streak(rs):
    days = _days(rs)
    best = cur = 0
    for i, day in enumerate(days):
        cur = cur + 1 if i and (day - days[i - 1]).days == 1 else 1
        best = max(best, cur)
    return best


def _weeks(rs):
    """Run count per Monday-start week, for every week from the first run to the last."""
    days = _days(rs)
    if not days:
        return []
    count = Counter(_mon(_d(r["date"])) for r in rs)
    out, w = [], _mon(days[0])
    while w <= _mon(days[-1]):
        out.append((w, count.get(w, 0)))
        w += timedelta(days=7)
    return out


def _months(rs):
    """(year, month) keys from the first run's month to the last's."""
    if not rs:
        return []
    a, b = _d(rs[0]["date"]), _d(rs[-1]["date"])
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_weeks(y, m):
    first = date(y, m, 1)
    last = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
    w, out = _mon(first), []
    while w <= last:
        out.append(w)
        w += timedelta(days=7)
    return out


def _consec_weeks(rs, n):
    return _longest_run(c >= n for _, c in _weeks(rs))


def _consec_months_all_weeks(rs, n):
    per = dict(_weeks(rs))
    return _longest_run(all(per.get(w, 0) >= n for w in _month_weeks(y, m)) for y, m in _months(rs))


def _per_month(rs, fn):
    groups = defaultdict(list)
    for r in rs:
        groups[r["date"][:7]].append(r)
    return max((fn(g) for g in groups.values()), default=0)


def _per_week(rs, fn):
    groups = defaultdict(list)
    for r in rs:
        groups[_mon(_d(r["date"]))].append(r)
    return max((fn(g) for g in groups.values()), default=0)


def _month_miles(rs):
    tot = Counter()
    for r in rs:
        tot[r["date"][:7]] += r["miles"]
    return [tot.get("%04d-%02d" % ym, 0.0) for ym in _months(rs)]


def _growth(rs):
    """Longest run of months each with more miles than the month before (which must have runs)."""
    m = _month_miles(rs)
    return _longest_run(i > 0 and m[i - 1] > 0 and m[i] > m[i - 1] for i in range(len(m)))


def _weekend_month(rs):
    """1 if some calendar month has a run on every weekend (weekends counted by their Saturday)."""
    days = set(_days(rs))
    for y, m in sorted({(d.year, d.month) for d in days}):
        sats = [date(y, m, 1) + timedelta(days=k) for k in range(31)]
        sats = [d for d in sats if d.month == m and d.weekday() == 5]
        if sats and all(s in days or s + timedelta(days=1) in days for s in sats):
            return 1
    return 0


def _consec_weekday(rs, wd):
    days = sorted(d for d in _days(rs) if d.weekday() == wd)
    best = cur = 0
    for i, d in enumerate(days):
        cur = cur + 1 if i and (d - days[i - 1]).days == 7 else 1
        best = max(best, cur)
    return best


def _working_weeks(rs):
    days = set(_days(rs))
    return sum(1 for w, _ in _weeks(rs) if all(w + timedelta(days=k) in days for k in range(5)))


def _gap(rs):
    days = _days(rs)
    return max(((days[i] - days[i - 1]).days - 1 for i in range(1, len(days))), default=0)


def _consec_months_with_runs(rs):
    have = {r["date"][:7] for r in rs}
    return _longest_run("%04d-%02d" % ym in have for ym in _months(rs))


def _consec_years(rs):
    years = sorted({_d(r["date"]).year for r in rs})
    return _longest_run(i == 0 or years[i] - years[i - 1] == 1 for i in range(len(years))) if years else 0


def _rolling_full_set(rs):
    """Runs on all seven weekdays inside some 7-day window (which means 7 days in a row)."""
    return _day_streak(rs)


def _the_usual(rs):
    days = set(_days(rs))
    for d in days:
        if d.day > 28:
            continue
        y, m, ok = d.year, d.month, 1
        for _ in range(2):
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            ok += date(y, m, d.day) in days
        if ok == 3:
            return 1
    return 0


def _pair_days(rs, a, b, next_year=False):
    days = set(_days(rs))
    for d in days:
        if (d.month, d.day) == a:
            other = date(d.year + (1 if next_year else 0), *b)
            if other in days:
                return 1
    return 0


def _four_seasons(rs):
    by_year = defaultdict(set)
    for d in _days(rs):
        by_year[d.year].add(_season(d))
    return max((len(s) for s in by_year.values()), default=0)


def _first_nice_in(rs, months):
    """Years in which you had a perfect-weather run in the given months."""
    return len({_d(r["date"]).year for r in rs if _d(r["date"]).month in months and _perfect(r)})


def _race_kinds(rs):
    kinds = set()
    for r in rs:
        if not r["race"]:
            continue
        km = r["miles"] * MILE / 1000
        for name, lo, hi in (("5k", 4.8, 5.4), ("10k", 9.6, 10.6), ("half", 20.5, 21.8)):
            if lo <= km <= hi:
                kinds.add(name)
    return len(kinds)


def _double_header(rs):
    races = sorted(_d(r["date"]) for r in rs if r["race"])
    return 2 if any((races[i] - races[i - 1]).days <= 6 for i in range(1, len(races))) else len(races[:1])


def _intervention(rs):
    return len({_d(r["date"]).year for r in rs if _d(r["date"]).month == 7 and 12 <= _dt(r["start"]).hour <= 16
                and (r.get("city") or "").lower() in ("new york", "manhattan", "brooklyn", "queens", "bronx", "staten island")})


def _dog_days(rs):
    per = Counter(_d(r["date"]).year for r in rs if r.get("temp_f") is not None and r["temp_f"] > 85)
    return max(per.values(), default=0)


def _bands(rs):
    return len({int(_mi2(r)) for r in rs if 3 <= _mi2(r) < 27})


def _dark_side(r):
    utc = _dt(r["utc"])
    nm = nearest_moon(utc, full=False)
    local_nm = (nm + timedelta(minutes=r["offset_min"])).date().isoformat()
    return local_nm == r["date"] and _night(r)


def _full_moon(r):
    mid = _dt(r["utc"]) + timedelta(seconds=r["elapsed_s"] / 2)
    fm = nearest_moon(mid, full=True)
    return abs((fm - mid).total_seconds()) <= 12 * 3600 and _night(r)


def _solstice_day(r, june):
    d = _d(r["date"])
    local = solstice(d.year, june) + timedelta(minutes=r["offset_min"])
    return local.date() == d


def _negative(r):
    """Split the run into four equal-distance quarters: none slower than the one before, and the
    last faster than the first (so an evenly paced run doesn't count)."""
    q = r.get("fourths")
    return r["miles"] >= 2 and bool(q) and all(q[i] <= q[i - 1] for i in range(1, 4)) and q[3] < q[0]


def _mile_range(sp):
    return max(sp) - min(sp) if sp else None


def _consecutive_window(sp, n, ok):
    return any(ok(sp[i:i + n]) for i in range(len(sp) - n + 1))


# ---------------------------------------------------------------- personal settings

# Filled by compute() from Mile Marker's settings and plans before the rules run.
CTX = {"birthday": None, "home_tz": None, "home": None, "home_place": None, "places": [], "planned": {}, "first": None}


def _mm_files():
    out = [os.path.join(MM_DATA, "settings.json")]
    try:
        out += [os.path.join(MM_DATA, "plans", n) for n in os.listdir(os.path.join(MM_DATA, "plans")) if n.endswith(".json")]
    except OSError:
        pass
    return out


def _mm_mtime():
    """Latest change to Mile Marker's settings or plans (a deleted plan changes the folder)."""
    times = []
    for f in _mm_files() + [os.path.join(MM_DATA, "plans")]:
        try:
            times.append(os.path.getmtime(f))
        except OSError:
            pass
    return max(times, default=0)


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def place_of(r):
    return ", ".join(x for x in (r.get("city"), r.get("state")) if x) or None


def _home_point(runs, place):
    """(place, point): the middle of the starts in your home place. Without one set, or if it
    isn't among your runs, the place you run from most."""
    starts = [r for r in runs if r.get("start_ll") and place_of(r)]
    if not starts:
        return None, None
    if not place or not any(place_of(r) == place for r in starts):
        place = Counter(place_of(r) for r in starts).most_common(1)[0][0]
    pts = np.array([r["start_ll"] for r in starts if place_of(r) == place])
    return place, [float(np.median(pts[:, 0])), float(np.median(pts[:, 1]))]


def load_context(runs):
    """Settings and planned workouts from Mile Marker, plus the derived home point."""
    st = _read_json(os.path.join(MM_DATA, "settings.json")) or {}
    bd = st.get("birthday")
    bd = bd if isinstance(bd, str) and re.fullmatch(r"(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", bd) else None
    tz = None
    if isinstance(st.get("home_tz"), str):
        try:
            tz = ZoneInfo(st["home_tz"])
        except Exception:
            tz = None
    planned = defaultdict(int)   # date -> planned workouts marked done that day
    for f in _mm_files()[1:]:
        p = _read_json(f)
        if not isinstance(p, dict) or p.get("is_sample") or not isinstance(p.get("workouts"), dict):
            continue
        for w in p["workouts"].values():
            if (isinstance(w, dict) and w.get("plan_week") is not None and w.get("status") == "done"
                    and not w.get("deleted") and w.get("type") != "rest" and isinstance(w.get("scheduled_date"), str)):
                planned[w["scheduled_date"]] += 1
    runs = sorted(runs, key=lambda r: r["start"])
    home_place, home = _home_point(runs, st.get("home_place"))
    CTX.update(birthday=bd, home_tz=tz, home=home, home_place=home_place, planned=dict(planned),
               first=runs[0]["date"] if runs else None,
               places=[{"place": k, "runs": n} for k, n in Counter(place_of(r) for r in runs if r.get("start_ll") and place_of(r)).most_common()])
    return CTX


def _on_day(year, mmdd):
    """That month and day in a year; Feb 29 falls back to Feb 28 outside leap years."""
    m, d = int(mmdd[:2]), int(mmdd[3:])
    while True:
        try:
            return date(year, m, d)
        except ValueError:
            d -= 1


def _half_birthday(year):
    m, d = int(CTX["birthday"][:2]), int(CTX["birthday"][3:])
    m = (m + 5) % 12 + 1
    return _on_day(year, "%02d-%02d" % (m, d))


def _birthday_years(rs):
    return len({_d(r["date"]).year for r in rs if _d(r["date"]) == _on_day(_d(r["date"]).year, CTX["birthday"])})


def _birthday_week(rs):
    """Most days run in any one birthday week: the birthday and 3 days either side."""
    best = Counter()
    for r in rs:
        d = _d(r["date"])
        for y in (d.year - 1, d.year, d.year + 1):
            if abs((d - _on_day(y, CTX["birthday"])).days) <= 3:
                best[(y, d)] = 1
    per_year = Counter(y for y, _ in best)
    return max(per_year.values(), default=0)


def _anniversaries(rs):
    first = _d(CTX["first"])
    return len({_d(r["date"]).year for r in rs
                if _d(r["date"]).year > first.year and _d(r["date"]) == _on_day(_d(r["date"]).year, CTX["first"][5:])})


def _away(r):
    h = CTX["home"]
    return bool(h and r.get("start_ll")) and float(_haversine(h[0], h[1], *r["start_ll"])) >= TRAVEL_MILES * MILE


def _training_on_tour(rs):
    return sum(CTX["planned"].get(day, 0) for day in {r["date"] for r in rs if _away(r)})


def _jet_lag(r):
    """In a different time zone from home, and between 11 PM and 6 AM at home when you start."""
    start = _dt(r["utc"])
    home = start.astimezone(CTX["home_tz"])
    return home.utcoffset().total_seconds() / 60 != r["offset_min"] and (home.hour >= 23 or home.hour < 6)


def _pl(r, key, default=None):
    return (r.get("places") or {}).get(key, default)


def _in_season(r, start, end):
    """Month-day within [start, end], both "MM-DD"."""
    return start <= r["date"][5:] <= end


def _boroughs_by(rs, key):
    """Most boroughs run in within one period (key gives the period of a date)."""
    seen = defaultdict(set)
    for r in rs:
        seen[key(_d(r["date"]))].update(_pl(r, "boroughs", []))
    return seen


def NEEDS(rule, *keys):
    """A rule that can't be worked out until these settings exist; the page says which."""
    words = {"birthday": "Add your birthday in Settings.", "home_tz": "Set your home time zone in Settings.",
             "home": "Needs runs with GPS to find home.", "first": "Needs a run first."}
    return {**rule, "needs": lambda: next((words[k] for k in keys if not CTX.get(k)), None)}


# ---------------------------------------------------------------- the rules

def RUN(test, unit="runs"):
    return {"kind": "run", "test": test, "unit": unit}


def HIST(value, unit, lower=False):
    return {"kind": "history", "value": value, "unit": unit, "lower": lower}


def _best_time(key):
    return lambda rs: min((r["best"][key] for r in rs if r.get("best") and r["best"].get(key)), default=None)


def _sum(field):
    return lambda rs: sum(r.get(field) or 0 for r in rs)


def _max(fn):
    return lambda rs: max((fn(r) for r in rs), default=0)


R = {}
# Single-run distances; the catalog's target_miles thresholds are restated here.
for aid, mi in (("single_1_6", 1.6), ("single_3", 3), ("single_6", 6), ("single_10", 10), ("single_13_1", 13.1),
                ("single_20", 20), ("single_26_2", 26.2)):
    R[aid] = {"kind": "history", "value": _max(_mi2), "unit": "mi", "target": mi}

R.update({
    "streak_3_days": HIST(_day_streak, "days"), "streak_5_days": HIST(_day_streak, "days"),
    "weekday_all_7": HIST(_rolling_full_set, "days"),
    "weekly_3x_3weeks": HIST(lambda rs: _consec_weeks(rs, 3), "weeks"),
    "weekly_3x_6weeks": HIST(lambda rs: _consec_weeks(rs, 3), "weeks"),
    "weekly_4x_4weeks": HIST(lambda rs: _consec_weeks(rs, 4), "weeks"),
    "weekly_5x_5weeks": HIST(lambda rs: _consec_weeks(rs, 5), "weeks"),
    "weekly_once_year": HIST(lambda rs: _consec_weeks(rs, 1), "weeks"),
    "weekly_3x_3months": HIST(lambda rs: _consec_months_all_weeks(rs, 3), "months"),
    "weekly_3x_6months": HIST(lambda rs: _consec_months_all_weeks(rs, 3), "months"),
    "monthly_every_weekend": HIST(_weekend_month, "months"),
    "distance_growth_3months": HIST(_growth, "months"), "distance_growth_6months": HIST(_growth, "months"),
    "monday_5": HIST(lambda rs: _consec_weekday(rs, 0), "Mondays"),
    "friday_5": HIST(lambda rs: _consec_weekday(rs, 4), "Fridays"),
    "wednesday_10": HIST(lambda rs: len({r["date"] for r in rs if _d(r["date"]).weekday() == 2}), "Wednesdays"),
    "saturday_10": HIST(lambda rs: len({r["date"] for r in rs if _d(r["date"]).weekday() == 5}), "Saturdays"),
    "sunday_10": HIST(lambda rs: len({r["date"] for r in rs if _d(r["date"]).weekday() == 6}), "Sundays"),
    "all_weekdays_month": HIST(_working_weeks, "weeks"),
    "calendar_month_dates_1_31": HIST(lambda rs: len({_d(r["date"]).day for r in rs}), "dates"),
    "clock_6am_9pm": HIST(lambda rs: len({_dt(r["start"]).hour for r in rs if 6 <= _dt(r["start"]).hour <= 21}), "hours"),
    "clock_24_hours": HIST(lambda rs: len({_dt(r["start"]).hour for r in rs}), "hours"),
    "runs_100": HIST(len, "runs"), "runs_500": HIST(len, "runs"), "runs_1000": HIST(len, "runs"),
    "active_weeks_50": HIST(lambda rs: len({_mon(_d(r["date"])) for r in rs}), "weeks"),
    "active_months_12": HIST(_consec_months_with_runs, "months"),
    "active_years_3": HIST(_consec_years, "years"), "active_years_5": HIST(_consec_years, "years"),
    "comeback_30days": HIST(_gap, "days"), "comeback_90days": HIST(_gap, "days"),
    # Performance (no PRs yet)
    "negative_split_once": RUN(_negative), "negative_split_three": RUN(_negative),
    "even_steven": RUN(lambda r: r["miles"] >= 5 and len(r.get("mile_splits") or []) >= 5 and _mile_range(r["mile_splits"]) <= 10),
    "perfectly_even": RUN(lambda r: _consecutive_window(r.get("mile_splits") or [], 5, lambda w: max(w) - min(w) <= 5)),
    "fast_finish": RUN(lambda r: len(r.get("mile_splits") or []) >= 2 and all(r["mile_splits"][-1] < x for x in r["mile_splits"][:-1])),
    "crescendo_3": RUN(lambda r: _consecutive_window(r.get("mile_splits") or [], 3, lambda w: w[0] > w[1] > w[2])),
    "perfect_mile": RUN(lambda r: any(max(q) - min(q) <= 5 for q in r.get("quarters") or [])),
    "last_gasp": RUN(lambda r: r.get("best_km_s") is not None and r["last_km_s"] <= r["best_km_s"] + 0.5),
    "metronome": RUN(lambda r: len(r.get("km_splits") or []) >= 5 and r["moving_s"] > 0 and all(
        abs(s / (r["moving_s"] / (r["miles"] * MILE / 1000)) - 1) <= 0.05 for s in r["km_splits"])),
    "tortoise_hare": RUN(lambda r: any(b - a <= -120 for a, b in zip((r.get("mile_splits") or [])[:-1], (r.get("mile_splits") or [])[1:]))),
    # Random & Quirky
    "make_a_wish": RUN(lambda r: _near_clock(_dt(r["finish"]), 11, 11, twelve=True)),
    "high_noon": RUN(lambda r: _near_clock(_dt(r["finish"]), 12, 0)),
    "witching_hour": RUN(lambda r: _dt(r["start"]).hour == 0),
    "midnight_express": RUN(lambda r: _near_clock(_dt(r["start"]), 0, 0)),
    "double_digits": RUN(lambda r: _near_clock(_dt(r["finish"]), 10, 10, twelve=True)),
    "1234": RUN(lambda r: _near_clock(_dt(r["finish"]), 0, 34, twelve=True)),
    "pi_day": RUN(lambda r: r["date"][5:] == "03-14" and _near_mi(r, 3.14)),
    "tau_day": RUN(lambda r: r["date"][5:] == "06-28" and _near_mi(r, 6.28)),
    "prime_time": RUN(lambda r: _is_prime(int(_mi2(r))) and _is_prime(round(_mi2(r) * 100) % 100)
                      and _is_prime(int(round(r["moving_s"]) // 60)) and _is_prime(int(round(r["moving_s"]) % 60))),
    "flat_numbers": RUN(lambda r: round(_mi2(r) * 100) % 100 == 0 and _mi2(r) >= 1 and int(round(r["moving_s"])) % 60 == 0),
    "pi_run": RUN(lambda r: _near_mi(r, 3.14)), "lucky_sevens": RUN(lambda r: _near_mi(r, 7.77)),
    "devils_distance": RUN(lambda r: _near_mi(r, 6.66)), "eleven_eleven": RUN(lambda r: _near_mi(r, 11.11)),
    "friday_thirteenth": RUN(lambda r: _d(r["date"]).day == 13 and _d(r["date"]).weekday() == 4),
    "leap_day": RUN(lambda r: r["date"][5:] == "02-29"), "new_years_day": RUN(lambda r: r["date"][5:] == "01-01"),
    "halloween": RUN(lambda r: r["date"][5:] == "10-31"), "christmas": RUN(lambda r: r["date"][5:] == "12-25"),
    "may_the_fourth": RUN(lambda r: r["date"][5:] == "05-04"), "groundhog_day": RUN(lambda r: r["date"][5:] == "02-02"),
    "palindrome_date": RUN(lambda r: r["date"].replace("-", "") == r["date"].replace("-", "")[::-1]),
    "full_moon_fever": RUN(_full_moon), "dark_side": RUN(_dark_side),
    "state_lines": HIST(lambda rs: len({r["state"] for r in rs if r.get("state") and r.get("country_code") == "us"}), "states"),
    "international_runner": HIST(lambda rs: len({r["country_code"] or r["country"] for r in rs if r.get("country_code") or r.get("country")}), "countries"),
    "double_trouble": HIST(lambda rs: sum(1 for n in Counter(r["date"] for r in rs).values() if n >= 2), "days"),
    "triple_threat": HIST(lambda rs: sum(1 for n in Counter(r["date"] for r in rs).values() if n >= 3), "days"),
    "night_owl": RUN(lambda r: _dt(r["start"]).hour >= 22), "early_bird": RUN(lambda r: _dt(r["start"]).hour < 6),
    # Weather (Smashrun's one reading per run)
    "singing_in_the_rain": RUN(_rain), "rain_check": RUN(_rain), "snow_problem": RUN(_snow), "snowflake": RUN(_snow),
    "cold_feet": RUN(lambda r: r.get("temp_f") is not None and r["temp_f"] <= 32),
    "deep_freeze": RUN(lambda r: r.get("temp_f") is not None and r["temp_f"] <= 15),
    "wind_beneath_my_wings": RUN(lambda r: r.get("wind_mph") is not None and r["wind_mph"] >= 20),
    "perfect_weather": RUN(_perfect),
    "four_seasons": HIST(_four_seasons, "seasons"),
    "weather_or_not": HIST(lambda rs: any(map(_rain, rs)) + any(map(_snow, rs)) + any(
        r.get("temp_f") is not None and r["temp_f"] <= 32 for r in rs), "kinds"),
    "spring_has_sprung": HIST(lambda rs: _first_nice_in(rs, (3, 4, 5)), "years"),
    "autumn_is_calling": HIST(lambda rs: _first_nice_in(rs, (9, 10, 11)), "years"),
    # Place, seasonal & life
    "turkey_trot": RUN(lambda r: _d(r["date"]) == thanksgiving(_d(r["date"]).year)),
    "twilight_zone": RUN(lambda r: r["date"][5:7] == "12" and 26 <= _d(r["date"]).day <= 30),
    "intervention": HIST(_intervention, "years"),
    "first_day_summer": RUN(lambda r: _solstice_day(r, True)),
    "longest_day": RUN(lambda r: _solstice_day(r, True) and bool(r.get("sunrise"))
                       and _dt(r["start"]) < _dt(r["sunrise"]) < _dt(r["finish"])),
    "shortest_day": RUN(lambda r: _solstice_day(r, False)),
    "dog_days": HIST(_dog_days, "runs"),
    "september_reset": RUN(lambda r: r["date"][5:] == "09-01"),
    "spooky_season": RUN(lambda r: r["date"][5:7] == "10" and _d(r["date"]).day >= 13),
    "sunday_scaries": RUN(lambda r: _d(r["date"]).weekday() == 6 and _dt(r["start"]).hour >= 19),
    "too_damn_hot": RUN(lambda r: r.get("temp_f") is not None and r["temp_f"] >= 95),
    "who_does_this": RUN(lambda r: 2 <= _dt(r["start"]).hour < 4),
    "lunch_break": RUN(lambda r: _d(r["date"]).weekday() < 5 and 12 <= _dt(r["start"]).hour < 14),
    "dinner_can_wait": RUN(lambda r: 18 <= _dt(r["start"]).hour < 20),
    "the_commitment": HIST(lambda rs: _pair_days(rs, (12, 24), (12, 25)), ""),
    "no_days_off": HIST(lambda rs: _pair_days(rs, (12, 31), (1, 1), next_year=True), ""),
    "the_usual": HIST(_the_usual, ""),
    "date_night": RUN(lambda r: r["date"][5:] == "02-14"),
    # Elevation & race
    "hill_climber": HIST(_max(lambda r: r.get("gain_ft") or 0), "ft"),
    "vertical_mile": HIST(_max(lambda r: r.get("gain_ft") or 0), "ft"),
    "mountain_goat": HIST(_sum("gain_ft"), "ft"), "stairway_to_heaven": HIST(_sum("gain_ft"), "ft"),
    "what_goes_up": HIST(_max(lambda r: min(r.get("gain_ft") or 0, r.get("loss_ft") or 0)), "ft"),
    "king_of_the_hill": RUN(lambda r: bool(r.get("climb_mile"))),
    "race_day": HIST(lambda rs: sum(r["race"] for r in rs), "races"),
    "serial_racer": HIST(lambda rs: sum(r["race"] for r in rs), "races"),
    "triple_crown": HIST(_race_kinds, "distances"),
    "the_big_one": RUN(lambda r: r["race"] and r["miles"] * MILE >= 41500),
    "double_header": HIST(_double_header, "races"),
    # Route & travel
    "full_circle": RUN(lambda r: r.get("start_end_m") is not None and r["start_end_m"] <= 100),
    "long_way_home": RUN(lambda r: r.get("start_end_m") is not None and r["start_end_m"] >= 5 * MILE),
    "out_and_back": RUN(lambda r: (r.get("out_back") or 0) >= 0.7),
    # Levels are a count of bands (the catalog lists the bands themselves: 3 to 26 miles).
    "distance_band_firsts": {**HIST(_bands, "distances"), "levels": list(range(1, 25))},
    # New York places (smoothrun/places.py)
    "borough_hopper": HIST(lambda rs: len({b for r in rs for b in _pl(r, "boroughs", [])}), "boroughs"),
    "borough_call": HIST(lambda rs: max((len(v) for v in _boroughs_by(rs, lambda d: d.year).values()), default=0), "boroughs"),
    "concrete_jungle": HIST(lambda rs: max((len(v & {"Manhattan", "Brooklyn", "Queens"})
                                            for v in _boroughs_by(rs, _mon).values()), default=0), "boroughs"),
    "the_great_escape": RUN(lambda r: _pl(r, "start_borough") == "Manhattan"
                            and any(b != "Manhattan" for b in _pl(r, "boroughs", []))),
    "bridge_troll": HIST(lambda rs: len({b for r in rs for b in _pl(r, "bridges", [])}), "bridges"),
    "petal_pusher": RUN(lambda r: _pl(r, "central_park_m", 0) >= 800 and _in_season(r, "03-25", "04-30")),
    "fall_ing_for_it": RUN(lambda r: _pl(r, "central_park_m", 0) >= 800 and _in_season(r, "10-15", "11-20")),
    "reservoir_dogs": RUN(lambda r: bool(_pl(r, "reservoir_loop"))),
    "the_long_loop": RUN(lambda r: bool(_pl(r, "cp_loop"))),
    "waterfront_property": RUN(lambda r: _pl(r, "waterfront_m", 0) >= MILE),
    "tourist_season": RUN(lambda r: bool(_pl(r, "times_square"))),
    # Personal dates and travel (Mile Marker's Settings page and plans)
    "birthday_suit": NEEDS(HIST(_birthday_years, "birthdays"), "birthday"),
    "birthday_week": NEEDS(HIST(_birthday_week, "days"), "birthday"),
    "half_birthday": NEEDS(RUN(lambda r: _d(r["date"]) == _half_birthday(_d(r["date"]).year)), "birthday"),
    "anniversary_run": NEEDS(HIST(_anniversaries, "years"), "first"),
    "jetlag": NEEDS(RUN(_jet_lag), "home_tz"),
    "travel_training": NEEDS(HIST(_training_on_tour, "workouts"), "home"),
})
for aid, mins in (("mile_sub_12", 12), ("mile_sub_10", 10), ("mile_sub_9", 9), ("mile_sub_8", 8), ("mile_sub_7", 7),
                  ("mile_sub_6", 6)):
    R[aid] = {**HIST(_best_time("mile"), "time", lower=True), "target": mins * 60}
for aid, key, mins in (("5k_sub_30", "5k", 30), ("5k_sub_25", "5k", 25), ("5k_sub_20", "5k", 20), ("10k_sub_60", "10k", 60),
                       ("10k_sub_55", "10k", 55), ("half_sub_2h", "half", 120), ("half_sub_90", "half", 90)):
    R[aid] = {**HIST(_best_time(key), "time", lower=True), "target": mins * 60}
for n in (8, 10, 15, 20):
    R["monthly_%d_days" % n] = HIST(lambda rs: _per_month(rs, lambda g: len({r["date"] for r in g})), "days")
for n in (20, 30, 40, 50, 60, 70, 80, 90, 100):
    R["monthly_%d_miles" % n] = {**HIST(lambda rs: _per_month(rs, lambda g: sum(r["miles"] for r in g)), "mi"), "target": n}
for n in (10, 15, 20, 25, 30, 35, 40):
    R["weekly_%d_miles" % n] = {**HIST(lambda rs: _per_week(rs, lambda g: sum(r["miles"] for r in g)), "mi"), "target": n}

# Thresholds for rules whose catalog entry has no levels: (value needed), by id.
TARGETS = {
    "streak_3_days": 3, "streak_5_days": 5, "weekday_all_7": 7,
    "weekly_3x_3weeks": 3, "weekly_3x_6weeks": 6, "weekly_4x_4weeks": 4, "weekly_5x_5weeks": 5, "weekly_once_year": 52,
    "weekly_3x_3months": 3, "weekly_3x_6months": 6, "monthly_every_weekend": 1,
    "distance_growth_3months": 3, "distance_growth_6months": 6, "monday_5": 5, "friday_5": 5,
    "wednesday_10": 10, "saturday_10": 10, "sunday_10": 10, "all_weekdays_month": 4, "calendar_month_dates_1_31": 31,
    "clock_6am_9pm": 16, "clock_24_hours": 24, "runs_100": 100, "runs_500": 500, "runs_1000": 1000,
    "active_weeks_50": 50, "active_months_12": 12, "active_years_3": 3, "active_years_5": 5,
    "comeback_30days": 30, "comeback_90days": 90, "monthly_8_days": 8, "monthly_10_days": 10, "monthly_15_days": 15,
    "monthly_20_days": 20, "negative_split_three": 3, "four_seasons": 4, "weather_or_not": 3, "dog_days": 7,
    "the_commitment": 1, "no_days_off": 1, "the_usual": 1, "hill_climber": 1000, "vertical_mile": 5280,
    "mountain_goat": 5000, "stairway_to_heaven": 10000, "what_goes_up": 500, "race_day": 1, "triple_crown": 3,
    "double_header": 2, "borough_call": 5, "concrete_jungle": 3,
}


# Series: achievements where reaching a bigger one means you've reached the smaller ones. The page
# shows the highest earned and the next to go in each, and hides the rest.
SERIES = {
    "streak_3_days": "day_streak", "streak_5_days": "day_streak", "weekday_all_7": "day_streak",
    # Three times a week: 3 weeks, 6 weeks, 3 months, 6 months (a month in a row is 4-5 weeks).
    "weekly_3x_3weeks": ("3x_week", 1), "weekly_3x_6weeks": ("3x_week", 2),
    "weekly_3x_3months": ("3x_week", 3), "weekly_3x_6months": ("3x_week", 4),
    "weekly_4x_4weeks": ("4x_5x", 1), "weekly_5x_5weeks": ("4x_5x", 2),
    "distance_growth_3months": "growth", "distance_growth_6months": "growth",
    "runs_100": "runs", "runs_500": "runs", "runs_1000": "runs",
    "active_years_3": "years", "active_years_5": "years",
    "comeback_30days": "comeback", "comeback_90days": "comeback",
    "hill_climber": "climb_one", "vertical_mile": "climb_one",
    "mountain_goat": "climb_total", "stairway_to_heaven": "climb_total",
    "negative_split_once": "negative", "negative_split_three": "negative",
    "singing_in_the_rain": "rain", "rain_check": "rain", "snow_problem": "snow", "snowflake": "snow",
    "race_day": "races", "serial_racer": "races",
}


def series_of(aid):
    """(series key, position) for an achievement in a series, else (None, None). Without an
    explicit position, the page orders a series by target."""
    for pattern, key in ((r"total_", "total"), (r"single_", "single"), (r"monthly_\d+_miles", "month_miles"),
                         (r"weekly_\d+_miles", "week_miles"), (r"monthly_\d+_days", "month_days"),
                         (r"mile_sub_", "mile"), (r"5k_sub_", "5k"), (r"10k_sub_", "10k"), (r"half_sub_", "half")):
        if re.match(pattern, aid):
            return key, None
    got = SERIES.get(aid)
    return got if isinstance(got, tuple) else (got, None)


def _total(target):
    return {"kind": "history", "value": lambda rs: sum(r["miles"] for r in rs), "unit": "mi", "target": target}


# ---------------------------------------------------------------- evaluation

def _levels(entry, rule):
    if "levels" in rule:
        return list(rule["levels"])
    if entry["levels"]:
        return list(entry["levels"])
    if "target" in rule:
        return [rule["target"]]
    return [TARGETS.get(entry["id"], 1)]


def evaluate(catalog, runs):
    """One result per first-cut catalog entry: levels reached (with date and run) and progress."""
    runs = sorted(runs, key=lambda r: r["start"])
    out = []
    for entry in catalog:
        if not entry.get("first_cut"):
            continue
        rule = R.get(entry["id"])
        if rule is None and entry["id"].startswith("total_"):
            rule = _total(float(re.match(r"total_(\d+)", entry["id"]).group(1)))
        if rule is None:
            print("  achievements: no rule for %s" % entry["id"], flush=True)
            continue
        levels = _levels(entry, rule)
        earned = []
        missing = rule["needs"]() if "needs" in rule else None
        if missing:
            value = None
        elif rule["kind"] == "run":
            hits = [r for r in runs if rule["test"](r)]
            for lv in levels:
                if len(hits) >= lv:
                    earned.append({"at": lv, "date": hits[int(lv) - 1]["date"], "run": hits[int(lv) - 1]["id"]})
            value = len(hits)
        else:
            fn, lower = rule["value"], rule.get("lower", False)
            ok = (lambda v, t: v is not None and v < t) if lower else (lambda v, t: v is not None and v >= t - 1e-9)
            value = fn(runs) if runs else None
            for lv in levels:
                if not ok(value, lv):
                    break
                lo, hi = 1, len(runs)   # smallest prefix that reaches the level
                while lo < hi:
                    mid = (lo + hi) // 2
                    if ok(fn(runs[:mid]), lv):
                        hi = mid
                    else:
                        lo = mid + 1
                earned.append({"at": lv, "date": runs[lo - 1]["date"], "run": runs[lo - 1]["id"]})
        nxt = next((lv for lv in levels if not any(e["at"] == lv for e in earned)), None)
        series, step = series_of(entry["id"])
        out.append({"id": entry["id"], "series": series, "series_step": step, "first": levels[0],
                    "earned": earned, "levels": levels if len(levels) > 1 else [],
                    "value": value, "next": nxt, "unit": rule["unit"], "lower": rule.get("lower", False),
                    "needs": missing})
    return out


def load_catalog():
    with open(CATALOG) as fh:
        return json.load(fh)


def compute(root=None):
    """Recompute from the run copy and save mirror/achievements.json."""
    index = mirror.load(root)
    rows = [r for r in index["runs"] if not r.get("removed") and (r.get("date") or "") >= ACH_SINCE
            and r.get("distance_m", 0) > 0 and (r.get("type") or "running") == "running"]
    catalog = load_catalog()
    runs = load_facts(rows, root)
    load_context(runs)
    results = evaluate(catalog, runs)
    by_id = {e["id"]: e for e in catalog}
    for r in results:
        e = by_id[r["id"]]
        r.update(name=e["name"], group=e["group"], description=e["description"])
    doc = {"version": RESULT_VERSION, "facts_version": FACTS_VERSION, "since": ACH_SINCE,
           "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "synced_at": index.get("synced_at"), "runs": len(rows),
           # For Mile Marker's Settings page: where you run, and which counts as home.
           "places": CTX["places"], "home_place": CTX["home_place"], "anniversary": CTX["first"],
           "later": [{"id": e["id"], "name": e["name"], "group": e["group"], "description": e["description"],
                      "needs": e.get("later")} for e in catalog if not e.get("first_cut")],
           "achievements": results}
    mirror._write_atomic(os.path.join(root or mirror.MIRROR_DIR, "achievements.json"), doc)
    return doc


def load(root=None):
    """The saved results, recomputed if missing, older than the last sync, or from older code."""
    path = os.path.join(root or mirror.MIRROR_DIR, "achievements.json")
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (FileNotFoundError, ValueError):
        doc = None
    index = mirror.load(root)
    if (not doc or doc.get("version") != RESULT_VERSION or doc.get("facts_version") != FACTS_VERSION
            or doc.get("synced_at") != index.get("synced_at")
            or os.path.getmtime(CATALOG) > os.path.getmtime(path) or _mm_mtime() > os.path.getmtime(path)):
        doc = compute(root)
    return doc


if __name__ == "__main__":
    import sys
    doc = compute(sys.argv[1] if len(sys.argv) > 1 else None)
    got = [a for a in doc["achievements"] if a["earned"]]
    print("%d runs; %d of %d achievements earned" % (doc["runs"], len(got), len(doc["achievements"])))
    for a in got:
        print("  %s (%s): %s" % (a["name"], a["earned"][-1]["date"], a["description"]))
