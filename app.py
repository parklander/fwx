"""CWFIS fire weather forecast: a map of the CWFIS daily layers and a forecast PDF for each area.

Run:  uvicorn app:app --host 127.0.0.1 --port 8000
"""
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
import push
import report_pdf
import wind
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
rdps_wind = wind.Wind(DATA)
_stop = threading.Event()


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=wx_alerts.loop, args=(_stop,), name="alerts", daemon=True).start()
    if os.environ.get("FW_WIND", "1") != "0":
        threading.Thread(target=rdps_wind.loop, args=(_stop,), name="wind", daemon=True).start()
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
    out = {"area": area, "today": t.isoformat(), "fwi_days": [], "weather": None, "errors": {}}
    rows = []
    try:
        first, last = _period((t - timedelta(days=1)).isoformat())
        rows = cwfis.area_daily(area, first, last)  # same days and cache as the default PDF
        out["fwi_days"] = [r for r in rows if t.isoformat() <= r["date"] <= (t + timedelta(days=2)).isoformat()]
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


# ---- RDPS 10 m wind frames for the map overlay ----------------------------------------------------------

@app.get("/api/wind")
def wind_meta():
    return rdps_wind.meta()


@app.get("/api/wind/{run}/{var}/{fh}")
def wind_frame(run: str, var: str, fh: int):
    if not re.fullmatch(r"\d{10}", run) or var not in wind.VARIABLES:
        raise HTTPException(404)
    p = rdps_wind.path(run, var, fh)
    if not p.is_file():
        raise HTTPException(404, "no such wind frame (a newer run may have replaced it)")
    # stored gzipped; the browser inflates it
    return FileResponse(p, media_type="application/octet-stream",
                        headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=31536000, immutable"})


# ---- ECCC alerts and push notifications ----------------------------------------------------------

@app.get("/api/alerts")
def weather_alerts():
    return {"poll_min": alerts.POLL_MIN, "areas": wx_alerts.view()}


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


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
