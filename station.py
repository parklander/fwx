"""Weather station readings, kept in data/station.sqlite in metric, one row per station and observation time.

Two ways in:
- Weather Underground (the usual one): with FW_WU_STATION and FW_WU_KEY set, the server polls the station's current
  conditions from the PWS API every FW_WU_POLL seconds (default 300), and on start fills in today's readings.
  The key is the free one Weather Underground gives station owners (wunderground.com/member/api-keys).
- Direct upload: an Ecowitt station's "Customized" upload POSTed to /api/station/<FW_STATION_TOKEN>. Its PASSKEY
  is replaced by a short hash that identifies the station.
The full source record is kept as JSON with each reading.
"""
import hashlib
import json
import logging
import math
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import requests

log = logging.getLogger("station")
WU_API = "https://api.weather.com/v2/pws/observations"

STALE_S = 600  # no upload for 10 minutes -> "stale"

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs (
    station_id    TEXT NOT NULL,
    obs_utc       TEXT NOT NULL,
    received_utc  TEXT NOT NULL,
    model         TEXT,
    temp_c        REAL,
    rh_pct        REAL,
    dewpoint_c    REAL,
    wind_kmh      REAL,
    gust_kmh      REAL,
    wind_dir_deg  REAL,
    rain_total_mm REAL,
    rain_daily_mm REAL,
    rain_rate_mmh REAL,
    pressure_hpa  REAL,
    solar_wm2     REAL,
    uv            REAL,
    raw           TEXT NOT NULL,
    PRIMARY KEY (station_id, obs_utc)
);
"""
COLS = ("station_id, obs_utc, received_utc, model, temp_c, rh_pct, dewpoint_c, wind_kmh, gust_kmh, wind_dir_deg, "
        "rain_total_mm, rain_daily_mm, rain_rate_mmh, pressure_hpa, solar_wm2, uv")


def _num(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except ValueError:
                pass
    return None


def _r(v, n=1):
    return None if v is None else round(v, n)


def _dewpoint(t, rh):
    if t is None or not rh:
        return None
    a, b = 17.625, 243.04
    g = math.log(rh / 100) + a * t / (b + t)
    return b * g / (a - g)


def _obs_time(s):
    # Ecowitt sends "YYYY-MM-DD HH:MM:SS" in UTC (URL-encoded); some firmware sends "now".
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        t = datetime.strptime(s or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return now
    return t if t <= now + timedelta(minutes=5) else now  # station clock ahead -> trust ours


def parse(body: bytes) -> dict:
    form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    key = form.pop("PASSKEY", "") or form.pop("passkey", "")
    t = _num(form, "tempf")
    t = None if t is None else (t - 32) * 5 / 9
    rh = _num(form, "humidity")
    mph, inch = 1.609344, 25.4
    mul = lambda v, k: None if v is None else v * k
    return {
        "station_id": hashlib.sha256(key.encode()).hexdigest()[:10] if key else "unknown",
        "obs_utc": _obs_time(form.get("dateutc")).isoformat(),
        "received_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "model": form.get("model") or form.get("stationtype"),
        "temp_c": _r(t),
        "rh_pct": _r(rh, 0),
        "dewpoint_c": _r(_dewpoint(t, rh)),
        "wind_kmh": _r(mul(_num(form, "windspeedmph"), mph)),
        "gust_kmh": _r(mul(_num(form, "windgustmph"), mph)),
        "wind_dir_deg": _r(_num(form, "winddir"), 0),
        "rain_total_mm": _r(mul(_num(form, "totalrainin", "yearlyrainin"), inch), 2),
        "rain_daily_mm": _r(mul(_num(form, "dailyrainin"), inch), 2),
        "rain_rate_mmh": _r(mul(_num(form, "rainratein"), inch), 2),
        "pressure_hpa": _r(mul(_num(form, "baromrelin"), 33.8639)),
        "solar_wm2": _r(_num(form, "solarradiation")),
        "uv": _num(form, "uv"),
        "raw": json.dumps(form, separators=(",", ":")),
    }


def _wu_row(o: dict, received: str) -> dict:
    """A PWS API observation (units=m) as a row. Works for /current (temp, windSpeed, ...) and for the
    /all/1day history (tempAvg, windspeedAvg, ...). precipTotal is the total since local midnight."""
    m = o.get("metric") or {}
    pick = lambda d, *ks: next((d[k] for k in ks if d.get(k) is not None), None)
    t, rh = pick(m, "temp", "tempAvg"), pick(o, "humidity", "humidityAvg")
    t_obs = datetime.fromisoformat(o["obsTimeUtc"].replace("Z", "+00:00")).astimezone(timezone.utc)
    return {
        "station_id": o.get("stationID", "unknown"),
        "obs_utc": t_obs.replace(microsecond=0).isoformat(),
        "received_utc": received,
        "model": o.get("softwareType"),
        "temp_c": _r(t),
        "rh_pct": _r(rh, 0),
        "dewpoint_c": _r(pick(m, "dewpt", "dewptAvg")),
        "wind_kmh": _r(pick(m, "windSpeed", "windspeedAvg")),
        "gust_kmh": _r(pick(m, "windGust", "windgustHigh")),
        "wind_dir_deg": _r(pick(o, "winddir", "winddirAvg"), 0),
        "rain_total_mm": None,
        "rain_daily_mm": _r(m.get("precipTotal"), 2),
        "rain_rate_mmh": _r(m.get("precipRate"), 2),
        "pressure_hpa": _r(pick(m, "pressure", "pressureMax")),
        "solar_wm2": _r(pick(o, "solarRadiation", "solarRadiationHigh")),
        "uv": pick(o, "uv", "uvHigh"),
        "raw": json.dumps(o, separators=(",", ":")),
    }


class Station:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "station.sqlite"
        self._lock = threading.Lock()
        self._ready = False

    def _db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con.executescript(SCHEMA)
            self._ready = True
        return con

    def _insert(self, rows):
        if not rows:
            return 0
        keys = list(rows[0])
        with self._lock, closing(self._db()) as con, con:
            before = con.total_changes
            con.executemany(f"INSERT OR IGNORE INTO obs ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                            [[r[k] for k in keys] for r in rows])
            return con.total_changes - before

    def ingest(self, body: bytes) -> dict:
        row = parse(body)
        self._insert([row])
        return row

    # ---- Weather Underground polling ----

    def _wu_get(self, path, station, key):
        r = requests.get(f"{WU_API}/{path}", timeout=20,
                         params={"stationId": station, "format": "json", "units": "m", "apiKey": key})
        if r.status_code == 204:  # station hasn't reported in the last hour
            return []
        r.raise_for_status()
        return r.json().get("observations") or []

    def poll_wu(self, station, key, history=False):
        """Fetch the current reading (or, with history=True, today's readings so far). Returns rows added."""
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        obs = self._wu_get("all/1day" if history else "current", station, key)
        return self._insert([_wu_row(o, now) for o in obs if o.get("obsTimeUtc")])

    def wu_loop(self, stop, station, key, every=300):
        every = max(60, every)  # the free key allows 1,500 calls a day
        try:
            log.info("Weather Underground %s: %d readings from today", station, self.poll_wu(station, key, history=True))
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("Weather Underground %s history: %s", station, e)
        while not stop.wait(every):
            try:
                self.poll_wu(station, key)
            except (requests.RequestException, ValueError, KeyError) as e:
                log.warning("Weather Underground %s: %s", station, e)

    def _rows(self, sql, args=()):
        with closing(self._db()) as con:
            return [dict(r) for r in con.execute(sql, args)]

    def stations(self):
        rows = self._rows("SELECT station_id, model, max(obs_utc) AS last_obs_utc, count(*) AS n_obs "
                          "FROM obs GROUP BY station_id ORDER BY last_obs_utc DESC")
        for r in rows:
            r["stale"] = self._age(r["last_obs_utc"]) > STALE_S
        return rows

    def _station(self, station_id):
        if station_id:
            return station_id
        s = self.stations()
        return s[0]["station_id"] if s else None

    @staticmethod
    def _age(iso):
        return int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds())

    def current(self, station_id=None):
        sid = self._station(station_id)
        rows = self._rows(f"SELECT {COLS} FROM obs WHERE station_id = ? ORDER BY obs_utc DESC LIMIT 1", (sid,))
        if not rows:
            return None
        r = rows[0]
        r["age_s"] = self._age(r["obs_utc"])
        r["stale"] = r["age_s"] > STALE_S
        return r

    def history(self, hours, station_id=None):
        sid = self._station(station_id)
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self._rows(f"SELECT {COLS} FROM obs WHERE station_id = ? AND obs_utc >= ? ORDER BY obs_utc", (sid, since))

    def hourly(self, hours, station_id=None):
        """Hourly means of temperature, RH and wind, the hour's highest gust, and the rain that fell in the hour
        (from the station's running total or today's total, so the reset at midnight doesn't count as rain). Hours are
        labelled by their start, in UTC. This is the shape the hourly FFMC/FWI calculations step through."""
        rows = self.history(hours + 1, station_id)
        out, prev, prev_day = {}, None, None
        for r in rows:
            h = out.setdefault(r["obs_utc"][:13] + ":00:00+00:00",
                               {"temp_c": [], "rh_pct": [], "wind_kmh": [], "gust_kmh": None, "rain_mm": 0.0, "n": 0})
            h["n"] += 1
            for k in ("temp_c", "rh_pct", "wind_kmh"):
                if r[k] is not None:
                    h[k].append(r[k])
            if r["gust_kmh"] is not None:
                h["gust_kmh"] = max(h["gust_kmh"] or 0, r["gust_kmh"])
            tot, day = r["rain_total_mm"], r["rain_daily_mm"]
            if tot is not None:  # station's running total: rain = increase; a drop is a counter reset
                if prev is not None and tot > prev:
                    h["rain_mm"] += tot - prev
                prev = tot
            elif day is not None:  # today's total (Weather Underground): a drop is the midnight reset
                if prev_day is not None:
                    h["rain_mm"] += day - prev_day if day >= prev_day else day
                prev_day = day
        mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
        return [{"hour_utc": k, "temp_c": mean(h["temp_c"]), "rh_pct": mean(h["rh_pct"]),
                 "wind_kmh": mean(h["wind_kmh"]), "gust_kmh": h["gust_kmh"],
                 "rain_mm": round(h["rain_mm"], 2), "n_obs": h["n"]}
                for k, h in sorted(out.items())][-hours:]
