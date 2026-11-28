"""CWFIS fire weather forecast: a map of the CWFIS daily layers and a forecast PDF for each area.

Run:  uvicorn app:app --host 127.0.0.1 --port 8000
"""
import hashlib
import html
import json
import logging
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool

import alerts
import cwfis
import fires
import goes
import smoke
import status as base_status
import snowdepth
import push
import report_pdf
import rdps
import wxpoint
import obs as obs_mod
import report as report_mod
import health as health_mod
import usage as usage_mod

ROOT = Path(__file__).resolve().parent
AOI_FILE = ROOT / "aois.json"
DATA = ROOT / "data"  # alert state, push keys and subscriptions (not in git)
MAX_DAYS = 12  # longest report
TZ = ZoneInfo(os.environ.get("FW_TZ", "America/Winnipeg"))  # decides "today" (obs vs forecast) and the issue time


def today():
    return datetime.now(TZ).date()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
snowdepth.HISTORY_FILE = DATA / "snow_history.json"  # a daily sample of snow cover per area
log_tally = report_mod.LogTally()  # warnings and errors, counted for the weekly report
logging.getLogger().addHandler(log_tally)
log = logging.getLogger("app")
pusher = push.Push(DATA)
wx_alerts = alerts.Alerts(AOI_FILE, DATA / "alerts_state.json", on_new=pusher.notify_alert)
rdps_maps = rdps.Rdps(DATA)
_aoi_list = lambda: json.loads(AOI_FILE.read_text(encoding="utf-8"))["areas"]
smoke_watch = smoke.SmokeWatch(DATA, pusher.notify_alert, _aoi_list, TZ)
_smoke_hook = smoke_watch.check if os.environ.get("FW_SMOKE_PUSH", "1") != "0" else None
bluesky = smoke.BlueSky(DATA, areas_fn=_aoi_list, on_update=_smoke_hook)
eer_plume = smoke.Eer(DATA, areas_fn=_aoi_list, on_update=_smoke_hook)
# regional EER run (finer contours), map layer only for now; FW_EER_REGION="" turns it off
EER_REGION = os.environ.get("FW_EER_REGION", "MB_ON_South")
eer_region = smoke.Eer(DATA, domain=EER_REGION, areas_fn=_aoi_list, name="eer_region") if EER_REGION else None
smoke_watch.sources = [eer_plume, bluesky]


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
usage_log = usage_mod.Usage(DATA)
obs_data = obs_mod.Obs()  # current station observations: ECCC (Canada) and Manitoba's fire weather network
ADMIN_TOKEN = os.environ.get("FW_ADMIN_TOKEN", "")  # turns on /admin; unset = no admin page

health = health_mod.Health(DATA, notify=lambda title, body: pusher.notify_admin(title, body))
health.add("ECCC alerts", health_mod.alerts_check(wx_alerts))
health.add("CWFIS", health_mod.cwfis_check(cwfis, today))


def _first_area():
    doc = json.loads(AOI_FILE.read_text(encoding="utf-8"))
    items = doc["areas"]
    return next((a for a in items if a["id"] == doc.get("default")), items[0] if items else None)


health.add("Open-Meteo forecast", health_mod.weather_check(wxpoint, _first_area))
if os.environ.get("FW_FIRES", "1") != "0":
    health.add("Fire detections", health_mod.fires_check(fire_data))
if os.environ.get("FW_OBS", "1") != "0":
    health.add("Station observations", health_mod.obs_check(obs_data))
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
        threading.Thread(target=eer_plume.loop, args=(_stop,), name="eer", daemon=True).start()
        if eer_region:
            threading.Thread(target=eer_region.loop, args=(_stop,), name="eer_region", daemon=True).start()
    if os.environ.get("FW_RDPS", os.environ.get("FW_WIND", "1")) != "0":
        health.add("RDPS wind", health_mod.rdps_check(rdps_maps, rdps.run_time))
    if os.environ.get("FW_SMOKE", "1") != "0":
        health.add("EER smoke plume", health_mod.smoke_check(eer_plume))
        if eer_region:
            health.add("EER regional plume", health_mod.smoke_check(eer_region))
        health.add("BlueSky smoke", health_mod.smoke_check(bluesky, seasonal=True))
    if os.environ.get("FW_OBS", "1") != "0":
        threading.Thread(target=obs_data.loop, args=(_stop,), name="obs", daemon=True).start()
    if os.environ.get("FW_REPORT", "1") != "0":
        threading.Thread(target=weekly.loop, args=(_stop,), name="report", daemon=True).start()
    if os.environ.get("FW_HEALTH", "1") != "0":
        threading.Thread(target=health.loop, args=(_stop,), name="health", daemon=True).start()
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
async def forecast_pdf(request: Request, aoi: str, start: str | None = Query(None, pattern=r"^\d{4}-\d\d-\d\d$")):
    area = find_area(aoi)
    usage_log.record(_ip(request), "main", "pdf", aoi=area["id"], ua=request.headers.get("user-agent"))
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
    out["smoke"] = smoke.area_outlook(area["id"], (eer_plume, bluesky))
    b = area.get("bbox")
    lat, lon = ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2) if b else (area["lat"], area["lon"])
    out["obs"] = obs_data.near(lat, lon)
    try:
        out["weather"] = wxpoint.area_weather(area, rows)
    except Exception as e:
        log.warning("Open-Meteo for %s: %s", area["id"], e)
        out["errors"]["weather"] = "The weather forecast isn't available right now"
    return out


@app.get("/api/area/{aoi}")
async def area_panel(aoi: str):
    return await run_in_threadpool(_area_panel, find_area(aoi))


# ---- beta interface: base status (one statement, the items behind it, a 48 h timeline) ---------------------

def _status(area, force_season=None):
    panel = _area_panel(area)
    cur = wx_alerts.for_area(area["id"])
    alerts_area = _with_acks([cur])[0] if cur else None
    try:
        near = fire_data.near(area, 24)
    except Exception:
        near = None
    snow = _area_snow(area)
    return base_status.build(area, panel, alerts_area, near, (eer_plume, bluesky), snow=snow, force_season=force_season)


def _area_snow(area):
    b = area.get("bbox")
    lat, lon = ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2) if b else (area["lat"], area["lon"])
    try:
        return snowdepth.area_snow(area, obs_data.snow_near(lat, lon))
    except Exception as e:
        log.warning("snow cover for %s: %s", area["id"], e)
        return None


@app.get("/api/snow/area/{aoi}")
def snow_area(aoi: str):
    """Snow on the ground inside an area: CaLDAS, HRDPS and station snow depth, and the best estimate."""
    out = _area_snow(find_area(aoi))
    if out is None:
        raise HTTPException(502, "Neither CaLDAS nor HRDPS is answering")
    return out


@app.get("/api/status/{aoi}")
async def area_status(aoi: str, season: str | None = Query(None, pattern="^(winter|patchy|grass|growing)$")):
    """season= previews that seasonal mode (sample season text; the rest of the status is live)."""
    return await run_in_threadpool(_status, find_area(aoi), season)


# ---- RDPS frames for the map overlays: 10 m wind, precipitation rate and type ------------------------------

# ---- smoke forecasts: ECCC FireWork (GeoMet WMS) and BlueSky Canada (PNG frames from its netCDF) --------

@app.get("/api/smoke")
async def smoke_meta():
    return {"wms": smoke.WMS, "firework": await run_in_threadpool(smoke.firework), "bluesky": bluesky.meta(),
            "eer": eer_plume.meta(), "eer_region": eer_region.meta() if eer_region else None, "bins": smoke.PM_BINS}


@app.get("/api/smoke/bluesky/{run}/{i}.png")
def bluesky_frame(run: str, i: int):
    if not re.fullmatch(r"\d{12}", run):
        raise HTTPException(404)
    p = bluesky.path(run, i)
    if not p.exists():
        raise HTTPException(404, "no such hour")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=86400, immutable"})


@app.get("/api/smoke/area/{aoi}")
def smoke_area(aoi: str):
    """The smoke outlook for an area from the plume model and BlueSky: peak PM2.5, when, hours elevated."""
    return smoke.area_outlook(find_area(aoi)["id"], (eer_plume, bluesky))


@app.get("/api/smoke/eer/{run}/{i}.json")
def eer_frame(run: str, i: int, src: str = ""):
    if not re.fullmatch(r"\d{12}", run):
        raise HTTPException(404)
    plume = eer_region if src == "region" and eer_region else eer_plume
    p = plume.path(run, i)
    if not p.exists():
        raise HTTPException(404, "no such hour")
    return FileResponse(p, media_type="application/json", headers={"Cache-Control": "public, max-age=86400, immutable"})


@app.get("/api/rdps")
def rdps_meta():
    return rdps_maps.meta()


@app.get("/api/rdps/{run}/mslp/{fh}.json")
def rdps_mslp(run: str, fh: int):
    """Isobars and H/L centres for an RDPS hour (see rdps.mslp_features)."""
    if not re.fullmatch(r"\d{10}", run):
        raise HTTPException(404)
    p = rdps_maps.path(run, "mslp", fh)
    if not p.is_file():
        raise HTTPException(404, "no pressure for this hour")
    return FileResponse(p, media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=31536000, immutable"})


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


@app.get("/api/obs")
def observations(bbox: str | None = Query(None, pattern=r"^-?[\d.]+,-?[\d.]+,-?[\d.]+,-?[\d.]+$")):
    """Latest reading at each station (ECCC and Manitoba), optionally inside west,south,east,north."""
    box = tuple(float(x) for x in bbox.split(",")) if bbox else None
    return Response(json.dumps(obs_data.stations(box), separators=(",", ":")), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=120"})


@app.get("/api/obs/history/{sid}")
def observation_history(sid: str):
    """The last 24 h of hourly readings for a Manitoba station (ECCC readings aren't kept)."""
    if not re.fullmatch(r"[a-z]+:[\w-]{1,40}", sid):
        raise HTTPException(404)
    return {"id": sid, "rows": obs_data.history(sid)}


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


# ---- usage beacons, the about page and the admin page ----------------------------------------------------

def _ip(request):
    return request.client.host if request.client else "?"


@app.post("/api/usage", status_code=204)
async def usage_beacon(request: Request):
    """{page, ev, aoi, base, layers} from the pages; sent with navigator.sendBeacon (text/plain JSON)."""
    try:
        b = json.loads((await request.body())[:4000] or b"{}")
    except ValueError:
        return Response(status_code=204)
    if isinstance(b, dict):
        ok = usage_log.record(_ip(request), b.get("page"), b.get("ev", "view"), aoi=b.get("aoi"), base=b.get("base"),
                              layers=b.get("layers"), ua=request.headers.get("user-agent"), sid=b.get("sid"),
                              secs=b.get("secs"), act=b.get("act"), vote=b.get("vote"), text=b.get("text"))
        if ok and b.get("ev") == "fb":  # feedback is rare and worth seeing right away
            where = f" ({b['aoi']})" if isinstance(b.get("aoi"), str) and re.fullmatch(r"[\w-]{1,40}", b["aoi"]) else ""
            text = re.sub(r"[\x00-\x1f\x7f]", " ", b["text"]) if isinstance(b.get("text"), str) else ""
            await run_in_threadpool(pusher.notify_admin, f"FWX feedback {'👍' if b['vote'] == 'up' else '👎'}{where}",
                                    text.strip()[:200] or "(no comment)")
    return Response(status_code=204)


def _admin(token):
    if not ADMIN_TOKEN or not token or not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(404)


@app.get("/api/admin/usage")
def admin_usage(days: int = Query(14, ge=1, le=120), exclude: str = "", x_admin_token: str = Header("")):
    _admin(x_admin_token)
    out = usage_log.summary(days, exclude={s.strip() for s in exclude.split(",") if s.strip()})
    _, known = load_areas()
    names = {a["id"]: a["name"] for a in known}
    for b in out["by_base"]:
        b["name"] = names.get(b["aoi"], b["aoi"])
    return out


@app.get("/api/admin/health")
def admin_health(x_admin_token: str = Header("")):
    _admin(x_admin_token)
    return {"checks": health.report(), "admin_devices": pusher.admin_count()}


@app.post("/api/admin/health/run")
async def admin_health_run(x_admin_token: str = Header("")):
    _admin(x_admin_token)
    await run_in_threadpool(health.run_once)
    return {"checks": health.report()}


@app.post("/api/admin/push/subscribe")
def admin_push_subscribe(subscription: dict = Body(...), label: str = Body(""), x_admin_token: str = Header("")):
    _admin(x_admin_token)
    try:
        return {"admin_devices": pusher.add_admin(subscription, label)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/admin/push/remove")
def admin_push_remove(endpoint: str = Body(..., embed=True), x_admin_token: str = Header("")):
    _admin(x_admin_token)
    return {"admin_devices": pusher.remove_admin(endpoint)}


@app.post("/api/admin/push/test")
async def admin_push_test(x_admin_token: str = Header("")):
    _admin(x_admin_token)
    n = await run_in_threadpool(pusher.notify_admin, "FWX admin test", "Health notices will arrive like this.")
    return {"sent": n}


weekly = report_mod.Weekly(DATA, TZ, lambda: report_mod.build(usage_log, health.events(), log_tally, load_areas()[1]))


@app.get("/api/admin/report")
async def admin_report(x_admin_token: str = Header("")):
    """This week's report as it stands now (not published), and when the last one was sent."""
    _admin(x_admin_token)
    text = await run_in_threadpool(lambda: report_mod.build(usage_log, health.events(), log_tally, load_areas()[1]))
    return {"text": text, "last": weekly.last(), "repo": os.environ.get("FW_REPORT_REPO") or None}


@app.post("/api/admin/report/publish")
async def admin_report_publish(x_admin_token: str = Header("")):
    _admin(x_admin_token)
    day, _, status = await run_in_threadpool(weekly.make)
    return {"day": day, "status": status}


@app.get("/admin")
def admin_page():
    if not ADMIN_TOKEN:
        raise HTTPException(404)
    return Response(_ADMIN, media_type="text/html", headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex"})


@app.get("/about")
def about_page():
    return Response(_ABOUT, media_type="text/html", headers={"Cache-Control": "no-cache"})


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
_ADMIN = _versioned_index("admin.html")


def _about():
    who = html.escape(os.environ.get("FW_ABOUT_CONTACT", "Rob, RTA Sustainability Officer, CFB Shilo"))
    mail = os.environ.get("FW_ABOUT_EMAIL", "")
    if re.fullmatch(r"[^@\s<>\"]+@[^@\s<>\"]+", mail):
        who += f' · <a href="mailto:{html.escape(mail)}">{html.escape(mail)}</a>'
    return _versioned_index("about.html").replace("{{CONTACT}}", who)


_ABOUT = _about()


_BETA = _versioned_index("beta.html")


@app.get("/beta")
@app.get("/beta/")
def beta_page():
    """Test interface, message first (see the interface review). Served at fwx-beta.rtams.ca too."""
    return Response(_BETA, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/range")
def range_panel():
    """Wall display for range control and the fire hall: ECCC alerts with acknowledgement, next 12 h wind."""
    return Response(_RANGE, media_type="text/html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
