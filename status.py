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
    "hotspot_recent_h": 6,       # a hotspot inside the area this recent counts
}
# hourly FWI -> approximate danger class, for the timeline only (CWFIS classes are set regionally; the
# official daily class from CWFIS is what the status uses)
FWI_BANDS = [(5, "Low"), (10, "Moderate"), (20, "High"), (30, "Very High"), (float("inf"), "Extreme")]


def _dclass(fwi):
    if fwi is None:
        return None
    return next(lab for top, lab in FWI_BANDS if fwi < top)


def _rank(label):
    return DANGER_ORDER.index(label) if label in DANGER_ORDER else -1


def _ang(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def build(area, panel, alerts_area, fires_near, smoke_sources, now=None):
    now = now or datetime.now(timezone.utc)
    R = RULES
    items = []

    def add(level, kind, title, detail="", at=None, until=None):
        items.append({"level": level, "kind": kind, "title": title, "detail": detail,
                      "at": at.isoformat() if isinstance(at, datetime) else at,
                      "until": until.isoformat() if isinstance(until, datetime) else until})

    # ---- official daily danger (CWFIS) -------------------------------------------------------------
    days = panel.get("fwi_days") or []
    today_d = days[0] if days else None
    danger_today = today_d.get("fdr_label") if today_d else None
    worst_today = max([x for x in (danger_today, today_d.get("fdr_max_label") if today_d else None) if x],
                      key=_rank, default=None)
    if worst_today:
        r = _rank(worst_today)
        lvl = 2 if r >= 3 else 1 if r == 2 else 0
        where = "" if worst_today == danger_today else " (worst part of the area)"
        add(lvl, "danger", f"Fire danger {worst_today} today{where}",
            f"CWFIS daily, FWI {today_d.get('fwi'):.0f}" if today_d.get("fwi") is not None else "CWFIS daily")
    for d in days[1:3]:
        lab = max([x for x in (d.get("fdr_label"), d.get("fdr_max_label")) if x], key=_rank, default=None)
        if lab and _rank(lab) >= 3 and _rank(lab) > _rank(worst_today):
            day = datetime.fromisoformat(d["date"])
            add(1, "danger", f"Fire danger rising to {lab} {day:%A}", "CWFIS forecast", at=d["date"])
    danger_r = _rank(worst_today)

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

        # smoke, hourly: the highest of the sources at each timeline hour
        smoke_by_hour = {}
        for src in smoke_sources:
            doc = src.doc
            if not doc or area["id"] not in (doc.get("outlook") or {}):
                continue
            for t, v in zip(doc["times"], doc["outlook"][area["id"]]):
                k = datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H")
                smoke_by_hour[k] = max(smoke_by_hour.get(k, 0), v or 0)
        tl = {
            "time": [t_utc[i].isoformat() for i in idx],
            "local": [h["time"][i] for i in idx],
            "timezone": w.get("timezone"),
            "temp": [get("temp", i) for i in idx], "rh": [get("rh", i) for i in idx],
            "ws": [get("ws", i) for i in idx], "gust": [get("gust", i) for i in idx], "wd": [get("wd", i) for i in idx],
            "precip": [get("precip", i) for i in idx], "fwi": [get("fwi", i) for i in idx],
            "danger": [_dclass(get("fwi", i)) for i in idx],
            "smoke": [smoke_by_hour.get(t_utc[i].strftime("%Y-%m-%dT%H")) for i in idx],
            "shifts": [idx.index(i) for i in shifts if i in idx],
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
        "headline": "; ".join(x["title"] for x in drivers) if drivers else "Nothing out of the ordinary in the next 48 h",
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
