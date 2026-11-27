"""Base status for the beta interface: one statement of the fire situation at an area, the items behind it,
and a 48 h timeline on one time axis.

RULES ARE A DRAFT. They are placeholders to test the layout with range control and the fire hall, not agreed
procedure; every threshold is in RULES below so the session that agrees them can change them in one place.

Levels: 0 Normal, 1 Heightened, 2 High. The status is the highest level among the attention items.
Inputs, all already collected by FWX: the area panel (CWFIS daily danger, Open-Meteo hourly weather and
FWX-calculated hourly FWI), ECCC alerts, FIRMS hotspots near the area, and the smoke outlook.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LEVELS = ["Normal", "Heightened", "High"]
DANGER_ORDER = ["Low", "Moderate", "High", "Very High", "Extreme"]

RULES = {
    "hours": 48,                 # timeline length
    "wind_hours": 24,            # how far ahead wind and RH items look
    "gust_heightened": 50,       # km/h
    "gust_high": 70,
    "shift_deg": 60,             # a wind shift: direction turns this much within shift_window h...
    "shift_window": 3,
    "shift_min_ws": 15,          # ...with at least this much wind, km/h
    "rh_low": 25,                # %, with danger Moderate or above
    # extreme-condition hours: 1 crossover (RH % at or below temperature °C), 2 "30/30" (temp >= 30 °C and
    # RH <= 30 %), 3 "30/30/30" (30/30 plus sustained wind >= 30 km/h)
    "x_temp": 30, "x_rh": 30, "x_wind": 30,
    "hotspot_recent_h": 6,       # a hotspot inside the area this recent counts
    # ---- seasons, from snow on the ground (CaLDAS, mean over the area's box) and the green-up dates ----
    "winter_cover_pct": 75,      # winter: at least this share of the area under 1 cm+ of snow...
    "winter_mean_cm": 2,         # ...and at least this much on average
    "patchy_cover_pct": 25,      # patchy snow: between this and winter
    "green_up": "05-20",         # MM-DD: grass greens up about here (an area can set "season": {"green_up", "cured"})
    "cured": "09-15",            # MM-DD: grass is cured (dormant) from about here
    # grass season (snow-free, cured grass): dry and windy hours in the next 24 h
    "grass_rh_watch": 35, "grass_ws_watch": 20,   # Heightened: RH at or below and wind at or above
    "grass_rh_high": 25, "grass_ws_high": 30,     # High
}
SEASONS = {
    "winter": ("Winter", "❄"),
    "patchy": ("Patchy snow", "◐"),
    "grass": ("Grass season", "▲"),
    "growing": ("Fire season", "●"),
    "unknown": ("Season unknown", "?"),
}
# FWI -> approximate class; kept for reference only. The timeline uses the CWFIS daily class.
FWI_BANDS = [(5, "Low"), (10, "Moderate"), (20, "High"), (30, "Very High"), (float("inf"), "Extreme")]


def _dclass(fwi):
    if fwi is None:
        return None
    return next(lab for top, lab in FWI_BANDS if fwi < top)


def _xover(t, rh, ws, R=RULES):
    """0 none, 1 crossover, 2 30/30, 3 30/30/30 for one hour."""
    if t is None or rh is None:
        return 0
    if t >= R["x_temp"] and rh <= R["x_rh"]:
        return 3 if (ws or 0) >= R["x_wind"] else 2
    return 1 if rh <= t else 0


X_NAMES = {1: "Crossover", 2: "30/30", 3: "30/30/30"}


PREVIEW_TEXT = {
    "winter": "Snow covers 95% of the area, 14 cm on average. Fire risk minimal while it lasts.",
    "patchy": "Snow covers 40% of the area, 1.5 cm on average. Exposed cured grass can burn on dry, windy days.",
    "grass": "Snow-free since Apr 2 and the grass is cured. Fires start and spread fast on dry, windy days, "
             "often before the daily fire danger catches up.",
    "growing": "Snow-free since Apr 2. Grass is green or greening up; CWFIS fire danger applies.",
}


def season(area, snow, today, R=RULES, force=None):
    """(mode, text): from snow cover over the area, then the green-up / cured dates for snow-free ground.
    force: a mode to preview (the beta's ?season=), with sample text."""
    if force in PREVIEW_TEXT:
        return force, PREVIEW_TEXT[force]
    md = today.strftime("%m-%d")
    dates = area.get("season") or {}
    green, cured = dates.get("green_up", R["green_up"]), dates.get("cured", R["cured"])
    growing = green <= md < cured
    if snow:
        cov, mean = snow["covered_pct"], snow["mean_cm"]
        snow_txt = f"snow covers {cov}% of the area, {mean:g} cm on average"
        if cov >= R["winter_cover_pct"] and mean >= R["winter_mean_cm"]:
            return "winter", f"{snow_txt[0].upper()}{snow_txt[1:]}. Fire risk minimal while it lasts."
        if cov >= R["patchy_cover_pct"]:
            return "patchy", f"{snow_txt[0].upper()}{snow_txt[1:]}. Exposed cured grass can burn on dry, windy days."
        free = snow.get("snow_free_since")
        since = f" since {datetime.fromisoformat(free):%b %-d}" if free else ""
        if growing:
            return "growing", f"Snow-free{since}. Grass is green or greening up; CWFIS fire danger applies."
        return "grass", (f"Snow-free{since} and the grass is cured. Fires start and spread fast "
                         f"on dry, windy days, often before the daily fire danger catches up.")
    if growing:
        return "growing", "Snow cover unknown; green season by date."
    return "unknown", "Snow cover unavailable, and outside the green season by date: check conditions on the ground."


def _rank(label):
    return DANGER_ORDER.index(label) if label in DANGER_ORDER else -1


def _ang(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def build(area, panel, alerts_area, fires_near, smoke_sources, now=None, snow=None, force_season=None):
    now = now or datetime.now(timezone.utc)
    R = RULES
    items = []
    tzname = (panel.get("weather") or {}).get("timezone") or "America/Winnipeg"
    mode, season_text = season(area, snow, now.astimezone(ZoneInfo(tzname)).date(), force=force_season)

    def add(level, kind, title, detail="", at=None, until=None):
        items.append({"level": level, "kind": kind, "title": title, "detail": detail,
                      "at": at.isoformat() if isinstance(at, datetime) else at,
                      "until": until.isoformat() if isinstance(until, datetime) else until})

    # ---- official daily danger (CWFIS) -------------------------------------------------------------
    # The status uses the class at the area's reference point, shown with that point's FWI. The worst cell in
    # the area's box is a separate item with its own FWI: information when High, a Heightened item when Very
    # High or Extreme (one hot cell at the edge of a large box shouldn't set the whole base's status).
    days = panel.get("fwi_days") or []
    today_d = days[0] if days else None
    danger_today = today_d.get("fdr_label") if today_d else None
    fwi_txt = lambda v: f"FWI {v:.0f}" if v is not None else "FWI not given"
    if danger_today:
        r = _rank(danger_today)
        lvl = 2 if r >= 3 else 1 if r == 2 else 0
        add(0 if mode == "winter" else lvl, "danger", f"Fire danger {danger_today} today",
            f"CWFIS daily at the base, {fwi_txt(today_d.get('fwi'))}" + (" · snow-covered, so not counted" if mode == "winter" and lvl else ""))
    worst = today_d.get("fdr_max_label") if today_d else None
    if worst and _rank(worst) > _rank(danger_today) and _rank(worst) >= 2:
        add(0 if mode == "winter" else 1 if _rank(worst) >= 3 else 0, "danger", f"Worst part of the area: {worst}",
            f"CWFIS daily, highest cell in the area box, {fwi_txt(today_d.get('fwi_max'))}")
    for d in days[1:3]:
        lab = d.get("fdr_label")
        if lab and _rank(lab) >= 3 and _rank(lab) > _rank(danger_today):
            day = datetime.fromisoformat(d["date"])
            add(0 if mode == "winter" else 1, "danger", f"Fire danger rising to {lab} {day:%A}", f"CWFIS forecast at the base, {fwi_txt(d.get('fwi'))}",
                at=d["date"])
    danger_r = _rank(danger_today)
    worst_today = danger_today

    # ---- ECCC alerts --------------------------------------------------------------------------------
    for a in (alerts_area or {}).get("alerts", []):
        lvl = {"warning": 2, "watch": 1, "statement": 1}.get(a.get("level"), 1)
        ack = a.get("ack")
        add(lvl, "alert", a["name"], "ECCC" + (f" · acknowledged by {ack['by']}" if ack else " · not acknowledged"),
            at=a.get("issued"), until=a.get("expires"))
        items[-1]["alert_level"] = a.get("level")

    # ---- hourly weather: the timeline, wind, shifts, RH ----------------------------------------------
    w = panel.get("weather")
    tl = None
    if w:
        tz = ZoneInfo(w.get("timezone") or "UTC")
        h = w["hourly"]
        t_utc = [datetime.fromisoformat(t).replace(tzinfo=tz).astimezone(timezone.utc) for t in h["time"]]
        i0 = next((i for i, t in enumerate(t_utc) if t >= now - timedelta(minutes=59)), 0)
        idx = list(range(i0, min(i0 + R["hours"] + 1, len(t_utc))))
        get = lambda k, i: h[k][i] if h.get(k) and i < len(h[k]) else None
        # wind shifts: direction turned R["shift_deg"]+ within the window, with real wind
        shifts = []
        for i in idx:
            j = i - R["shift_window"]
            if j < 0 or get("wd", i) is None or get("wd", j) is None:
                continue
            if _ang(get("wd", i), get("wd", j)) >= R["shift_deg"] and (get("ws", i) or 0) >= R["shift_min_ws"]:
                if not shifts or i - shifts[-1] > R["shift_window"]:
                    shifts.append(i)
        ahead = [i for i in idx if t_utc[i] <= now + timedelta(hours=R["wind_hours"])]
        if ahead:
            gi = max(ahead, key=lambda i: get("gust", i) or 0)
            g = get("gust", gi) or 0
            if g >= R["gust_high"]:
                add(2, "wind", f"Gusts to {g:.0f} km/h", "Forecast, next 24 h", at=t_utc[gi])
            elif g >= R["gust_heightened"]:
                add(1, "wind", f"Gusts to {g:.0f} km/h", "Forecast, next 24 h", at=t_utc[gi])
        for i in shifts:
            if t_utc[i] <= now + timedelta(hours=R["wind_hours"]):
                j = i - R["shift_window"]
                lvl = 2 if danger_r >= 2 else 1
                add(lvl, "shift", f"Wind shift {_compass(get('wd', j))} to {_compass(get('wd', i))}",
                    f"{get('ws', i):.0f} km/h gusting {get('gust', i) or 0:.0f}", at=t_utc[i])
                break
        if ahead and danger_r >= 1:
            ri = min(ahead, key=lambda i: get("rh", i) if get("rh", i) is not None else 999)
            if get("rh", ri) is not None and get("rh", ri) <= R["rh_low"]:
                add(1, "rh", f"Humidity down to {get('rh', ri):.0f}%", "Forecast, next 24 h", at=t_utc[ri])

        # grass season: dry, windy hours over cured grass (patchy snow: Heightened at most)
        if mode in ("grass", "patchy") and ahead:
            def dry_windy(i, rh_max, ws_min):
                rh, ws, g = get("rh", i), get("ws", i), get("gust", i)
                return rh is not None and rh <= rh_max and max(ws or 0, (g or 0) * 0.7) >= ws_min
            hi = [i for i in ahead if dry_windy(i, R["grass_rh_high"], R["grass_ws_high"])]
            wa = [i for i in ahead if dry_windy(i, R["grass_rh_watch"], R["grass_ws_watch"])]
            run = hi or wa
            if run:
                lvl = 2 if hi and mode == "grass" else 1
                pk = min(run, key=lambda i: get("rh", i))
                add(lvl, "grass", f"Dry and windy over cured grass: {len(run)} h in the next 24 h",
                    f"Lowest RH {get('rh', pk):.0f}% with wind {get('ws', pk):.0f} km/h gusting {get('gust', pk) or 0:.0f}",
                    at=t_utc[run[0]], until=t_utc[run[-1]] + timedelta(hours=1))

        # crossover / 30/30 / 30/30/30: one item per tier for its first run of hours in the next 24 h
        xo = {i: _xover(get("temp", i), get("rh", i), get("ws", i)) for i in idx}
        for tier in (3, 2, 1):
            run = [i for i in ahead if xo[i] >= tier]
            if not run:
                continue
            a = b = run[0]
            while b + 1 in xo and xo[b + 1] >= tier:
                b += 1
            pk = max(range(a, b + 1), key=lambda i: (get("temp", i) or 0) - (get("rh", i) or 0))
            span = f"{(t_utc[b] - t_utc[a]).total_seconds() / 3600 + 1:.0f} h"
            detail = (f"{span}, worst {get('temp', pk):.0f} °C / RH {get('rh', pk):.0f}%"
                      + (f" / wind {get('ws', pk):.0f} km/h" if tier == 3 else ""))
            add(2 if tier == 3 else 1, "xover", X_NAMES[tier] + (" (RH below temperature)" if tier == 1 else ""),
                detail, at=t_utc[a], until=t_utc[b] + timedelta(hours=1))
            break  # only the highest tier reached; the timeline row shows the rest

        # smoke, hourly: the highest of the sources at each timeline hour
        smoke_by_hour = {}
        for src in smoke_sources:
            doc = src.doc
            if not doc or area["id"] not in (doc.get("outlook") or {}):
                continue
            for t, v in zip(doc["times"], doc["outlook"][area["id"]]):
                k = datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H")
                smoke_by_hour[k] = max(smoke_by_hour.get(k, 0), v or 0)
        day_class = {d.get("date", "")[:10]: d.get("fdr_label") for d in days if d.get("date")}
        tl = {
            "time": [t_utc[i].isoformat() for i in idx],
            "local": [h["time"][i] for i in idx],
            "timezone": w.get("timezone"),
            "temp": [get("temp", i) for i in idx], "rh": [get("rh", i) for i in idx],
            "ws": [get("ws", i) for i in idx], "gust": [get("gust", i) for i in idx], "wd": [get("wd", i) for i in idx],
            "precip": [get("precip", i) for i in idx], "fwi": [get("fwi", i) for i in idx],
            # the official CWFIS daily class for each hour's local day (not a class made from hourly FWI:
            # hourly FWI peaks in the afternoon above the daily value the classes are defined on)
            "danger": [day_class.get(h["time"][i][:10]) for i in idx],
            "smoke": [smoke_by_hour.get(t_utc[i].strftime("%Y-%m-%dT%H")) for i in idx],
            "shifts": [idx.index(i) for i in shifts if i in idx],
            "xover": [xo[i] for i in idx],
        }

    # ---- fires ----------------------------------------------------------------------------------------
    if fires_near:
        recent = [x for x in fires_near.get("hotspots", []) if x["km"] == 0
                  and datetime.fromisoformat(x["time"]) >= now - timedelta(hours=R["hotspot_recent_h"])]
        if recent:
            add(2 if danger_r >= 2 else 1, "fire", f"{len(recent)} satellite hotspot{'s' if len(recent) > 1 else ''} inside the area",
                f"Last {R['hotspot_recent_h']} h, NASA FIRMS · a heat signal, not a confirmed fire",
                at=max(x["time"] for x in recent))
        near = fires_near.get("nearby") or 0
        if near:
            add(0, "fire", f"{near} hotspot{'s' if near > 1 else ''} within {fires_near.get('km', 25):g} km", "Last 24 h, NASA FIRMS",
                at=fires_near.get("latest"))
        for f in (fires_near.get("reported") or [])[:3]:
            add(1 if f["km"] <= 25 else 0, "fire", f"Reported fire {f['name']} {f['km']:g} km away",
                " · ".join(x for x in (f.get("agency"), f.get("stage"), f"{f['ha']} ha" if f.get("ha") else None) if x))

    # ---- smoke --------------------------------------------------------------------------------------
    for sm in (panel.get("smoke") or {}).get("sources", []):
        if sm["level"] in ("elevated", "high"):
            add(2 if sm["level"] == "high" else 1, "smoke", f"Smoke forecast: PM2.5 up to {sm['max']:.0f} µg/m³",
                sm["label"] + " · model guidance", at=sm["first_elevated"] or sm["at"])
            break

    # ---- the statement ----------------------------------------------------------------------------
    items.sort(key=lambda x: (-x["level"], x["at"] or ""))
    level = max((x["level"] for x in items), default=0)
    drivers = [x for x in items if x["level"] == level and level > 0][:3]
    future = sorted((x for x in items if x["at"] and x["level"] > 0 and _parse(x["at"]) and _parse(x["at"]) > now),
                    key=lambda x: x["at"])
    return {
        "area": {k: area.get(k) for k in ("id", "name", "prov", "lat", "lon", "bbox")},
        "level": level, "label": LEVELS[level],
        "headline": "; ".join(x["title"] for x in drivers) if drivers else {
            "winter": "Snow-covered. Nothing out of the ordinary in the next 48 h",
            "grass": "Snow-free, cured grass, but no dry and windy hours in the next 24 h"}.get(mode, "Nothing out of the ordinary in the next 48 h"),
        "season": {"mode": mode, "label": SEASONS[mode][0], "icon": SEASONS[mode][1], "text": season_text, "snow": snow,
                   "preview": force_season in PREVIEW_TEXT},
        "next": future[0] if future else None,
        "items": items,
        "timeline": tl,
        "danger_today": worst_today,
        "sources_ok": {"cwfis": "cwfis" not in panel.get("errors", {}), "weather": "weather" not in panel.get("errors", {}),
                       "alerts": bool(alerts_area and not alerts_area.get("error"))},
        "fetched": panel.get("fetched"),
        "draft_rules": True, "rules": RULES,
    }


def _parse(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else None
    except ValueError:
        return None


def _compass(d):
    if d is None:
        return "?"
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round((d % 360) / 45) % 8]
