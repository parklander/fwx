"""CWFIS fire weather forecast: a map of the CWFIS daily layers and a forecast PDF for each area.

Run:  uvicorn app:app --host 127.0.0.1 --port 8000
"""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool

import cwfis
import report_pdf

ROOT = Path(__file__).resolve().parent
AOI_FILE = ROOT / "aois.json"
MAX_DAYS = 12  # longest report
TZ = ZoneInfo(os.environ.get("FW_TZ", "America/Winnipeg"))  # decides "today" (obs vs forecast) and the issue time


def today():
    return datetime.now(TZ).date()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("app")
app = FastAPI(title="CWFIS fire weather forecast")


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
    pdf = await run_in_threadpool(report_pdf.build, area, rows, today(), datetime.now(TZ))
    name = re.sub(r"[^A-Za-z0-9]+", "-", area["name"]).strip("-")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="fire-weather-{name}-{first.isoformat()}.pdf"'})


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
