# CWFIS Fire Weather Forecast

A small web page with three pickers (area, date, layer) over a map of the CWFIS daily fire weather grids, plus a forecast PDF for the selected area. Fire weather comes from the Canadian Wildland Fire Information System (NRCan) and alerts from Environment and Climate Change Canada: no model downloads.

- **Map:** the CWFIS layer (FWI, ISI, FFMC, BUI, DMC, DC, fire danger, head fire intensity) for the chosen date, straight from CWFIS's WMS, with its legend. The area's reference point and bounding box are drawn on top.
- **Forecast PDF:** for the chosen area, from the day before the selected date to the end of the CWFIS forecast (up to 12 days). It includes:
  - noon temperature, RH, wind and rain, the FWI System codes and indices, and the CWFIS fire danger, at the area's reference point;
  - the highest FWI and danger of any cell in the area's bounding box;
  - an FWI chart coloured by danger, and the source notes.

## Alerts and notifications

The server checks ECCC public weather alerts (warnings, watches, statements, including Special Air Quality Statements) for every area every 5 minutes:

- An area with an `alert_feed` code in `aois.json` (CFB Shilo uses `mbrm67`) reads that ECCC alert region's feed, the same one weather.gc.ca's warnings page uses. To find a code, open the region on weather.gc.ca/warnings and take it from its RSS link.
- Other areas use the MSC GeoMet alerts API at the area's reference point.

On the page, areas with alerts show ⚠ on the map, the **Alerts** button lists them, and the forecast PDF lists any alerts in effect at the top.

**🔔 Notify me** turns on push notifications on that device for the areas you tick (or all areas). They arrive even with the page closed, whenever ECCC issues a new alert, and tapping one opens the site on that area. Android and desktop browsers work directly. On iPhone or iPad, add the site to the Home Screen first (Share → Add to Home Screen) and turn notifications on from there; Apple only allows web notifications for installed sites.

The server keeps its notification key, the subscriptions and the alerts it has already announced in `data/`, which isn't in git. Alerts already in effect when the server first starts aren't announced, and neither are alerts that were already announced before a restart. `FW_CONTACT` (default `https://fwx.rtams.ca`) is the contact address sent to the browsers' push services.

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

The smallest droplet is plenty: the server only queries CWFIS and keeps a short in-memory cache. Push this folder to a GitHub repo, point a domain's DNS at the droplet (optional), then:

```
ssh root@<droplet-ip>
curl -fsSLO <raw URL of deploy/setup.sh in your repo>
bash setup.sh https://github.com/<you>/<repo>.git fireweather.example.ca
```

With no domain, it uses `<ip>.sslip.io` with a real HTTPS certificate. Updates: `bash /opt/cwfis-forecast/deploy/update.sh`. Logs: `journalctl -u cwfis-forecast -f`.

## Notes

- A full PDF is about 150 small WCS requests to CWFIS (12 grids × up to 12 days), which takes 10–30 s the first time. Results are cached for 3 hours.
- CWFIS doesn't label which dates are observations and which are forecast. The PDF treats dates before today as observed.
- The data access code comes from the fire-modeling viewer's `firewx.py`, which has been checked against CWFIS.
