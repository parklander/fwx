# CWFIS Fire Weather Forecast

A small web page with three pickers (area, date, layer) over a map of the CWFIS daily fire weather grids, plus a forecast PDF for the selected area. Fire weather comes from the Canadian Wildland Fire Information System (NRCan), the point forecast from ECCC's GEM models via Open-Meteo, and alerts from Environment and Climate Change Canada. The only model downloads are the RDPS wind and precipitation for the map overlays.

- **Map:** the CWFIS layer (FWI, ISI, FFMC, BUI, DMC, DC, fire danger, head fire intensity) for the chosen date, straight from CWFIS's WMS, with its legend. The area's reference point and bounding box are drawn on top.
- **Forecast PDF:** for the chosen area, from the day before the selected date to the end of the CWFIS forecast (up to 12 days). It includes:
  - noon temperature, RH, wind and rain, the FWI System codes and indices, and the CWFIS fire danger, at the area's reference point;
  - the highest FWI and danger of any cell in the area's bounding box;
  - an FWI chart coloured by danger, and the source notes.

## Area panel

Picking an area (from the list, or by tapping it on the map) opens a panel at the bottom of the screen, beside the map on wider screens, with four tabs:

- **Forecast:** the next 24 hours hour by hour, and a 7-day outlook (high/low, rain, wind and gusts, lowest RH, peak FWI) from ECCC's GEM models via [Open-Meteo](https://open-meteo.com/) (`gem_seamless`: HRDPS, then RDPS, then GDPS).
- **Fire weather:** hourly meteograms for temperature, RH, wind and gusts, rain, FFMC and FWI over 48 hours, 3 days or 7 days. Touch or hover any chart to read every value at that hour.
- **FWI 3 days:** CWFIS's daily values for today, tomorrow and the day after: danger class, FWI, the area's worst cell, ISI, BUI, FFMC, DMC, DC and noon weather.
- **Rx:** each forecast hour from 0600 to 2100 checked against prescribed burn limits, as a timeline per day (3 or 7 days): green **GO** (within every enabled limit), yellow **CAUTION** (within every limit but close to one), red **NO-GO** (outside at least one), grey where a value isn't forecast yet. The ignition period (default 1100–1700) is outlined. Each day gets a verdict, its GO windows and the limits that stopped it most often. Tapping an hour shows every value with ✓, ! or ✗.

  The default limits are the fire-modeling viewer's prescription (Annex A): temperature 10–24 °C, RH 20–45 %, wind ≤ 22 km/h, FFMC 77–90, ISI 4–7, FWI < 12, DMC < 25, DC < 210, BUI < 30. Gusts, wind direction and rain in the hour are there but off. *Prescription limits* edits them and turns them on or off, and they're saved in the browser for each area. The caution margins are ±2 °C, ±5 % RH, 3 km/h wind, 5 km/h gusts, 15° direction, 1 FFMC, 0.5 ISI, 1 FWI, 2 DMC, 15 DC and 3 BUI.

  Weather and hourly FFMC, ISI and FWI are the Fire weather tab's. DMC, DC and BUI are CWFIS's daily values, so hours past the end of the CWFIS forecast are grey while those limits are on. Atmospheric stability, ventilation index and air quality, which the fire-modeling prescription also checks, aren't available here.

The hourly FFMC, ISI and FWI in the first two tabs are calculated by this app ([wxpoint.py](wxpoint.py)), not by CWFIS. They start from CWFIS's daily FFMC at noon yesterday and step through the Open-Meteo hourly weather with the hourly FFMC equations (Van Wagner 1977, as in the cffdrs package's `hffmc`), using CWFIS's daily BUI. They show the shape of the day: overnight recovery, the afternoon peak, the effect of rain.

## Basemap

The **Basemap** setting picks the map underneath. The choices are Esri's **Dark gray** canvas (the default), Esri **Imagery** and **OpenStreetMap**. The Esri maps put their place names above the fire weather, satellite and precipitation layers. On the two dark maps, area outlines are drawn light and wind streaks are drawn white. Esri basemaps are used under the [Esri terms of use](https://www.esri.com/en-us/legal/terms/full-master-agreement), linked in the map's attribution.

## Satellite

The **Layer** list has a *Satellite (GOES)* group with **Natural colour**: the latest GOES 1 km image from ECCC GeoMet (WMS), new every 10 minutes. In it land is green-brown, water clouds are white, ice clouds and snow are cyan, and smoke is a grey-tan haze. The image is blank at night. The map uses GOES-West when it's centred west of 105° W, where that satellite sees at a much better angle, and GOES-East elsewhere. The date picker is greyed out while it's showing, and wind streaks turn white over it. The server only looks up each layer's latest image time ([goes.py](goes.py), cached 5 minutes); the browser fetches the images from GeoMet directly.

## Wind and precipitation (RDPS)

**Wind** (Off, Particles, Arrows) draws ECCC's RDPS 10 m wind over the map: animated streaks that flow with the wind (faster is darker and heavier, red above about 55 km/h; white over the satellite image), or arrows pointing downwind.

**Precip** (Off, Rate by type) colours the RDPS precipitation that falls in the hour ending at the time shown, with the same types, colours and labels as the fire-modeling viewer: one intensity ramp each for rain, rain/snow mix, freezing rain, ice pellets and snow, from 0.05 mm/h (faint) upward, and nothing where it's dry. The key under the hour bar runs 0.1 → 10 mm/h for each type.

**Precip → Snow from now** adds up the snow from the current hour to the hour on the bar: each hour's RDPS precipitation classed as snow, plus half of any rain/snow mix, shown as snowfall at 10:1 (1 mm of water ≈ 1 cm of snow). The page works it out from the same hourly frames, so the server does nothing extra. The colours run from pale blue at 1 cm to deep purple at 50 cm or more, and the readout gives the total at the pointer, e.g. "Snow since now ≈ 12 cm".

Both use a bar at the top of the map that steps through the hours from now to 48 hours ahead (−1h / +1h, or ▶ to play them). Hovering or tapping the map shows the values there, e.g. "Snow 0.8 mm/h · 24 km/h from NW (315°)".

The server ([rdps.py](rdps.py)) checks the MSC Datamart every 10 minutes for a newer RDPS run. Once a run's files reach 48 hours past now, it downloads for each hour `WindSpeed_AGL-10m`, `WindDir_AGL-10m`, `Precip-Accum_Sfc`, `PrecipType-Instant_Sfc` and `AirTemp_AGL-2m` (about 260 GRIB2 files), regrids them from the rotated lat-lon grid onto a Web Mercator raster over Canada, and keeps one-byte frames in `data/rdps/`:

- wind speed (km/h) and direction (2° steps);
- precipitation: rate = this hour's accumulation minus the last hour's, and type = the RDPS instantaneous type, or, where it stopped by the end of the hour, the 2 m temperature (≤ 0 °C snow, < 2 °C mix, else rain), as in fire-modeling's `precip.py`. Rate and type share one byte: 50 rate steps per type on a log scale from 0.05 to 50 mm/h.

Only the newest complete run is kept, and the previous one is served until the next has fully arrived. The browser fetches one hour at a time (up to three small frames). Until the first run is in, the page re-checks every 30 seconds.

RDPS has no 2 m wind. 10 m is the height the FWI System, the FBP System and CWFIS use.

`FW_RDPS=0` turns the downloads off. `FW_WIND_HOURS` (default 48) sets how far ahead they go.

## Snow depth (ECCC)

The **Layer** list has two snow depth layers from ECCC's MSC GeoMet map service ([snowdepth.py](snowdepth.py) reads each one's current time window):

- **Snow on ground (CaLDAS analysis):** `CaLDAS-NSRPS_2.5km_SnowDepth`, the CaLDAS-NSRPS land-surface analysis of snow on the ground, 2.5 km, every 3 hours. The map shows the latest analysis. ([product](https://eccc-msc.github.io/open-data/msc-data/nwp_caldas-nsrps/readme_caldas-nsrps_en/))
- **Snow depth forecast (HRDPS):** `HRDPS.CONTINENTAL_SD`, hourly, about 48 hours ahead, starting from the CaLDAS analysis and adding new snowfall and melt. It follows the hour on the wind/precip bar when that's open, otherwise the hour nearest now. ([product](https://eccc-msc.github.io/open-data/msc-data/nwp_hrdps/readme_hrdps_en/))

Both use GeoMet's `SNOWDEPTH` style and legend. If a layer's newest model run is more than 18 hours old, the legend says ECCC hasn't updated it. Data Source: Environment and Climate Change Canada ([licence](https://eccc-msc.github.io/open-data/licence/readme_en/)).

## Alerts and notifications

The server checks ECCC public weather alerts (warnings, watches, statements, including Special Air Quality Statements) for every area every 5 minutes:

- An area with an `alert_feed` code in `aois.json` (CFB Shilo uses `mbrm67`) reads that ECCC alert region's feed, the same one weather.gc.ca's warnings page uses. To find a code, open the region on weather.gc.ca/warnings and take it from its RSS link.
- Other areas use the MSC GeoMet alerts API at the area's reference point.

On the page, areas with alerts show ⚠ on the map, the **Alerts** button lists them, and the forecast PDF lists any alerts in effect at the top.

**🔔 Notify me** turns on push notifications on that device for the areas you tick (or all areas). They arrive even with the page closed, whenever ECCC issues a new alert, and tapping one opens the site on that area. Android and desktop browsers work directly. On iPhone or iPad, add the site to the Home Screen first (Share → Add to Home Screen) and turn notifications on from there; Apple only allows web notifications for installed sites.

The server keeps its notification key, the subscriptions and the alerts it has already announced in `data/`, which isn't in git. Alerts already in effect when the server first starts aren't announced, and neither are alerts that were already announced before a restart. `FW_CONTACT` (default `https://fwx.rtams.ca`) is the contact address sent to the browsers' push services.

## Weather station

The server takes uploads from an Ecowitt weather station (or anything that speaks the Ecowitt upload protocol) and keeps them in `data/station.sqlite`, converted to metric.

On the station's web page (or the WS View Plus app), under *Weather Services → Customized*: Enable, Protocol Type **Ecowitt**, Server **fwx.rtams.ca**, Protocol **HTTP**, Path **/api/station/**`<token>`, Port **80**, Upload Interval **60**. The token is the server's `FW_STATION_TOKEN` (put it in `/etc/fwx.env` on the droplet, e.g. `FW_STATION_TOKEN=…`, then `systemctl restart cwfis-forecast`). Without it, uploads are refused. Stations post over plain HTTP and don't follow redirects, so Caddy passes station uploads through on port 80 while everything else still redirects to HTTPS.

Each station is identified by a short hash of its PASSKEY; the PASSKEY itself isn't stored. The full upload is kept with each reading, so channels that aren't in the columns (soil moisture, extra sensors, battery levels) aren't lost.

- `GET /api/station`: stations that have uploaded, with their last reading time and whether they've gone quiet (no upload for 10 minutes).
- `GET /api/station/current`: the latest reading: temperature, RH, dew point, wind, gust, direction, rain (rate, today's, running total), pressure, solar radiation, UV, and its age.
- `GET /api/station/history?hours=24`: every reading in the period.
- `GET /api/station/hourly?hours=48`: hourly mean temperature, RH and wind, the highest gust, and the rain that fell in each hour (from the station's running total, so the reset at midnight doesn't count as rain). This is the hourly weather the FFMC/FWI calculations step through.

Add `?station=<id>` to any of them when there's more than one station; without it, they use the one that uploaded most recently. The FWI System assumes 10 m wind in the open, so a station on a house or a short mast will read low.

## Run locally

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --port 8000
```

Open http://127.0.0.1:8000. On Windows use `.venv\Scripts\...`.

## Areas

[aois.json](aois.json) lists the areas: `id`, `name`, `prov`, the reference point `lat`/`lon`, and an optional `bbox` [west, south, east, north]. `default` is the one the page opens with. Edits show up on the next page load without a restart. The starting list is the bases from the fire-modeling viewer.

## Settings

`FW_TZ` (default `America/Winnipeg`) sets which day counts as "today", which decides where the PDF switches from observed to forecast, and the time zone of the issue time.

## Deploy on DigitalOcean

The smallest droplet is plenty: the server queries CWFIS, keeps a short in-memory cache, and stores about 40 MB of RDPS frames. Push this folder to a GitHub repo, point a domain's DNS at the droplet (optional), then:

```
ssh root@<droplet-ip>
curl -fsSLO <raw URL of deploy/setup.sh in your repo>
bash setup.sh https://github.com/<you>/<repo>.git fireweather.example.ca
```

With no domain, it uses `<ip>.sslip.io` with a real HTTPS certificate. Updates: `bash /opt/cwfis-forecast/deploy/update.sh`. `update.sh` doesn't reinstall the Caddyfile or the service file; after a change to either, copy them as `setup.sh` does (the Caddyfile with `{DOMAIN}` replaced) and restart Caddy or the service. Logs: `journalctl -u cwfis-forecast -f`.

## Notes

- A full PDF is about 150 small WCS requests to CWFIS (12 grids × up to 12 days), which takes 10–30 s the first time. Results are cached for 3 hours.
- CWFIS doesn't label which dates are observations and which are forecast. The PDF treats dates before today as observed.
- The data access code comes from the fire-modeling viewer's `firewx.py`, which has been checked against CWFIS.
