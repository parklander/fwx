"""Point forecast for an area: Open-Meteo (ECCC GEM models) hourly weather, plus hourly FFMC, ISI and FWI.

Hourly fire weather is computed here, not taken from CWFIS: it starts from CWFIS's daily FFMC at noon
yesterday and steps the hourly FFMC (Van Wagner 1977, as in the cffdrs package's hffmc) through
Open-Meteo's hourly temperature, RH, wind and rain. ISI and FWI then use that hourly FFMC with the
hourly wind and CWFIS's daily BUI for that day (the last CWFIS BUI is held past the end of its forecast).
"""
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("wxpoint")

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
MODEL = "gem_seamless"  # ECCC: HRDPS near term, then RDPS, then GDPS
HOURLY = ["temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m",
          "wind_direction_10m", "wind_gusts_10m", "weather_code"]
DAILY = ["weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum", "sunrise", "sunset"]

_session = requests.Session()
_cache, _lock = {}, threading.Lock()


def open_meteo(lat, lon):
    """Open-Meteo GEM forecast: yesterday plus 7 days, hourly and daily, local time (cached 30 min)."""
    key = (round(lat, 3), round(lon, 3))
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
    r = _session.get(OPEN_METEO, timeout=30, params={
        "latitude": lat, "longitude": lon, "models": MODEL, "timezone": "auto",
        "past_days": 1, "forecast_days": 7, "wind_speed_unit": "kmh",
        "hourly": ",".join(HOURLY), "daily": ",".join(DAILY)})
    r.raise_for_status()
    data = r.json()
    with _lock:
        _cache[key] = (time.time(), data)
    return data


# ---- FWI System, hourly FFMC ------------------------------------------------------------------------

def hourly_ffmc(ffmc_prev, t, rh, ws, rain):
    """One hour of the hourly FFMC (Van Wagner 1977; cffdrs hffmc)."""
    rh = min(max(rh, 0.0), 100.0)
    ws = max(ws, 0.0)
    rain = max(rain, 0.0)
    mo = 147.27723 * (101.0 - ffmc_prev) / (59.5 + ffmc_prev)
    if rain > 0:
        add = 42.5 * rain * math.exp(-100.0 / (251.0 - mo)) * (1.0 - math.exp(-6.93 / rain))
        if mo > 150:
            add += 0.0015 * (mo - 150.0) ** 2 * math.sqrt(rain)
        mo = min(mo + add, 250.0)
    ed = 0.942 * rh ** 0.679 + 11.0 * math.exp((rh - 100.0) / 10.0) + 0.18 * (21.1 - t) * (1.0 - math.exp(-0.115 * rh))
    ew = 0.618 * rh ** 0.753 + 10.0 * math.exp((rh - 100.0) / 10.0) + 0.18 * (21.1 - t) * (1.0 - math.exp(-0.115 * rh))
    if mo > ed:
        k0 = 0.424 * (1.0 - (rh / 100.0) ** 1.7) + 0.0694 * math.sqrt(ws) * (1.0 - (rh / 100.0) ** 8)
        kd = k0 * 0.0579 * math.exp(0.0365 * t)
        m = ed + (mo - ed) * 10.0 ** (-kd)
    elif mo < ew:
        k1 = 0.424 * (1.0 - ((100.0 - rh) / 100.0) ** 1.7) + 0.0694 * math.sqrt(ws) * (1.0 - ((100.0 - rh) / 100.0) ** 8)
        kw = k1 * 0.0579 * math.exp(0.0365 * t)
        m = ew - (ew - mo) * 10.0 ** (-kw)
    else:
        m = mo
    return min(max(59.5 * (250.0 - m) / (147.27723 + m), 0.0), 101.0)


def isi(ffmc, ws):
    m = 147.27723 * (101.0 - ffmc) / (59.5 + ffmc)
    ff = 91.9 * math.exp(-0.1386 * m) * (1.0 + m ** 5.31 / 4.93e7)
    return 0.208 * math.exp(0.05039 * ws) * ff


def fwi(isi_v, bui):
    fd = 0.626 * bui ** 0.809 + 2.0 if bui <= 80 else 1000.0 / (25.0 + 108.64 * math.exp(-0.023 * bui))
    b = 0.1 * isi_v * fd
    return b if b <= 1 else math.exp(2.72 * (0.434 * math.log(b)) ** 0.647)


def hourly_fire(times, temp, rh, ws, rain, cwfis_rows):
    """Hourly FFMC, ISI, FWI over Open-Meteo's hours, started from CWFIS's daily noon FFMC.

    times: local "YYYY-MM-DDTHH:MM" strings. cwfis_rows: cwfis.area_daily rows (date, ffmc, bui, ...).
    Hours before the starting noon get None. Returns (ffmc, isi, fwi, start_label).
    """
    by_day = {r["date"]: r for r in cwfis_rows}
    n = len(times)
    out_f, out_i, out_w = [None] * n, [None] * n, [None] * n
    start = None
    for k, t in enumerate(times):  # first noon with a CWFIS FFMC
        if t.endswith("T12:00") and (by_day.get(t[:10]) or {}).get("ffmc") is not None:
            start = k
            break
    if start is None:
        return out_f, out_i, out_w, None
    f = by_day[times[start][:10]]["ffmc"]
    bui_day, bui = None, None
    for k in range(start, n):
        if k > start:
            if None in (temp[k], rh[k]):
                break
            f = hourly_ffmc(f, temp[k], rh[k], ws[k] or 0.0, rain[k] or 0.0)
        day = times[k][:10]
        # the day's BUI applies from its noon; before noon the previous day's
        bday = day if times[k][11:13] >= "12" else (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        if bday != bui_day:
            row = by_day.get(bday)
            if row and row.get("bui") is not None:
                bui = row["bui"]
            bui_day = bday
        out_f[k] = round(f, 1)
        if ws[k] is not None:
            i = isi(f, ws[k])
            out_i[k] = round(i, 1)
            if bui is not None:
                out_w[k] = round(fwi(i, bui), 1)
    return out_f, out_i, out_w, times[start]


def area_weather(area, cwfis_rows):
    om = open_meteo(area["lat"], area["lon"])
    h = om["hourly"]
    times = h["time"]
    ffmc, isi_h, fwi_h, start = hourly_fire(times, h["temperature_2m"], h["relative_humidity_2m"],
                                            h["wind_speed_10m"], h["precipitation"], cwfis_rows)
    hourly = {"time": times, "temp": h["temperature_2m"], "rh": h["relative_humidity_2m"],
              "precip": h["precipitation"], "ws": h["wind_speed_10m"], "wd": h["wind_direction_10m"],
              "gust": h["wind_gusts_10m"], "code": h["weather_code"], "ffmc": ffmc, "isi": isi_h, "fwi": fwi_h}
    d = om["daily"]
    daily = []
    for k, day in enumerate(d["time"]):
        idx = [i for i, t in enumerate(times) if t.startswith(day)]
        pick = lambda key: [hourly[key][i] for i in idx if hourly[key][i] is not None]
        rhs, wss, gus, fws = pick("rh"), pick("ws"), pick("gust"), pick("fwi")
        # direction of the strongest hourly wind of the day
        strongest = max(idx, key=lambda i: hourly["ws"][i] or -1) if idx else None
        daily.append({
            "date": day, "code": d["weather_code"][k], "tmax": d["temperature_2m_max"][k], "tmin": d["temperature_2m_min"][k],
            "precip": d["precipitation_sum"][k], "rh_min": min(rhs) if rhs else None,
            "ws_max": max(wss) if wss else None, "gust_max": max(gus) if gus else None,
            "wd": hourly["wd"][strongest] if strongest is not None else None,
            "fwi_peak": max(fws) if fws else None,
            "sunrise": d["sunrise"][k], "sunset": d["sunset"][k]})
    tz = om.get("timezone") or "UTC"
    return {"model": "ECCC GEM via Open-Meteo", "timezone": tz, "now": datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:00"),
            "fire_start": start,
            "hourly": hourly, "daily": daily}
