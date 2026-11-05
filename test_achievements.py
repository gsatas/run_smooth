"""Checks smoothrun/achievements.py with made-up runs: python test_achievements.py"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoothrun import achievements as A  # noqa: E402

MILE = A.MILE


def run(i, when, miles=3.0, moving=1800, elapsed=None, **kw):
    """A facts row like achievements.facts() makes, for a run starting at `when` (local, EST)."""
    start = datetime.fromisoformat(when)
    el = elapsed if elapsed is not None else moving
    r = {"id": i, "updated": "u", "start": start.isoformat(), "date": start.date().isoformat(),
         "utc": (start + timedelta(hours=5)).replace(tzinfo=timezone.utc).isoformat(), "offset_min": -300,
         "finish": (start + timedelta(seconds=el)).isoformat(), "miles": miles, "moving_s": moving, "elapsed_s": el,
         "temp_f": None, "humidity": None, "wind_mph": None, "weather": None, "gain_ft": 50, "loss_ft": 50,
         "race": False, "treadmill": False, "city": "New York", "state": "New York", "country": "USA", "country_code": "us"}
    r.update(kw)
    return r


CAT = A.load_catalog()


def results(runs):
    return {x["id"]: x for x in A.evaluate(CAT, runs)}


def earned(res, aid):
    return [(e["at"], e["date"], e["run"]) for e in res[aid]["earned"]]


# --- astronomy
assert A.thanksgiving(2026) == date(2026, 11, 26) and A.thanksgiving(2027) == date(2027, 11, 25)
assert A.solstice(2026, True).date() == date(2026, 6, 21) and A.solstice(2026, False).date() == date(2026, 12, 21)
nov3 = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)
assert A.nearest_moon(nov3, full=False).date() == date(2026, 11, 9), A.nearest_moon(nov3, False)
assert A.nearest_moon(nov3, full=True).date() in (date(2026, 10, 26), date(2026, 11, 24))
rise, sett = A.sun_times(date(2026, 11, 3), 40.8012, -73.9477, timedelta(hours=-5))
assert abs((rise - datetime(2026, 11, 3, 6, 29)).total_seconds()) < 240, rise     # Smashrun: 06:29
assert abs((sett - datetime(2026, 11, 3, 16, 50)).total_seconds()) < 240, sett    # Smashrun: 16:50

# --- running totals and single-run distances: earned on the run that crossed the line
rs = [run(1, "2026-03-01T08:00", 2.5), run(2, "2026-03-02T08:00", 2.0), run(3, "2026-03-05T08:00", 13.1)]
res = results(rs)
assert earned(res, "total_6") == [(6.0, "2026-03-05", 3)], earned(res, "total_6")
assert earned(res, "single_13_1") == [(13.1, "2026-03-05", 3)]
assert not res["single_20"]["earned"] and res["single_20"]["value"] == 13.1

# --- streaks and weeks
rs = [run(i, (date(2026, 1, 5) + timedelta(days=i)).isoformat() + "T07:00") for i in range(7)]   # Mon 5th to Sun 11th
res = results(rs)
assert earned(res, "streak_3_days") == [(3, "2026-01-07", 2)]
assert earned(res, "weekday_all_7") == [(7, "2026-01-11", 6)]
assert earned(res, "all_weekdays_month") == [] and res["all_weekdays_month"]["value"] == 1
assert res["weekly_5x_5weeks"]["value"] == 1
gap = rs + [run(9, "2026-02-20T07:00")]   # Jan 11 → Feb 20: 39 days without a run
res = results(gap)
assert earned(res, "comeback_30days") == [(30, "2026-02-20", 9)] and not res["comeback_90days"]["earned"]

# --- per-run levels count runs; history levels come from the first run that reached them
owls = [run(i, "2026-05-%02dT22:30" % (i + 1)) for i in range(6)]
res = results(owls)
assert [e[0] for e in earned(res, "night_owl")] == [1, 3, 5], earned(res, "night_owl")
assert earned(res, "night_owl")[1][2] == 2

# --- clock, date and number quirks
res = results([run(1, "2026-03-14T10:39:00", 3.14, moving=1920, elapsed=1920 + 11)])   # finishes 11:11:11
assert res["pi_day"]["earned"] and res["pi_run"]["earned"] and res["make_a_wish"]["earned"]
res = results([run(1, "2026-04-01T07:00", 3.17, moving=37 * 60 + 11)])
assert res["prime_time"]["earned"]
res = results([run(1, "2026-04-01T07:00", 3.00, moving=1800)])
assert res["flat_numbers"]["earned"] and not res["prime_time"]["earned"]
assert results([run(1, "2030-03-02T07:00")])["palindrome_date"]["earned"]          # 20300302
assert not results([run(1, "2026-03-02T07:00")])["palindrome_date"]["earned"]
assert results([run(1, "2026-11-26T07:00")])["turkey_trot"]["earned"]
assert results([run(1, "2026-12-21T07:00")])["shortest_day"]["earned"]

# --- moon: night run on the new-moon date counts for Dark Side; a daytime one doesn't
night = run(1, "2026-11-09T19:00", sunrise="2026-11-09T06:35:00", sunset="2026-11-09T16:44:00")
day = run(2, "2026-11-09T12:00", sunrise="2026-11-09T06:35:00", sunset="2026-11-09T16:44:00")
assert results([night])["dark_side"]["earned"] and not results([day])["dark_side"]["earned"]

# --- performance from splits and best efforts
fast = run(1, "2026-06-01T07:00", 5.2, mile_splits=[600, 598, 599, 597, 600], fourths=[760, 755, 755, 740],
           best={"mile": 590, "5k": 1750, "10k": None, "half": None}, quarters=[[150, 150, 149, 151]],
           km_splits=[372] * 8)
res = results([fast])
assert res["even_steven"]["earned"] and res["perfectly_even"]["earned"] and res["perfect_mile"]["earned"]
assert res["mile_sub_10"]["earned"] and not res["mile_sub_9"]["earned"] and res["mile_sub_10"]["value"] == 590
assert res["5k_sub_30"]["earned"] and not res["5k_sub_25"]["earned"]
assert res["negative_split_once"]["earned"] and not res["crescendo_3"]["earned"]
assert results([run(1, "2026-06-01T07:00", 3.1, mile_splits=[700, 650, 600])])["crescendo_3"]["earned"]
# negative split: a quarter that fades fails it, and so does a perfectly even run
assert not results([run(1, "2026-06-01T07:00", 3.1, fourths=[700, 720, 690, 680])])["negative_split_once"]["earned"]
assert not results([run(1, "2026-06-01T07:00", 3.1, fourths=[700, 700, 700, 700])])["negative_split_once"]["earned"]
assert not results([run(1, "2026-06-01T07:00", 1.5, fourths=[700, 690, 680, 670])])["negative_split_once"]["earned"]
assert results([run(1, "2026-06-01T07:00", 3.1, mile_splits=[800, 670])])["tortoise_hare"]["earned"]

# --- weather and heat (Smashrun's one reading per run)
hot = [run(i, "2026-07-%02dT15:00" % (i + 1), temp_f=88) for i in range(7)]
res = results(hot)
assert earned(res, "dog_days") == [(7, "2026-07-07", 6)] and not res["too_damn_hot"]["earned"]
assert results([run(1, "2026-07-01T15:00", temp_f=96)])["too_damn_hot"]["earned"]
nice = run(1, "2026-04-10T07:00", temp_f=60, humidity=50, weather="clear")
res = results([nice])
assert res["perfect_weather"]["earned"] and res["spring_has_sprung"]["earned"]
assert not results([run(1, "2026-04-10T07:00", temp_f=60, humidity=50, weather="rain")])["perfect_weather"]["earned"]

# --- races
races = [run(1, "2026-03-01T08:00", 3.11, race=True), run(2, "2026-03-05T08:00", 6.21, race=True),
         run(3, "2026-04-01T08:00", 13.1, race=True)]
res = results(races)
assert earned(res, "race_day") == [(1, "2026-03-01", 1)] and earned(res, "double_header") == [(2, "2026-03-05", 2)]
assert earned(res, "triple_crown") == [(3, "2026-04-01", 3)] and not res["serial_racer"]["earned"]

# --- facts() on a made-up activity: a straight out-and-back mile each way with one pause
n = 400
lat = [40.0 + 0.0145 * (i / 200 if i <= 200 else (400 - i) / 200) for i in range(n + 1)]
clock = [i * 3.0 + (60 if i > 100 else 0) for i in range(n + 1)]   # a 60 s pause before point 101
dist = [abs(la - 40.0) / 0.0145 * 1.6113 if i <= 200 else 1.6113 + (1.6113 - abs(la - 40.0) / 0.0145 * 1.6113)
        for i, la in enumerate(lat)]
act = {"activityId": 9, "startDateTimeLocal": "2026-05-02T07:00:00-04:00", "distance": dist[-1], "duration": n * 3.0,
       "recordingKeys": ["clock", "distance", "latitude", "longitude"], "recordingValues": [clock, dist, lat, [-73.96] * (n + 1)],
       "pauseIndexes": [101], "isRace": 0}
f = A.facts(act)
assert abs(f["elapsed_s"] - (n * 3 + 60)) < 1e-6 and f["finish"] == "2026-05-02T07:21:00"
assert len(f["mile_splits"]) == 2 and abs(sum(f["mile_splits"]) - 2 * MILE / (1611.3 / 600)) < 5, f["mile_splits"]
assert f["out_back"] > 0.9 and f["start_end_m"] < 5 and f["sunrise"].startswith("2026-05-02T05:")

# --- series group the ladders
assert A.series_of("total_6") == ("total", None) and A.series_of("monthly_20_miles") == ("month_miles", None)
assert A.series_of("mile_sub_10") == ("mile", None) and A.series_of("pi_day") == (None, None)
assert [A.series_of(i) for i in ("weekly_3x_6weeks", "weekly_3x_3months")] == [("3x_week", 2), ("3x_week", 3)]

# --- Five-Day Regular needs 5 weeks in a row of 5 runs
five = [run(w * 7 + k, (date(2026, 2, 2) + timedelta(days=w * 7 + k)).isoformat() + "T07:00") for w in range(5) for k in range(5)]
assert not results(five[:20])["weekly_5x_5weeks"]["earned"] and results(five)["weekly_5x_5weeks"]["earned"]

# --- personal dates and travel, from Mile Marker's settings and plans
import json, tempfile  # noqa: E401,E402
mm = tempfile.mkdtemp()
os.makedirs(os.path.join(mm, "plans"))
A.MM_DATA = mm
nyc, boston = [40.80, -73.95], [42.36, -71.06]
rs = [run(1, "2026-03-10T07:00", start_ll=nyc), run(2, "2026-03-12T07:00", start_ll=nyc),
      run(3, "2026-03-14T07:00", start_ll=nyc), run(4, "2026-09-14T07:00", start_ll=nyc),
      run(5, "2026-10-02T07:00", start_ll=boston, city="Boston", state="Massachusetts"),
      run(6, "2027-03-10T07:00", start_ll=nyc), run(7, "2027-03-12T07:00", start_ll=nyc)]
A.load_context(rs)
res = results(rs)
assert res["birthday_suit"]["needs"] == "Add your birthday in Settings." and not res["birthday_suit"]["earned"]
assert res["jetlag"]["needs"] and res["anniversary_run"]["needs"] is None
assert earned(res, "anniversary_run") == [(1, "2027-03-10", 6)]          # first run: 2026-03-10
with open(os.path.join(mm, "settings.json"), "w") as fh:
    json.dump({"birthday": "03-12", "home_tz": "America/New_York"}, fh)
plan = {"workouts": {"a": {"plan_week": 3, "status": "done", "type": "easy", "scheduled_date": "2026-10-02"},
                     "b": {"plan_week": 3, "status": "done", "type": "rest", "scheduled_date": "2026-10-02"},
                     "c": {"plan_week": 1, "status": "done", "type": "easy", "scheduled_date": "2026-03-12"}}}
with open(os.path.join(mm, "plans", "plan-x.json"), "w") as fh:
    json.dump(plan, fh)
A.load_context(rs)
assert A.CTX["home"] == nyc                                                # most runs start in New York
res = results(rs)
assert earned(res, "birthday_suit") == [(1, "2026-03-12", 2), (2, "2027-03-12", 7)]
assert earned(res, "birthday_week") == [(3, "2026-03-14", 3)] and res["birthday_week"]["value"] == 3   # 9th to 15th
assert not res["half_birthday"]["earned"]                                 # Sep 12; the run was the 14th
assert earned(res, "travel_training") == [(1, "2026-10-02", 5)]          # Boston is 190 mi away; rest days don't count
assert A._half_birthday(2026) == date(2026, 9, 12)
A.CTX["birthday"] = "08-31"
assert A._half_birthday(2027) == date(2027, 2, 28) and A._on_day(2027, "02-29") == date(2027, 2, 28)
# Jet lag: 9 AM in Paris (UTC+1) is 3 AM in New York
paris = run(8, "2026-11-20T09:00", offset_min=60, utc="2026-11-20T08:00:00+00:00")
half = run(9, "2026-09-12T07:00", start_ll=nyc)
A.load_context(rs + [half, paris])
assert earned(results(rs + [half]), "half_birthday") == [(1, "2026-09-12", 9)]
assert results(rs + [paris])["jetlag"]["earned"] and not results(rs)["jetlag"]["earned"]
# 5:30 AM in Paris is 11:30 PM the night before in New York (counts); 1 PM in Paris is 7 AM there (doesn't)
late = run(10, "2026-11-20T05:30", offset_min=60, utc="2026-11-20T04:30:00+00:00")
day = run(11, "2026-11-20T13:00", offset_min=60, utc="2026-11-20T12:00:00+00:00")
assert A._jet_lag(late) and not A._jet_lag(day)

print("all achievement tests passed")
