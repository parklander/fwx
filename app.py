"""CWFIS fire weather forecast: a map of the CWFIS daily layers and a forecast PDF for each area.

Run:  uvicorn app:app --host 127.0.0.1 --port 8000
"""
import hashlib
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool

import alerts
import cwfis
import fires
import goes
import smoke
import snowdepth
import push
import report_pdf
import rdps
import wxpoint

ROOT = Path(__file__).resolve().parent
AOI_FILE = ROOT / "aois.json"
DATA = ROOT / "data"  # alert state, push keys and subscriptions (not in git)
MAX_DAYS = 12  # longest report
TZ = ZoneInfo(os.environ.get("FW_TZ", "America/Winnipeg"))  # decides "today" (obs vs forecast) and the issue time


def today():
    return datetime.now(TZ).date()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("app")
pusher = push.Push(DATA)
wx_alerts = alerts.Alerts(AOI_FILE, DATA / "alerts_state.json", on_new=pusher.notify_alert)
rdps_maps = rdps.Rdps(DATA)
bluesky = smoke.BlueSky(DATA)


def _fire_push(area, hotspots):
    """New satellite hotspots inside an area's box -> a push to that area's subscribers (if FW_FIRE_PUSH=1)."""
    if os.environ.get("FW_FIRE_PUSH", "0") != "1":
        return
    t = max(h["time"] for h in hotspots)
    when = datetime.fromisoformat(t).astimezone(TZ).strftime("%H:%M")
    n = len(hotspots)
    pusher.notify_alert(area, {"name": "Satellite hotspot" + ("s" if n > 1 else ""),
                               "text": f"{n} new satellite fire detection{'s' if n > 1 else ''} inside the area, latest {when} "
                                       f"({hotspots[-1]['sat']}). Could be a range fire, a burn or a wildfire; check it."})


fire_data = fires.Fires(AOI_FILE, DATA, on_new=_fire_push)
_stop = threading.Event()


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=wx_alerts.loop, args=(_stop,), name="alerts", daemon=True).start()
    if os.environ.get("FW_FIRES", "1") != "0":
        threading.Thread(target=fire_data.loop, args=(_stop,), name="fires", daemon=True).start()
    if os.environ.get("FW_RDPS", os.environ.get("FW_WIND", "1")) != "0":
        threading.Thread(target=rdps_maps.loop, args=(_stop,), name="rdps", daemon=True).start()
    if os.environ.get("FW_SMOKE", "1") != "0":
        threading.Thread(target=bluesky.loop, args=(_stop,), name="bluesky", daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="CWFIS fire weather forecast", lifespan=lifespan)


def load_areas():
    # Re-read each time so edits to aois.json show up without a restart
    doc = json.loads(AOI_FILE.read_text(encoding="utf-8"))
    return doc.get("default"), doc["areas"]


def find_area(aoi_id):
    _, areas = load_areas()
    for a in areas:
        if a["id"] == aoi_id:
            return a
    raise HTTPException(404, f"unknown area {aoi_id!r}")


@app.get("/api/areas")
def areas():
    default, items = load_areas()
    return {"default": default, "areas": items}


@app.get("/api/layers")
async def layers():
    try:
        return {"wms": cwfis.WMS, "layers": await run_in_threadpool(cwfis.layers)}
    except Exception as e:
        log.warning("layer list failed: %s", e)
        raise HTTPException(502, "CWFIS isn't answering right now")


@app.get("/api/goes")
async def goes_times():
    """GOES-East/West natural colour (GeoMet WMS) and each layer's current time range."""
    return {"wms": goes.WMS, "layers": await run_in_threadpool(goes.times)}


@app.get("/api/snow")
async def snow_layers():
    """GeoMet snow depth layers (CaLDAS analysis, HRDPS forecast) and their current time windows."""
    return {"wms": snowdepth.WMS, "layers": await run_in_threadpool(snowdepth.layers)}


def _period(start):
    last = cwfis.last_date()
    if last is None:
        raise HTTPException(502, "CWFIS isn't answering right now")
    first = date.fromisoformat(start) if start else today() - timedelta(days=1)
    if first > last:
        raise HTTPException(400, f"CWFIS has nothing after {last.isoformat()}")
    return first, min(last, first + timedelta(days=MAX_DAYS - 1))


@app.get("/api/forecast")
async def forecast(aoi: str, start: str | None = Query(None, pattern=r"^\d{4}-\d\d-\d\d$")):
    area = find_area(aoi)
    first, last = await run_in_threadpool(_period, start)
    rows = await run_in_threadpool(cwfis.area_daily, area, first, last)
    return {"area": area, "first": first.isoformat(), "last": last.isoformat(), "rows": rows}


@app.get("/api/forecast.pdf")
async def forecast_pdf(aoi: str, start: str | None = Query(None, pattern=r"^\d{4}-\d\d-\d\d$")):
    area = find_area(aoi)
    first, last = await run_in_threadpool(_period, start)
    rows = await run_in_threadpool(cwfis.area_daily, area, first, last)
    if not rows:
        raise HTTPException(502, "CWFIS returned no values for this area")
    in_effect = (wx_alerts.for_area(area["id"]) or {}).get("alerts", [])
    pdf = await run_in_threadpool(report_pdf.build, area, rows, today(), datetime.now(TZ), in_effect)
    name = re.sub(r"[^A-Za-z0-9]+", "-", area["name"]).strip("-")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="fire-weather-{name}-{first.isoformat()}.pdf"'})


# ---- the area panel: point forecast, hourly fire weather, next three days' CWFIS FWI ------------------

def _area_panel(area):
    t = today()
    out = {"area": area, "today": t.isoformat(), "fwi_days": [], "weather": None, "errors": {},
           "fetched": datetime.now(TZ).isoformat(timespec="seconds"), "cwfis_last": None}
    rows = []
    try:
        first, last = _period((t - timedelta(days=1)).isoformat())
        rows = cwfis.area_daily(area, first, last)  # same days and cache as the default PDF
        out["fwi_days"] = [r for r in rows if t.isoformat() <= r["date"] <= (t + timedelta(days=2)).isoformat()]
        # daily codes for the Rx tab's prescription check, every day CWFIS has
        out["daily_codes"] = [{k: r.get(k) for k in ("date", "dmc", "dc", "bui")} for r in rows]
        have = [r["date"] for r in rows if r.get("fwi") is not None]
        out["cwfis_last"] = max(have) if have else None
    except Exception as e:
        log.warning("CWFIS for %s: %s", area["id"], e)
        out["errors"]["cwfis"] = "CWFIS isn't answering right now"
    try:
        out["weather"] = wxpoint.area_weather(area, rows)
    except Exception as e:
        log.warning("Open-Meteo for %s: %s", area["id"], e)
        out["errors"]["weather"] = "The weather forecast isn't available right now"
    return out


@app.get("/api/area/{aoi}")
async def area_panel(aoi: str):
    return await run_in_threadpool(_area_panel, find_area(aoi))


# ---- RDPS frames for the map overlays: 10 m wind, precipitation rate and type ------------------------------

# ---- smoke forecasts: ECCC FireWork (GeoMet WMS) and BlueSky Canada (PNG frames from its netCDF) --------

@app.get("/api/smoke")
async def smoke_meta():
    return {"wms": smoke.WMS, "firework": await run_in_threadpool(smoke.firework), "bluesky": bluesky.meta(),
            "bins": smoke.PM_BINS}


@app.get("/api/smoke/bluesky/{run}/{i}.png")
def bluesky_frame(run: str, i: int):
    if not re.fullmatch(r"\d{12}", run):
        raise HTTPException(404)
    p = bluesky.path(run, i)
    if not p.exists():
        raise HTTPException(404, "no such hour")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=86400, immutable"})


@app.get("/api/rdps")
def rdps_meta():
    return rdps_maps.meta()


@app.get("/api/rdps/{run}/{var}/{fh}")
def rdps_frame(run: str, var: str, fh: int):
    if not re.fullmatch(r"\d{10}", run) or var not in rdps.SERVED:
        raise HTTPException(404)
    p = rdps_maps.path(run, var, fh)
    if not p.is_file():
        raise HTTPException(404, "no such frame (a newer run may have replaced it)")
    # stored gzipped; the browser inflates it
    return FileResponse(p, media_type="application/octet-stream",
                        headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=31536000, immutable"})


# ---- ECCC alerts and push notifications ----------------------------------------------------------

ACK_LOG = DATA / "alert_acks.jsonl"  # every acknowledgement, append-only
_ack_lock = threading.Lock()


def _read_acks():
    try:
        lines = ACK_LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def _ts(s):
    """Seconds since the epoch for an ISO time (with Z or an offset); 0 if it can't be read."""
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.timestamp() if d.tzinfo else d.replace(tzinfo=TZ).timestamp()
    except Exception:
        return 0


def _with_acks(areas):
    """Each active alert gets the latest acknowledgement made while it was in effect (key: area + alert name).

    An ack older than the alert's first appearance belongs to an earlier alert of the same name.
    """
    latest = {}
    for a in _read_acks():
        latest[(a["area"], a["name"])] = a
    out = []
    for v in areas:
        v = dict(v)
        al = []
        for x in v["alerts"]:
            x = dict(x)
            first = wx_alerts.first_seen.get(f"{v['id']}|{x['name']}")
            ack = latest.get((v["id"], x["name"]))
            if ack and first and _ts(ack["at"]) < _ts(first):
                ack = None
            x["first_seen"], x["ack"] = first, ack
            al.append(x)
        v["alerts"] = al
        out.append(v)
    return out


@app.get("/api/alerts")
def weather_alerts():
    return {"poll_min": alerts.POLL_MIN, "now": datetime.now().astimezone().isoformat(timespec="seconds"),
            "areas": _with_acks(wx_alerts.view())}


@app.post("/api/alerts/ack")
def ack_alert(area: str = Body(...), name: str = Body(...), by: str = Body(...), issued: str | None = Body(None)):
    by = re.sub(r"\s+", " ", by).strip()[:40]
    if not by:
        raise HTTPException(400, "enter your name or initials")
    cur = wx_alerts.for_area(area)
    if not cur or name not in {x["name"] for x in cur["alerts"]}:
        raise HTTPException(404, "that alert is no longer in effect")
    rec = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "area": area, "name": name,
           "issued": issued, "by": by}
    with _ack_lock:
        DATA.mkdir(parents=True, exist_ok=True)
        with ACK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    log.warning("ALERT ACK %s: %s by %s", area, name, by)
    return rec


@app.get("/api/alerts/acklog")
def ack_log(area: str | None = None, limit: int = Query(20, ge=1, le=500)):
    rows = [a for a in _read_acks() if area is None or a["area"] == area]
    return {"acks": rows[-limit:][::-1]}


# ---- fire detections: FIRMS satellite hotspots and CWFIS agency-reported fires ----------------------------

@app.get("/api/fires")
def fire_map(bbox: str | None = Query(None, pattern=r"^-?[\d.]+,-?[\d.]+,-?[\d.]+,-?[\d.]+$"),
             hours: int = Query(24, ge=1, le=fires.KEEP_HOURS), hotspots: bool = True, reported: bool = True):
    """Satellite hotspots (last `hours`) and the agency-reported fires in effect now (not time-limited)."""
    box = tuple(float(v) for v in bbox.split(",")) if bbox else None
    return fire_data.geojson(box, hours, hotspots=hotspots, reported=reported)


@app.get("/api/fires/area/{aoi}")
def fires_near(aoi: str, hours: int = Query(24, ge=1, le=fires.KEEP_HOURS), km: float = Query(fires.NEAR_KM, ge=0, le=200)):
    return fire_data.near(find_area(aoi), hours, km)


@app.get("/api/push/key")
def push_key():
    return {"key": pusher.public_key}


@app.post("/api/push/subscribe")
def push_subscribe(subscription: dict = Body(...), areas: list[str] | str = Body(...)):
    if isinstance(areas, str) and areas != "all":
        raise HTTPException(400, 'areas must be a list of area ids or "all"')
    _, known = load_areas()
    ids = {a["id"] for a in known}
    if areas != "all" and not set(areas) <= ids:
        raise HTTPException(400, "unknown area")
    try:
        return pusher.subscribe(subscription, areas)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/push/status")
def push_status(endpoint: str = Body(..., embed=True)):
    return pusher.status(endpoint)


@app.post("/api/push/unsubscribe")
def push_unsubscribe(endpoint: str = Body(..., embed=True)):
    pusher.unsubscribe(endpoint)
    return {"subscribed": False, "areas": []}


@app.post("/api/push/test")
async def push_test(endpoint: str = Body(..., embed=True)):
    if not await run_in_threadpool(pusher.send_test, endpoint):
        raise HTTPException(400, "couldn't send to this browser; turn notifications off and on again")
    return {"sent": True}


@app.get("/sw.js")
def service_worker():
    # served from the root so it can handle notifications for the whole site
    return FileResponse(ROOT / "static" / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(ROOT / "static" / "manifest.webmanifest", media_type="application/manifest+json")


def _versioned_index(page="index.html"):
    """index.html with ?v=<content hash> on its own scripts and stylesheet.

    Browsers (installed phone apps especially) cache /static files for a while and could run an old panel.js
    under a new index.html. A new hash after every deploy makes them fetch the new files.
    """
    html = (ROOT / "static" / page).read_text(encoding="utf-8")

    def ver(m):
        f = ROOT / "static" / m[2]
        if not f.is_file():
            return m[0]
        return f'{m[1]}/static/{m[2]}?v={hashlib.sha1(f.read_bytes()).hexdigest()[:10]}"'
    return re.sub(r'((?:src|href)=")/static/([\w.-]+\.(?:js|css))"', ver, html)


_INDEX = _versioned_index()


@app.get("/")
def index():
    return Response(_INDEX, media_type="text/html", headers={"Cache-Control": "no-cache"})


_RANGE = _versioned_index("range.html")


@app.get("/range")
def range_panel():
    """Wall display for range control and the fire hall: ECCC alerts with acknowledgement, next 12 h wind."""
    return Response(_RANGE, media_type="text/html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
