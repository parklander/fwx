"""Weather station uploads (Ecowitt "Customized" upload, Ecowitt protocol).

The station POSTs a form every interval to /api/station/<token>. Readings are converted to metric and kept in
data/station.sqlite, one row per station and observation time, with the full upload as JSON (minus the PASSKEY,
which is replaced by a short hash that identifies the station).
"""
import hashlib
import json
import math
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

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

    def ingest(self, body: bytes) -> dict:
        row = parse(body)
        with self._lock, closing(self._db()) as con, con:
            con.execute(f"INSERT OR IGNORE INTO obs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                        list(row.values()))
        return row

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
        (from the station's running total, so the daily reset at midnight doesn't count as rain). Hours are
        labelled by their start, in UTC. This is the shape the hourly FFMC/FWI calculations step through."""
        rows = self.history(hours + 1, station_id)
        out, prev = {}, None
        for r in rows:
            h = out.setdefault(r["obs_utc"][:13] + ":00:00+00:00",
                               {"temp_c": [], "rh_pct": [], "wind_kmh": [], "gust_kmh": None, "rain_mm": 0.0, "n": 0})
            h["n"] += 1
            for k in ("temp_c", "rh_pct", "wind_kmh"):
                if r[k] is not None:
                    h[k].append(r[k])
            if r["gust_kmh"] is not None:
                h["gust_kmh"] = max(h["gust_kmh"] or 0, r["gust_kmh"])
            tot = r["rain_total_mm"]
            if tot is not None:
                if prev is not None and tot > prev:
                    h["rain_mm"] += tot - prev
                prev = tot
        mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
        return [{"hour_utc": k, "temp_c": mean(h["temp_c"]), "rh_pct": mean(h["rh_pct"]),
                 "wind_kmh": mean(h["wind_kmh"]), "gust_kmh": h["gust_kmh"],
                 "rain_mm": round(h["rain_mm"], 2), "n_obs": h["n"]}
                for k, h in sorted(out.items())][-hours:]
