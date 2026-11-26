/* CWFIS fire weather forecast: area, date and layer pickers over a map of the CWFIS daily grids */
(async function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const status = (t) => { $("status").textContent = t || ""; };
  const iso = (d) => d.toISOString().slice(0, 10);
  const today = iso(new Date(Date.now() - new Date().getTimezoneOffset() * 60000));
  const prefs = (() => { try { return JSON.parse(localStorage.getItem("cwfis-prefs") || "{}"); } catch (_) { return {}; } })();
  const save = () => { try { localStorage.setItem("cwfis-prefs", JSON.stringify({ aoi: $("aoi").value, layer: $("layer").value, wind: $("wind").value, precip: $("precip").value, basemap: $("basemap").value, fires: $("fires").value, repfires: $("repfires").value, smoke: $("smoke").value })); } catch (_) {} };

  const map = L.map("map", { zoomControl: true }).setView([55, -97], 4);

  // ---- basemaps: Esri dark gray canvas (default), Esri imagery or OpenStreetMap -------------------
  // Esri's place names go in their own pane above the fire weather, satellite and precipitation layers.
  map.createPane("labels");
  map.getPane("labels").style.zIndex = 420;
  map.getPane("labels").style.pointerEvents = "none";
  const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
  const ESRI_TERMS = '<a href="https://www.esri.com/en-us/legal/terms/full-master-agreement" target="_blank" rel="noopener">Esri terms</a>';
  const esri = (svc, o = {}) => L.tileLayer(`${ESRI}/${svc}/MapServer/tile/{z}/{y}/{x}`, o);
  const BASEMAPS = {
    dark: { label: "Dark gray", dark: true, make: () => [
      esri("Canvas/World_Dark_Gray_Base", { maxNativeZoom: 16, maxZoom: 18,
        attribution: `Esri, HERE, Garmin, © OpenStreetMap contributors, and the GIS user community · ${ESRI_TERMS}` }),
      esri("Canvas/World_Dark_Gray_Reference", { maxNativeZoom: 16, maxZoom: 18, pane: "labels" }),
    ] },
    imagery: { label: "Imagery", dark: true, make: () => [
      esri("World_Imagery", { maxNativeZoom: 19, maxZoom: 19,
        attribution: `Esri, Maxar, Earthstar Geographics, and the GIS User Community · ${ESRI_TERMS}` }),
      esri("Reference/World_Boundaries_and_Places", { maxNativeZoom: 19, maxZoom: 19, pane: "labels" }),
    ] },
    osm: { label: "OpenStreetMap", dark: false, make: () => [
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap contributors" }),
    ] },
  };
  $("basemap").innerHTML = Object.entries(BASEMAPS).map(([k, b]) => `<option value="${k}">${b.label}</option>`).join("");
  $("basemap").value = BASEMAPS[prefs.basemap] ? prefs.basemap : "dark";
  let baseLayers = [];
  const darkBase = () => BASEMAPS[$("basemap").value].dark;
  function setBasemap() {
    baseLayers.forEach((l) => map.removeLayer(l));
    baseLayers = BASEMAPS[$("basemap").value].make();
    baseLayers.forEach((l) => l.addTo(map));
    map.setMaxZoom(baseLayers[0].options.maxZoom);
    document.body.classList.toggle("dark-base", darkBase());
  }
  setBasemap();

  let wind = null;  // RDPS wind overlay, set up once the RDPS metadata arrives

  // ---- areas -----------------------------------------------------------------------------------
  const { areas } = await (await fetch("/api/areas")).json();
  const byId = Object.fromEntries(areas.map((a) => [a.id, a]));
  // the area list: the user's own base on top (if they set one), then by province west to east, A-Z within
  // ("CFB Borden" sorts under B); no base is favoured until the user picks theirs under Layers → My base
  const PROV = ["BC", "YT", "AB", "NT", "SK", "MB", "NU", "ON", "QC", "NB", "PE", "NS", "NL"];
  const PROV_NAME = { BC: "British Columbia", YT: "Yukon", AB: "Alberta", NT: "Northwest Territories", SK: "Saskatchewan", MB: "Manitoba",
    NU: "Nunavut", ON: "Ontario", QC: "Quebec", NB: "New Brunswick", PE: "Prince Edward Island", NS: "Nova Scotia", NL: "Newfoundland and Labrador" };
  const sortKey = (n) => n.replace(/^(CFB|CFS|CFD|BFC|CF Camp|Camp|Det|NDHQ|\d+ Wing)\s+/i, "").toLowerCase();
  const getBase = () => { try { const b = localStorage.getItem("fwx-base"); return byId[b] ? b : ""; } catch (_) { return ""; } };
  function areaOptions(base, placeholder) {
    const opt = (a) => `<option value="${a.id}">${a.name}${a.prov ? ` (${a.prov})` : ""}</option>`;
    const groups = {};
    for (const a of areas) (groups[a.prov || "Other"] ||= []).push(a);
    const order = Object.keys(groups).sort((x, y) => ((PROV.indexOf(x) + 1) || 99) - ((PROV.indexOf(y) + 1) || 99));
    return (placeholder ? `<option value="" disabled>${placeholder}</option>` : "")
      + (base ? `<optgroup label="My base">${opt(byId[base])}</optgroup>` : "")
      + order.map((p) => `<optgroup label="${PROV_NAME[p] || p}">${groups[p].sort((x, y) => sortKey(x.name).localeCompare(sortKey(y.name))).map(opt).join("")}</optgroup>`).join("");
  }
  function fillAreas() {
    const cur = $("aoi").value;
    $("aoi").innerHTML = areaOptions(getBase(), "Choose an area…");
    $("aoi").value = cur;
  }
  fillAreas();
  const linked = new URLSearchParams(location.search).get("aoi");  // e.g. from a notification
  $("aoi").value = byId[linked] ? linked : byId[prefs.aoi] ? prefs.aoi : getBase();
  // every area: its box (or a dot where it has none) with a label; click one to select it
  const areaLayer = L.layerGroup().addTo(map);
  const shapes = {};
  // area outlines: dark on the light OSM map, light on the dark gray and imagery basemaps
  const styleFor = (on) => on ? { color: "#e8590c", weight: 3, fill: true, fillColor: "#e8590c", fillOpacity: 0.12 }
    : darkBase() ? { color: "#f1f3f5", weight: 1.5, fill: true, fillColor: "#fff", fillOpacity: 0.06 }
    : { color: "#111", weight: 1.5, fill: true, fillColor: "#111", fillOpacity: 0.06 };
  const dotStyle = () => ({ color: darkBase() ? "#f1f3f5" : "#111" });
  for (const a of areas) {
    const g = L.featureGroup().addTo(areaLayer);
    if (a.bbox) {
      const [w, s, e, n] = a.bbox;
      L.rectangle([[s, w], [n, e]], styleFor(false)).addTo(g);
    }
    // the dot keeps small areas visible at country scale
    L.circleMarker([a.lat, a.lon], { radius: 4, ...dotStyle(), weight: 1.5, fillColor: darkBase() ? "#1d2329" : "#fff", fillOpacity: 1 }).addTo(g)
      .bindTooltip(a.name, { permanent: true, interactive: true, direction: "right", offset: [6, 0], className: "aoi-label" });
    g.on("click", () => { $("aoi").value = a.id; $("aoi").onchange(); });
    // a label hidden to avoid overlap shows while the pointer is on its dot or box
    g.on("mouseover", () => g.eachLayer((x) => {
      const el = x.getTooltip?.()?.getElement();
      if (el) { el.style.visibility = "visible"; el.style.zIndex = 1000; }
    }));
    g.on("mouseout", () => { g.eachLayer((x) => { const el = x.getTooltip?.()?.getElement(); if (el) el.style.zIndex = ""; }); declutter(); });
    shapes[a.id] = g;
  }
  // southern Canada, coast to coast, where the areas are
  const CANADA = L.latLngBounds([[42, -139], [61, -52.6]]);
  function showCountry() { map.fitBounds(CANADA); }

  // Hide labels that would overlap one already shown (the selected area's goes first); zooming in brings them back
  function declutter() {
    const sel = $("aoi").value;
    const order = [sel, ...areas.map((a) => a.id).filter((id) => id !== sel)].filter((id) => shapes[id]);
    const shown = [];
    for (const id of order) {
      let el = null;
      shapes[id].eachLayer((x) => { el = x.getTooltip?.()?.getElement() || el; });
      if (!el) continue;
      el.style.visibility = "visible";
      el.style.marginLeft = "";
      const hits = (r) => shown.some((o) => r.left < o.right + 2 && r.right > o.left - 2 && r.top < o.bottom + 1 && r.bottom > o.top - 1);
      let r = el.getBoundingClientRect();
      if (hits(r)) {
        el.style.marginLeft = `${-(r.width + 14)}px`;  // try the left side of the dot
        r = el.getBoundingClientRect();
      }
      if (hits(r)) el.style.visibility = "hidden"; else shown.push(r);
    }
  }
  map.on("zoomend moveend", declutter);
  // room taken on the right by the Layers panel (wide screens), so a fitted area isn't hidden under it
  const panelRight = () => {
    const c = $("controls");
    return document.body.classList.contains("settings-open") && c && !window.matchMedia("(max-width: 700px)").matches
      ? c.getBoundingClientRect().width + 12 : 0;
  };
  function showArea(fly, close) {
    const id = $("aoi").value, a = byId[id];
    for (const [k, g] of Object.entries(shapes)) {
      const on = k === id;
      g.eachLayer((x) => {
        if (x instanceof L.Rectangle) x.setStyle(styleFor(on));
        else x.getTooltip()?.getElement()?.classList.toggle("on", on);
      });
      if (on) g.bringToFront();
    }
    declutter();
    if (fly && a) {
      // keep the area clear of the forecast panel at the bottom
      const pad = window.fwxSheet?.padding() || { left: 0, bottom: 0 };
      const box = a.bbox ? L.latLngBounds([[a.bbox[1], a.bbox[0]], [a.bbox[3], a.bbox[2]]])
                         : L.latLng(a.lat, a.lon).toBounds(20000);
      // close: the base on opening the page, filling most of the map; otherwise with some country around it
      map.fitBounds(box.pad(a.bbox ? (close ? 0.15 : 1.5) : 0), { maxZoom: close ? 12 : 10, paddingTopLeft: [pad.left + 10, 10], paddingBottomRight: [panelRight() + 10, pad.bottom + 10] });
    }
  }
  // tell the forecast panel which area was picked
  const openPanel = () => document.dispatchEvent(new CustomEvent("area-selected", { detail: { id: $("aoi").value, name: byId[$("aoi").value].name } }));

  // ---- layers and dates ------------------------------------------------------------------------
  let info;
  try {
    const r = await fetch("/api/layers");
    if (!r.ok) throw new Error((await r.json()).detail);
    info = await r.json();
  } catch (e) {
    status(`CWFIS layers unavailable: ${e.message || e}`);
    info = { wms: "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/wms", layers: [] };
  }
  const layerById = Object.fromEntries(info.layers.map((l) => [l.id, l]));
  // GOES satellite imagery (ECCC GeoMet WMS): the latest image, 1 km, every 10 min
  const SAT = {
    sat_natural: { key: "natural", title: "Natural colour",
      desc: "Daytime colour image: land green-brown, water clouds white, ice clouds and snow cyan, smoke a grey-tan haze. Blank at night." },
  };
  const SAT_SPLIT_LON = -105;  // GOES-West west of here: it sees BC and Yukon at a far better angle
  const isSat = (id) => id in SAT;
  // Snow depth from ECCC GeoMet: the CaLDAS analysis of snow on the ground, and the HRDPS forecast (see snowdepth.py)
  const SNOWL = {
    snow_caldas: { key: "caldas", title: "Snow on ground (CaLDAS analysis)",
      desc: "Estimated snow on the ground from ECCC's CaLDAS land-surface analysis (2.5 km, every 3 h). No colour means no snow." },
    snow_hrdps: { key: "hrdps", title: "Snow depth forecast (HRDPS)",
      desc: "Forecast snow depth from ECCC's HRDPS (2.5 km, hourly, about 2 days ahead), starting from the CaLDAS analysis and adding new snowfall and melt. No colour means no snow." },
  };
  const isSnowL = (id) => id in SNOWL;
  // "none": just the basemap, e.g. to read the wind and precipitation overlays on their own
  $("layer").innerHTML = `<option value="none">None (basemap only)</option><optgroup label="CWFIS daily">${info.layers.map((l) => `<option value="${l.id}">${l.title}</option>`).join("")}</optgroup>`
    + `<optgroup label="Satellite (GOES)">${Object.entries(SAT).map(([id, s]) => `<option value="${id}">${s.title}</option>`).join("")}</optgroup>`
    + `<optgroup label="Snow (ECCC)">${Object.entries(SNOWL).map(([id, s]) => `<option value="${id}">${s.title}</option>`).join("")}</optgroup>`;
  $("layer").value = layerById[prefs.layer] || isSat(prefs.layer) || isSnowL(prefs.layer) || prefs.layer === "none" ? prefs.layer
    : info.layers.some((l) => l.id === "fwi") ? "fwi" : info.layers[0]?.id || "sat_natural";

  function setDateRange() {
    const l = layerById[$("layer").value];
    if (!l) return;
    $("date").min = l.start; $("date").max = l.end;
    if (!$("date").value) $("date").value = today <= l.end ? today : l.end;
    if ($("date").value > l.end) $("date").value = l.end;
    if ($("date").value < l.start) $("date").value = l.start;
  }

  let wms = null, GOES = null;
  async function loadGoes() {
    try { GOES = await (await fetch("/api/goes")).json(); } catch (_) { return; }
    if (isSat($("layer").value)) showLayer();
  }
  const satSide = () => (map.getCenter().lng < SAT_SPLIT_LON ? "West" : "East");

  function showSat() {
    const s = SAT[$("layer").value], t = GOES?.layers?.[`${satSide().toLowerCase()}:${s.key}`];
    $("legend-img").hidden = true;
    $("legend-title").textContent = `GOES-${satSide()} ${s.title.toLowerCase()}`;
    if (!t) {
      if (wms) { map.removeLayer(wms); wms = null; }
      $("legend-date").textContent = GOES ? "Satellite images are unavailable right now." : "Loading…";
      $("legend").hidden = false;
      return;
    }
    if (!wms || wms.options.layers !== t.layer || wms._url !== GOES.wms) {
      if (wms) map.removeLayer(wms);
      wms = L.tileLayer.wms(GOES.wms, { layers: t.layer, format: "image/png", transparent: true, version: "1.3.0",
        time: t.end, opacity: 1, attribution: `GOES-${satSide()} imagery: ECCC GeoMet` }).addTo(map);
      wms.on("loading", () => status("Loading satellite image…"));
      wms.on("load", () => status(""));
    } else if (wms.wmsParams.time !== t.end) {
      wms.setParams({ time: t.end });
    }
    const when = new Date(t.end).toLocaleString("en-CA", { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" });
    $("legend-date").innerHTML = `${s.desc}<br><b>Image ${when}</b> · latest, refreshed every 10 min`;
    $("legend").hidden = false;
  }

  // ---- snow depth layers ----
  let SNOW = null, barHour = () => null;  // barHour: the wind/precip hour bar's hour, once that code has run
  async function loadSnow() {
    try { SNOW = await (await fetch("/api/snow")).json(); } catch (_) { return; }
    if (isSnowL($("layer").value)) showSnowL();
  }
  const isoHour = (d) => `${d.toISOString().slice(0, 13)}:00:00Z`;
  const fmtT = (s) => new Date(s).toLocaleString("en-CA", { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" });
  function snowLTime(key, L0) {  // -> [time, clamped]
    if (key === "caldas") return [L0.end, false];  // the latest analysis
    // the HRDPS forecast follows the wind/precip hour bar when it's open, otherwise the hour nearest now
    const h = !$("windbar").hidden ? barHour() : null;
    const want = h ? h.t : new Date(Math.round(Date.now() / 3600e3) * 3600e3);
    const t = Math.min(Math.max(want.getTime(), Date.parse(L0.start)), Date.parse(L0.end));
    return [isoHour(new Date(t)), Math.abs(t - want.getTime()) >= 3600e3];
  }
  function showSnowL() {
    const spec = SNOWL[$("layer").value], L0 = SNOW?.layers?.[spec.key];
    $("legend-title").textContent = spec.title;
    if (!L0) {
      if (wms) { map.removeLayer(wms); wms = null; }
      $("legend-img").hidden = true;
      $("legend-date").textContent = SNOW ? "This ECCC layer is unavailable right now." : "Loading…";
      $("legend").hidden = false;
      return;
    }
    const [time, clamped] = snowLTime(spec.key, L0);
    if (!wms || wms.options.layers !== L0.layer || wms._url !== SNOW.wms) {
      if (wms) map.removeLayer(wms);
      wms = L.tileLayer.wms(SNOW.wms, { layers: L0.layer, styles: L0.style, format: "image/png", transparent: true,
        version: "1.3.0", time, opacity: 0.8, attribution: "Snow depth: Data Source: Environment and Climate Change Canada" }).addTo(map);
      wms.on("loading", () => status("Loading snow depth…"));
      wms.on("load", () => status(""));
    } else if (wms.wmsParams.time !== time) {
      wms.setParams({ time });
    }
    $("legend-img").hidden = false;
    $("legend-img").src = `${SNOW.wms}?service=WMS&version=1.3.0&request=GetLegendGraphic&sld_version=1.1.0&format=image/png&layer=${L0.layer}&STYLE=${L0.style}`;
    const run = L0.run ? `${L0.run.slice(11, 13)}Z ${L0.run.slice(8, 10)}/${L0.run.slice(5, 7)}` : "";
    $("legend-date").innerHTML = `${spec.desc}<br><b>${spec.key === "caldas" ? "Analysis" : "Forecast for"} ${fmtT(time)}</b>`
      + (spec.key === "hrdps" && run ? ` · run ${run}` : "")
      + (clamped ? `<br>The HRDPS forecast doesn't cover the hour on the bar; this is its nearest hour.` : "")
      + (L0.stale ? `<br><b>ECCC hasn't updated this layer since the ${run} run, so this may be out of date.</b>` : "");
    $("legend").hidden = false;
  }

  function showLayer() {
    const id = $("layer").value, sat = isSat(id), snowL = isSnowL(id);
    $("date").disabled = sat || snowL;
    $("date").title = sat ? "The satellite layer shows the latest image" : snowL ? "Snow depth shows the latest analysis or the forecast hour" : "";
    wind?.setLight(!sat && !darkBase());  // white wind streaks over the satellite image and dark basemaps
    if (id === "none") {
      if (wms) { map.removeLayer(wms); wms = null; }
      $("legend").hidden = true;
      status("");
    } else if (sat) { showSat(); }
    else if (snowL) { showSnowL(); }
    else {
      const l = layerById[id];
      if (!l) return;
      setDateRange();
      const time = $("date").value;
      if (wms) map.removeLayer(wms);
      wms = L.tileLayer.wms(info.wms, {
        layers: l.id, format: "image/png", transparent: true, version: "1.3.0", time, opacity: 0.7,
        attribution: "NRCan CWFIS",
      }).addTo(map);
      wms.on("loading", () => status("Loading map…"));
      wms.on("load", () => status(""));
      $("legend-img").hidden = false;
      $("legend-title").textContent = l.title;
      $("legend-img").src = `${info.wms}?service=WMS&version=1.3.0&request=GetLegendGraphic&format=image/png&layer=${l.id}`;
      $("legend-date").textContent = `${time}${time >= today ? " (forecast)" : " (observed)"}`;
      $("legend").hidden = false;
    }
    areaLayer.eachLayer((g) => g.bringToFront());
    shapes[$("aoi").value]?.bringToFront();
  }
  // GOES-East/West swap as the map crosses SAT_SPLIT_LON; new images every 10 min
  map.on("moveend", () => { if (isSat($("layer").value)) showSat(); });
  setInterval(loadGoes, 5 * 60e3);
  setInterval(loadSnow, 10 * 60e3);

  function updatePdfLink() {
    const none = !$("aoi").value;
    $("pdf").classList.toggle("off", none);
    $("pdf").title = none ? "Choose an area first" : "Forecast PDF for the selected area";
    if (none) { $("pdf").removeAttribute("href"); return; }
    const d = $("date").value || today;
    const start = iso(new Date(Date.parse(d) - 86400e3));
    $("pdf").href = `/api/forecast.pdf?aoi=${encodeURIComponent($("aoi").value)}&start=${start}`;
  }

  $("aoi").onchange = () => { save(); openPanel(); showArea(true); updatePdfLink(); };
  $("country").onclick = showCountry;
  $("layer").onchange = () => { save(); showLayer(); };
  $("date").onchange = () => { showLayer(); updatePdfLink(); };
  $("pdf").onclick = (e) => {
    if (!$("aoi").value) { e.preventDefault(); status("Choose an area first, or set your base under Layers → My base."); return; }
    status("Building the PDF — CWFIS takes 10–30 s for a full forecast…");
  };

  // ---- My base: the user's own base, kept in this browser; it heads the area list and opens by default ----
  $("mybase").innerHTML = areaOptions("", "").replace(/^/, '<option value="">None</option>');
  $("mybase").value = getBase();
  $("mybase").onchange = () => {
    const b = $("mybase").value;
    try { b ? localStorage.setItem("fwx-base", b) : localStorage.removeItem("fwx-base"); } catch (_) {}
    fillAreas();
    if (b && !$("aoi").value) { $("aoi").value = b; $("aoi").onchange(); }
  };
  window.addEventListener("focus", () => { if ($("status").textContent.startsWith("Building")) status(""); });

  // ---- ECCC alerts ----------------------------------------------------------------------------------
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const RANK = { warning: 3, watch: 2, statement: 1 };
  const worst = (list) => list.reduce((w, x) => (RANK[x.level] > RANK[w] ? x.level : w), "statement");
  const when = (t) => new Date(t).toLocaleString("en-CA", { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  let ALERTS = null, alertsOpen = false;

  function markAlertedAreas() {
    const by = Object.fromEntries((ALERTS?.areas || []).map((a) => [a.id, a.alerts]));
    for (const a of areas) shapes[a.id].eachLayer((x) => {
      const tt = x.getTooltip?.();
      if (!tt) return;
      const list = by[a.id] || [];
      tt.setContent(list.length ? `⚠ ${esc(a.name)}` : esc(a.name));
      const el = tt.getElement();
      if (el) {
        el.classList.toggle("alert", list.length > 0);
        el.classList.remove("lvl-warning", "lvl-watch", "lvl-statement");
        if (list.length) el.classList.add(`lvl-${worst(list)}`);
      }
    });
    declutter();
  }

  function renderAlerts() {
    if (!ALERTS) return;
    const active = ALERTS.areas.filter((a) => a.alerts.length);
    const chip = $("alert-chip");
    chip.className = "chip" + (active.length ? ` on lvl-${worst(active.flatMap((a) => a.alerts))}` : "");
    chip.innerHTML = active.length ? `⚠<span class="long"> Alerts:</span> ${active.length}<span class="long"> area${active.length > 1 ? "s" : ""}</span>`
                                   : `<span class="long">No alerts</span><span class="short">✓ 0</span>`;
    markAlertedAreas();
    const panel = $("alerts");
    if (!alertsOpen) { panel.hidden = true; return; }
    const sel = ALERTS.areas.find((a) => a.id === $("aoi").value);
    const mine = sel?.alerts || [];
    const item = (x) => `<div class="wx-alert lvl-${x.level}"><div class="n">${esc(x.name)}</div>
      <div class="sub">${x.issued ? `Issued ${when(x.issued)}` : ""}${x.expires ? ` · expires ${when(x.expires)}` : ""}</div>
      ${x.text ? `<div class="t">${esc(x.text)}</div>` : ""}
      ${x.link || sel?.page ? `<div class="sub"><a href="${esc(x.link || sel.page)}" target="_blank" rel="noopener">Full text on weather.gc.ca ↗</a></div>` : ""}</div>`;
    const others = active.filter((a) => a !== sel);
    panel.innerHTML = `<h2><span>ECCC alerts · ${esc(byId[$("aoi").value]?.name)}</span><button class="x" id="al-close" aria-label="Close">×</button></h2>
      ${mine.length ? mine.map(item).join("") : `<div class="sub">No alerts in effect${sel?.region ? ` for ${esc(sel.region)}` : ""}.
        ${sel?.page ? `<a href="${esc(sel.page)}" target="_blank" rel="noopener">Check weather.gc.ca ↗</a>` : ""}</div>`}
      ${sel?.error ? `<div class="sub">Last check failed; showing the previous result.</div>` : ""}
      ${others.length ? `<div class="others"><div class="sub">Other areas</div>${others.map((a) =>
        `<button data-id="${esc(a.id)}" class="lvl-${worst(a.alerts)}"><b>⚠</b> ${esc(a.name)}: ${esc(a.alerts.map((x) => x.name).join(", "))}</button>`).join("")}</div>` : ""}
      <div class="sub" style="margin-top:8px">Checked every ${ALERTS.poll_min} min${sel?.checked ? `, last at ${new Date(sel.checked).toLocaleTimeString("en-CA", { hour: "2-digit", minute: "2-digit", hour12: false })}` : ""}.</div>`;
    panel.hidden = false;
    $("al-close").onclick = () => { alertsOpen = false; panel.hidden = true; };
    panel.querySelectorAll(".others button").forEach((b) => (b.onclick = () => { $("aoi").value = b.dataset.id; $("aoi").onchange(); }));
  }
  async function loadAlerts() {
    try { ALERTS = await (await fetch("/api/alerts")).json(); } catch (_) { return; }
    renderAlerts();
  }
  $("alert-chip").onclick = () => {
    alertsOpen = !alertsOpen; $("notify").hidden = true; closeSettings(); renderAlerts();
  };
  const aoiChange = $("aoi").onchange;
  $("aoi").onchange = () => { aoiChange(); renderAlerts(); };
  setInterval(loadAlerts, 2 * 60e3);

  // ---- push notifications (work with the page closed) ------------------------------------------------
  const pushable = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const b64 = (s) => Uint8Array.from(atob((s + "=".repeat((4 - (s.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const post = async (url, body) => {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || r.statusText);
    return d;
  };
  let swReg = null, mySub = { subscribed: false, areas: [] };
  async function pushState() {
    if (!pushable) return;
    swReg = swReg || await navigator.serviceWorker.register("/sw.js");
    const sub = await swReg.pushManager.getSubscription();
    mySub = sub ? await post("/api/push/status", { endpoint: sub.endpoint }).catch(() => ({ subscribed: false, areas: [] }))
                : { subscribed: false, areas: [] };
    $("bell").innerHTML = mySub.subscribed ? `🔔<span class="long"> Notifications on</span>` : `🔔<span class="long"> Notify me of alerts</span>`;
    $("bell").classList.toggle("on", mySub.subscribed);
  }
  function renderNotify(msg) {
    const p = $("notify");
    const ios = /iPhone|iPad/.test(navigator.userAgent) && !navigator.standalone;
    if (!pushable) {
      p.innerHTML = `<h2><span>Notifications</span><button class="x" id="nt-close">×</button></h2>
        <div class="sub">${ios ? "On iPhone or iPad, tap Share → <b>Add to Home Screen</b>, then open the site from the Home Screen icon and turn notifications on there."
          : "This browser can't receive web notifications."}</div>`;
    } else {
      const all = mySub.areas === "all";
      const chosen = new Set(all ? [] : mySub.areas || []);
      if (!mySub.subscribed && $("aoi").value) chosen.add($("aoi").value);
      p.innerHTML = `<h2><span>Notify me of new ECCC alerts</span><button class="x" id="nt-close">×</button></h2>
        <div class="sub">A notification on this device whenever ECCC issues a new warning, watch or statement for these areas, even with this page closed.</div>
        <label style="display:block;margin-top:8px"><input type="checkbox" id="nt-all"${all ? " checked" : ""}> <b>All areas</b></label>
        <div class="areas"${all ? ' style="opacity:.4;pointer-events:none"' : ""}>${areas.map((a) =>
          `<label><input type="checkbox" value="${esc(a.id)}"${chosen.has(a.id) ? " checked" : ""}> ${esc(a.name)}</label>`).join("")}</div>
        <div class="row"><button class="primary" id="nt-save">${mySub.subscribed ? "Save" : "Turn on"}</button>
          ${mySub.subscribed ? `<button id="nt-test">Send a test</button><button id="nt-off">Turn off</button>` : ""}</div>
        ${msg ? `<div class="sub" style="margin-top:8px">${esc(msg)}</div>` : ""}`;
      $("nt-all").onchange = (e) => { const box = p.querySelector(".areas"); box.style.opacity = e.target.checked ? ".4" : ""; box.style.pointerEvents = e.target.checked ? "none" : ""; };
      $("nt-save").onclick = () => turnOn().catch((e) => renderNotify(`Couldn't turn on notifications: ${e.message}`));
      if ($("nt-test")) $("nt-test").onclick = async () => {
        const sub = await swReg.pushManager.getSubscription();
        post("/api/push/test", { endpoint: sub.endpoint }).then(() => renderNotify("Test sent; it should appear in a few seconds."), (e) => renderNotify(e.message));
      };
      if ($("nt-off")) $("nt-off").onclick = turnOff;
    }
    $("nt-close").onclick = () => { p.hidden = true; };
    p.hidden = false;
  }
  async function turnOn() {
    const p = $("notify");
    const chosen = $("nt-all").checked ? "all" : [...p.querySelectorAll(".areas input:checked")].map((i) => i.value);
    if (chosen !== "all" && !chosen.length) return renderNotify("Pick at least one area.");
    if (await Notification.requestPermission() !== "granted") return renderNotify("Notifications are blocked for this site; allow them in the browser's site settings.");
    let sub = await swReg.pushManager.getSubscription();
    if (!sub) {
      const { key } = await (await fetch("/api/push/key")).json();
      sub = await swReg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(key) });
    }
    mySub = await post("/api/push/subscribe", { subscription: sub.toJSON(), areas: chosen });
    await pushState();
    renderNotify(chosen === "all" ? "On for all areas." : `On for ${chosen.length} area${chosen.length > 1 ? "s" : ""}.`);
  }
  async function turnOff() {
    const sub = await swReg.pushManager.getSubscription();
    if (sub) { await post("/api/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe(); }
    await pushState();
    renderNotify("Notifications are off on this device.");
  }
  $("bell").onclick = async () => {
    if (!$("notify").hidden) { $("notify").hidden = true; return; }
    alertsOpen = false; $("alerts").hidden = true; closeSettings();
    await pushState().catch(() => {});
    renderNotify();
  };
  pushState().catch(() => {});

  // ---- RDPS overlays for the next 48 hours: 10 m wind (particles or arrows) and precipitation rate by type ----
  // Both use the hour bar at the top of the map. Frames are byte rasters on a Web Mercator grid (see rdps.py).
  const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const hourLabel = (t) => t.toLocaleString("en-CA", { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" });
  let WM = null, rdpsHours = [], rdpsFrames = new Map(), shown = {}, playing = null, metaTimer = null;
  const curHour = () => rdpsHours[+$("wb-hour").value];
  barHour = curHour;
  const windOn = () => $("wind").value !== "off", precipOn = () => $("precip").value !== "off";

  // Precipitation rate (mm/h) coloured by type, as in the fire-modeling viewer: one intensity ramp per type,
  // transparent where dry. Index = type: 1 rain, 2 rain/snow mix, 3 freezing rain, 4 ice pellets, 5 snow.
  const PTYPE = [
    null,
    { key: "Rain", stops: [[0.05, "#9ec9ea", 0.35], [0.5, "#4a90d9", 0.8], [2, "#2a4fc8"], [5, "#a23bc4"], [10, "#f2c94c"], [25, "#ff4a3d"]] },
    { key: "Mix", stops: [[0.05, "#f3c4e8", 0.4], [0.5, "#e590d6", 0.85], [2, "#cc4fbd"], [5, "#8e2386"]] },
    { key: "Frz rain", stops: [[0.05, "#ffb3a8", 0.5], [0.5, "#ff7a6b", 0.9], [2, "#e5322a"], [5, "#a30d0d"]] },
    { key: "Pellets", stops: [[0.05, "#ffd9a0", 0.5], [0.5, "#ffb760", 0.9], [2, "#f08a1c"], [5, "#c25e00"]] },
    { key: "Snow", stops: [[0.05, "#eefaff", 0.4], [0.5, "#c4efff", 0.85], [2, "#7fd6f2"], [5, "#2fb0d6"]] },
  ];
  const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  function rampColor(st, v) {
    if (v <= st[0][0]) return [...hex(st[0][1]), st[0][2] ?? 1];
    for (let i = 1; i < st.length; i++) {
      if (v <= st[i][0]) {
        const [v0, c0, a0 = 1] = st[i - 1], [v1, c1, a1 = 1] = st[i];
        const t = (v - v0) / (v1 - v0), A = hex(c0), Bc = hex(c1);
        return [0, 1, 2].map((k) => A[k] + (Bc[k] - A[k]) * t).concat(a0 + (a1 - a0) * t);
      }
    }
    const l = st[st.length - 1];
    return [...hex(l[1]), l[2] ?? 1];
  }
  // precip byte -> (type, mm/h): 0 no data, 1 dry, else 2 + (type-1)*levels + level on a log scale
  const precipOf = (q) => {
    if (q < 2) return null;
    const P = WM.precip, k = q - 2, lvl = k % P.levels;
    return { type: Math.floor(k / P.levels) + 1, rate: P.min * Math.pow(P.top / P.min, lvl / (P.levels - 1)) };
  };
  let PLUT = null;
  function precipLut() {  // precip byte -> RGBA (little-endian ABGR)
    if (PLUT) return PLUT;
    PLUT = new Uint32Array(256);
    for (let q = 2; q < 256; q++) {
      const p = precipOf(q);
      if (!p || !PTYPE[p.type]) continue;
      const [r, g, b, a] = rampColor(PTYPE[p.type].stops, Math.min(p.rate, 30));
      PLUT[q] = ((Math.round(a * 255) << 24) | (Math.round(b) << 16) | (Math.round(g) << 8) | Math.round(r)) >>> 0;
    }
    return PLUT;
  }
  let pCanvas = null, pctx, pImg, pImg32, pOverlay = null;
  function drawPrecip(q) {
    if (!pCanvas) {
      pCanvas = document.createElement("canvas");
      pCanvas.width = WM.width; pCanvas.height = WM.height;
      pctx = pCanvas.getContext("2d");
      pImg = pctx.createImageData(WM.width, WM.height); pImg32 = new Uint32Array(pImg.data.buffer);
      const B = WM.bounds;  // rows are already Web Mercator, so stretching over the bounds lines up
      pOverlay = L.svgOverlay(pCanvas, [[B.south, B.west], [B.north, B.east]], { opacity: 0.85, interactive: false });
    }
    if (!q) { if (map.hasLayer(pOverlay)) pOverlay.remove(); return; }
    const T = precipLut();
    for (let i = 0; i < q.length; i++) pImg32[i] = T[q[i]];
    pctx.putImageData(pImg, 0, 0);
    if (!map.hasLayer(pOverlay)) pOverlay.addTo(map);
  }
  // ---- snow from now: the hourly snow (rate × hours classed as snow; half of rain/snow mix) added up from the
  // current hour to the hour on the bar, shown as snowfall at the usual 10:1 ratio (1 mm water ≈ 1 cm snow)
  const SNOW_STOPS = [[0.2, "#cde2fb", 0.45], [1, "#86b6ef", 0.75], [5, "#3987e5", 0.9], [10, "#1c5cab"], [25, "#104281"], [50, "#2b0a5c"]];
  const SNOW_TICKS = [1, 5, 10, 25, 50];
  const SNOW_FRAC = [0, 0, 0.5, 0, 0, 1];  // share of each type that counts as snow: mix half, snow all
  const snowMode = () => $("precip").value === "snow";
  let SNOWQ = null;  // precip byte -> mm of snow (water) if that rate fell for an hour
  function snowPerHour() {
    if (SNOWQ) return SNOWQ;
    SNOWQ = new Float32Array(256);
    for (let q = 2; q < 256; q++) { const p = precipOf(q); if (p) SNOWQ[q] = p.rate * SNOW_FRAC[p.type]; }
    return SNOWQ;
  }
  const snowSpan = (cm) => Math.log(cm / 0.2) / Math.log(60 / 0.2);  // key position for cm (log scale 0.2 → 60)
  let snowAcc = null;  // { run, from, k, cm: Float32Array, gaps } – the running total up to bar position k
  async function snowTotal(run, k) {
    const from = rdpsHours[0]?.valid;
    if (!snowAcc || snowAcc.run !== run || snowAcc.from !== from || snowAcc.k > k) {
      snowAcc = { run, from, k: 0, cm: new Float32Array(WM.width * WM.height), gaps: 0 };
    }
    const S = snowPerHour();
    // what fell in the hour ending at each bar position after the first (the first hour ended before now)
    for (let j = snowAcc.k + 1; j <= k; j++) {
      const h = rdpsHours[j];
      if (!h.precip) { snowAcc.gaps++; snowAcc.k = j; continue; }
      const q = await frame(run, "precip", h.fh);
      if (snowAcc.run !== run || snowAcc.k !== j - 1) return snowTotal(run, k);  // restarted meanwhile
      const cm = snowAcc.cm;
      for (let i = 0; i < q.length; i++) if (q[i] > 1) cm[i] += S[q[i]];
      snowAcc.k = j;
    }
    return snowAcc;
  }
  let SLUT = null;
  function snowLut() {  // 0.1 cm steps up to 102 cm -> RGBA
    if (SLUT) return SLUT;
    SLUT = new Uint32Array(1024);
    for (let i = 2; i < 1024; i++) {
      const [r, g, b, a] = rampColor(SNOW_STOPS, i / 10);
      SLUT[i] = ((Math.round(a * 255) << 24) | (Math.round(b) << 16) | (Math.round(g) << 8) | Math.round(r)) >>> 0;
    }
    return SLUT;
  }
  function drawSnow(acc) {
    if (!pCanvas || !acc) { drawPrecip(null); if (!acc) return; }  // makes the canvas the first time
    const T = snowLut(), cm = acc.cm;
    for (let i = 0; i < cm.length; i++) pImg32[i] = cm[i] < 0.2 ? 0 : T[Math.min(1023, Math.round(cm[i] * 10))];
    pctx.putImageData(pImg, 0, 0);
    if (!map.hasLayer(pOverlay)) pOverlay.addTo(map);
  }
  function drawSnowKey() {
    const c = $("wb-pkey"), ctx = c.getContext("2d"), w = c.width;
    ctx.clearRect(0, 0, w, c.height);
    for (let x = 0; x < w; x++) {
      const [r, g, b, a] = rampColor(SNOW_STOPS, 0.2 * Math.pow(60 / 0.2, x / (w - 1)));
      ctx.fillStyle = `rgba(${r},${g},${b},${Math.max(a, 0.35)})`;
      ctx.fillRect(x, 0, 1, c.height);
    }
    $("wb-pticks").innerHTML = SNOW_TICKS.map((v) => `<span style="left:${snowSpan(v) * 100}%">${v}${v === 50 ? " cm" : ""}</span>`).join("");
    $("wb-pk").title = "Snow added up from the current hour to the time shown, as snowfall at 10:1 (1 mm water ≈ 1 cm); rain/snow mix counts half";
  }

  function drawPrecipKey() {  // one gradient segment per type, 0.1 → 10 mm/h each
    const c = $("wb-pkey"), ctx = c.getContext("2d"), w = c.width, n = 5, bw = w / n;
    ctx.clearRect(0, 0, w, c.height);
    for (let k = 1; k <= n; k++) {
      for (let x = 0; x < bw - 2; x++) {
        const v = Math.exp(Math.log(0.1) + (x / (bw - 3)) * (Math.log(10) - Math.log(0.1)));
        const [r, g, b, a] = rampColor(PTYPE[k].stops, v);
        ctx.fillStyle = `rgba(${r},${g},${b},${Math.max(a, 0.35)})`;
        ctx.fillRect((k - 1) * bw + x, 0, 1, c.height);
      }
    }
    $("wb-pticks").innerHTML = PTYPE.slice(1).map((p, i) => `<span style="left:${((i + 0.5) / n) * 100}%">${p.key}</span>`).join("");
    $("wb-pk").title = "Precipitation in the hour ending at the time shown, by type; each segment runs 0.1 → 10 mm/h";
  }

  async function loadRdpsMeta() {
    let m;
    try { m = await (await fetch("/api/rdps")).json(); } catch (_) { return; }
    const keep = curHour()?.valid;
    WM = m;
    if (!wind && m.width) { wind = WindLayer(map, $("wind-canvas"), m, { light: !isSat($("layer").value) && !darkBase() }); wind.setMode("off"); }
    // hours from the current hour on (the run starts a little before now)
    const now = Date.now() - 3600e3;
    rdpsHours = (m.hours || []).map((h) => ({ ...h, t: new Date(h.valid.replace("Z", ":00Z")) })).filter((h) => h.t.getTime() >= now);
    $("wb-hour").max = Math.max(0, rdpsHours.length - 1);
    const k = rdpsHours.findIndex((h) => h.valid === keep);
    $("wb-hour").value = k >= 0 ? k : 0;
    $("wb-src").textContent = m.run ? `RDPS ${m.run.slice(8)}Z ${m.run.slice(6, 8)}/${m.run.slice(4, 6)}` : "";
    // re-check every 30 s until a run is in, then every 10 min
    clearTimeout(metaTimer);
    metaTimer = setTimeout(loadRdpsMeta, m.run ? 10 * 60e3 : 30e3);
    showRdps();
  }

  function frame(run, v, fh) {
    const key = `${run}/${v}/${fh}`;
    if (!rdpsFrames.has(key)) {
      rdpsFrames.set(key, fetch(`/api/rdps/${run}/${v}/${fh}`)
        .then((r) => { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); }).then((b) => new Uint8Array(b))
        .catch((e) => { rdpsFrames.delete(key); throw e; }));
      while (rdpsFrames.size > 64) rdpsFrames.delete(rdpsFrames.keys().next().value);
    }
    return rdpsFrames.get(key);
  }
  // the frames the current settings need for an hour
  const framesFor = (run, h) => Promise.all([
    windOn() ? frame(run, "wspd", h.fh) : null, windOn() ? frame(run, "wdir", h.fh) : null,
    precipOn() && !snowMode() && h.precip ? frame(run, "precip", h.fh) : null,
  ]);

  // key for the speed-coloured arrows: one swatch per band, the band edges (km/h) under it
  function drawWindKey() {
    const R = window.WindLayer?.RAMP;
    if (!R || $("wb-wbar").childElementCount) return;
    $("wb-wbar").innerHTML = R.map(([, c]) => `<i style="background:${c}"></i>`).join("");
    $("wb-wticks").innerHTML = R.slice(0, -1).map(([top], k) => `<span style="left:${((k + 1) / R.length) * 100}%">${top}</span>`).join("");
  }

  async function showRdps() {
    const mode = $("wind").value, pOn = precipOn(), sOn = smokeOn();
    $("windbar").hidden = mode === "off" && !pOn && !sOn;
    $("wb-pk").hidden = !pOn;
    $("wb-wk").hidden = mode !== "arrows";
    if (mode === "arrows") drawWindKey();
    if (pOn) (snowMode() ? drawSnowKey : drawPrecipKey)();
    if (mode === "off" && !pOn) { wind?.setMode("off"); if (pOverlay) drawPrecip(null); }
    if (mode === "off" && !pOn && !sOn) { stopPlay(); showSmoke(); return; }
    if (!WM?.run || !rdpsHours.length) {
      $("wb-time").textContent = WM?.status === "error" ? "Hour bar unavailable: RDPS forecast down"
        : WM?.run ? "The RDPS forecast is out of date" : "RDPS forecast is still downloading…";
      wind?.setFrames(null); showSmoke(); return;
    }
    if (wind.mode !== mode) wind.setMode(mode);
    const h = curHour(), run = WM.run;
    $("wb-time").textContent = hourLabel(h.t);
    showSmoke();
    try {
      const [spd, dir, pq] = await framesFor(run, h);
      if (curHour() !== h) return;  // moved on while loading
      const snow = pOn && snowMode() ? await snowTotal(run, +$("wb-hour").value) : null;
      if (curHour() !== h) return;
      shown = { spd, dir, pq, snow };
      wind.setFrames(spd, dir, "wspd", "wdir");
      if (snow) drawSnow(snow); else drawPrecip(pOn ? pq : null);
      if (snow) $("wb-time").textContent = `${hourLabel(h.t)} · snow since ${rdpsHours[0].t.toLocaleTimeString("en-CA", { hour: "2-digit", minute: "2-digit", hour12: false })}`;
      updateReadout();
      const nxt = rdpsHours[+$("wb-hour").value + 1];
      if (nxt) (snow ? frame(run, "precip", nxt.fh) : framesFor(run, nxt)).catch(() => {});  // prefetch the next hour
      if ($("layer").value === "snow_hrdps") showSnowL();
    } catch (_) {
      $("wb-time").textContent = "Couldn't load this hour";
    }
  }

  // values under the pointer (or a tap)
  const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
  let lastLL = null;
  function updateReadout(ll = lastLL) {
    lastLL = ll;
    if (!WM?.run || $("windbar").hidden) return;
    if (!ll) { $("wb-read").textContent = "Hover or tap the map for values"; return; }
    const B = WM.bounds;
    const x = Math.floor(((ll.lng - B.west) / (B.east - B.west)) * WM.width);
    const y = Math.floor(((mercY(B.north) - mercY(ll.lat)) / (mercY(B.north) - mercY(B.south))) * WM.height);
    const i = x >= 0 && x < WM.width && y >= 0 && y < WM.height ? y * WM.width + x : -1;
    const parts = [];
    if (precipOn() && snowMode()) {
      const v = i >= 0 && shown.snow ? shown.snow.cm[i] : null;
      parts.push(v == null ? "No precip data" : `Snow since now ≈ ${v < 10 ? v.toFixed(1) : Math.round(v)} cm${shown.snow.gaps ? " (some hours missing)" : ""}`);
    } else if (precipOn()) {
      const q = i >= 0 && shown.pq ? shown.pq[i] : 0, p = q ? precipOf(q) : null;
      parts.push(!q ? "No precip data" : p ? `${WM.precip.types[p.type]} ${p.rate < 1 ? p.rate.toFixed(2) : p.rate.toFixed(1)} mm/h` : "Dry");
    }
    if (windOn()) {
      const q = i >= 0 && shown.spd ? shown.spd[i] : 0, d = i >= 0 && shown.dir ? shown.dir[i] : 0;
      parts.push(q && d ? `${q - 1} km/h from ${COMPASS[Math.round(((d - 1) * 2) / 22.5) % 16]} (${(d - 1) * 2}°)` : "No wind data");
    }
    $("wb-read").textContent = parts.join(" · ");
  }
  map.on("mousemove click", (e) => updateReadout(e.latlng));

  function step(n) {
    const k = Math.min(Math.max(+$("wb-hour").value + n, 0), rdpsHours.length - 1);
    $("wb-hour").value = k; showRdps();
    return k;
  }
  function stopPlay() { clearTimeout(playing); playing = null; $("wb-play").textContent = "▶︎"; $("wb-play").classList.remove("on"); }
  async function playStep() {
    if (+$("wb-hour").value >= rdpsHours.length - 1) $("wb-hour").value = -1;
    step(1);
    await framesFor(WM.run, curHour()).catch(() => {});
    if (playing) playing = setTimeout(playStep, 900);
  }
  $("wb-play").onclick = () => {
    if (playing) return stopPlay();
    $("wb-play").textContent = "❚❚"; $("wb-play").classList.add("on");
    playing = setTimeout(playStep, 0);
  };
  $("wb-prev").onclick = () => { stopPlay(); step(-1); };
  $("wb-next").onclick = () => { stopPlay(); step(1); };
  $("wb-hour").oninput = () => { stopPlay(); showRdps(); };
  $("wb-close").onclick = () => { $("wind").value = "off"; $("precip").value = "off"; $("smoke").value = "off"; save(); showRdps(); };

  // ---- smoke: ECCC FireWork (GeoMet WMS) and BlueSky Canada (hourly PNGs made from its netCDF; see smoke.py) ----
  // Both follow the hour bar; the hour nearest now when the bar has no RDPS hours yet.
  map.createPane("smoke");
  map.getPane("smoke").style.zIndex = 350;      // over the danger maps, under precipitation, labels and fires
  map.getPane("smoke").style.pointerEvents = "none";
  const smokeOn = () => $("smoke").value !== "off";
  let SMOKE = null, smokeWms = null, smokeImg = null, smokeTimer = null;
  // EER plume: filled contour bands of near-ground PM2.5, one canvas layer redrawn per hour
  const smokeCanvas = L.canvas({ pane: "smoke", padding: 0.2 });
  const plumeLayer = L.layerGroup();
  const PLUME_COL = { 5: "#cfe0ee", 10: "#a9c9e2", 25: "#d2c38a", 35: "#d9a54a", 50: "#d9822f", 75: "#d0582a",
    100: "#b52d2a", 200: "#8a1f4a", 300: "#5e1550", 500: "#3a0a3a" };
  const plumeCache = new Map();
  const PLUME_ATTR = 'Smoke plume: <a href="https://eer.cmc.ec.gc.ca/mandats/AutoSim/Fire/" target="_blank" rel="noopener">ECCC EER</a>';
  let plumeShown = null;
  function plumeFrame(run, i) {
    const k = `${run}/${i}`;
    if (!plumeCache.has(k)) {
      plumeCache.set(k, fetch(`/api/smoke/eer/${run}/${i}.json`).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .catch((e) => { plumeCache.delete(k); throw e; }));
      while (plumeCache.size > 12) plumeCache.delete(plumeCache.keys().next().value);
    }
    return plumeCache.get(k);
  }
  async function drawPlume(run, i) {
    const k = `${run}/${i}`;
    if (plumeShown === k && map.hasLayer(plumeLayer)) return;
    let levels;
    try { levels = await plumeFrame(run, i); } catch (_) { return; }
    if ($("smoke").value !== "eer") return;
    plumeShown = k;
    plumeLayer.clearLayers();
    for (const L0 of levels) {  // lowest level first; each band's rings filled even-odd, so holes stay open
      const col = PLUME_COL[L0.level] || "#5e1550";
      L.polygon(L0.rings.map((r) => r.map(([x, y]) => [y, x])), { renderer: smokeCanvas, interactive: false,
        stroke: true, weight: 0.6, color: col, opacity: 0.9, fillColor: col, fillOpacity: L0.level >= 25 ? 0.55 : 0.35 }).addTo(plumeLayer);
    }
    if (!map.hasLayer(plumeLayer)) { plumeLayer.addTo(map); map.attributionControl.addAttribution(PLUME_ATTR); }
  }
  async function loadSmoke() {
    try { SMOKE = await (await fetch("/api/smoke", { cache: "no-store" })).json(); } catch (_) { SMOKE = { failed: true }; }
    showSmoke();
  }
  const smokeWant = () => (rdpsHours.length && curHour() ? curHour().t : new Date(Math.round(Date.now() / 36e5) * 36e5));
  function clearSmoke(which) {
    if (which !== "firework" && smokeWms) { map.removeLayer(smokeWms); smokeWms = null; }
    if (which !== "bluesky" && smokeImg) { map.removeLayer(smokeImg); smokeImg = null; }
    if (which !== "eer" && map.hasLayer(plumeLayer)) {
      map.removeLayer(plumeLayer); plumeLayer.clearLayers(); plumeShown = null; map.attributionControl.removeAttribution(PLUME_ATTR);
    }
  }
  const nearestIdx = (times, want) => {
    const ts = times.map((t) => Date.parse(t.replace("Z", ":00Z")));
    let i = 0;
    ts.forEach((t, k) => { if (Math.abs(t - want) < Math.abs(ts[i] - want)) i = k; });
    return [i, ts[i], Math.abs(ts[i] - want) > 90 * 60e3];
  };
  const pmKey = (bins) => `<div class="pmkey">${bins.map(([v, c, a]) => `<span><i style="background:${c};opacity:${Math.max(a, 0.55)}"></i>${v}</span>`).join("")}<em>µg/m³</em></div>`;
  function showSmoke() {
    const v = $("smoke").value, key = $("wb-sk");
    if (v === "off") { clearSmoke(); key.hidden = true; return; }
    key.hidden = false;
    clearSmoke(v);  // drop the other source's layer
    if (!SMOKE) { key.textContent = "Smoke forecast: loading…"; return; }
    const want = smokeWant();
    if (v === "firework") {
      const F = SMOKE.firework;
      if (!F) { clearSmoke(); key.innerHTML = "<b>FireWork</b> smoke forecast is unavailable from ECCC GeoMet right now."; return; }
      const t = Math.min(Math.max(want.getTime(), Date.parse(F.start)), Date.parse(F.end));
      const time = isoHour(new Date(t)), off = Math.abs(t - want.getTime()) >= 36e5;
      if (!smokeWms || smokeWms.options.layers !== F.layer) {
        if (smokeWms) map.removeLayer(smokeWms);
        smokeWms = L.tileLayer.wms(SMOKE.wms, { layers: F.layer, styles: F.style || "", format: "image/png", transparent: true, version: "1.3.0", time,
          opacity: 0.75, pane: "smoke", attribution: "FireWork smoke: Data Source: Environment and Climate Change Canada" }).addTo(map);
      } else if (smokeWms.wmsParams.time !== time) smokeWms.setParams({ time });
      const run = F.run ? `${F.run.slice(11, 13)}Z ${F.run.slice(8, 10)}/${F.run.slice(5, 7)}` : "";
      key.innerHTML = `<div><b>Smoke · FireWork (ECCC RAQDPS)</b> ${esc(F.title)} · ${esc(fmtT(time))}${run ? ` · run ${run}` : ""}</div>`
        + (off ? `<div class="warn">FireWork doesn't cover the hour on the bar; showing its nearest hour.</div>` : "")
        + (F.stale ? `<div class="warn">ECCC hasn't updated FireWork since the ${run} run.</div>` : "")
        + `<img alt="" onerror="this.remove()" src="${SMOKE.wms}?service=WMS&version=1.3.0&request=GetLegendGraphic&sld_version=1.1.0&format=image/png&layer=${encodeURIComponent(F.layer)}${F.style ? `&STYLE=${encodeURIComponent(F.style)}` : ""}">`;
      return;
    }
    if (v === "eer") {
      const E = SMOKE.eer || {};
      if (!E.times?.length) {
        clearSmoke();
        key.innerHTML = `<b>Smoke · ECCC EER plume model</b> ${esc(E.error || (SMOKE.failed ? "Couldn't reach FWX for the smoke forecast." : "No plume forecast yet."))}`;
        return;
      }
      const [i, t, off] = nearestIdx(E.times, want);
      drawPlume(E.run, i);
      if (i + 1 < E.times.length) plumeFrame(E.run, i + 1).catch(() => {});  // the next hour, ready for play
      const lv = (E.levels || Object.keys(PLUME_COL).map(Number));
      key.innerHTML = `<div><b>Smoke · ECCC EER plume model</b> near-ground PM2.5 (MLDP, experimental) · ${esc(fmtT(new Date(t).toISOString()))}</div>`
        + (E.ended ? `<div class="warn">The latest plume run has ended; no newer run has been published.</div>`
          : off ? `<div class="warn">The plume run doesn't cover the hour on the bar; showing its nearest hour.</div>` : "")
        + (E.stale && !E.ended ? `<div class="warn">This plume run is more than 18 h old.</div>` : "")
        + pmKey(lv.map((l) => [l, PLUME_COL[l], l >= 25 ? 0.75 : 0.55]))
        + `<div class="fine">For guidance, planning and prevention only (ECCC). Bands are 1-hour mean PM2.5 from the surface to 250 m.</div>`;
      return;
    }
    const B = SMOKE.bluesky || {};
    if (!B.times?.length) {
      clearSmoke();
      key.innerHTML = `<b>Smoke · BlueSky Canada</b> ${esc(B.error || (SMOKE.failed ? "Couldn't reach FWX for the smoke forecast." : "No BlueSky forecast yet."))}`;
      return;
    }
    const ts = B.times.map((t) => Date.parse(t.replace("Z", ":00Z")));
    let i = 0;
    ts.forEach((t, k) => { if (Math.abs(t - want) < Math.abs(ts[i] - want)) i = k; });
    const off = Math.abs(ts[i] - want) > 90 * 60e3;
    const url = `/api/smoke/bluesky/${B.run}/${i}.png`, bb = B.bounds;
    const bounds = [[bb.south, bb.west], [bb.north, bb.east]];
    if (!smokeImg) smokeImg = L.imageOverlay(url, bounds, { pane: "smoke", opacity: 0.85,
      attribution: 'Smoke: © <a href="https://firesmoke.ca" target="_blank" rel="noopener">BlueSky Canada</a>' }).addTo(map);
    else { smokeImg.setUrl(url); smokeImg.setBounds(L.latLngBounds(bounds)); }
    if (i + 1 < ts.length) new Image().src = `/api/smoke/bluesky/${B.run}/${i + 1}.png`;  // the next hour, ready for play
    key.innerHTML = `<div><b>Smoke · BlueSky Canada</b> surface PM2.5 · ${esc(fmtT(new Date(ts[i]).toISOString()))}</div>`
      + (B.ended ? `<div class="warn">This is the last BlueSky run; its forecast has ended. BlueSky runs April to September.</div>`
        : off ? `<div class="warn">BlueSky doesn't cover the hour on the bar; showing its nearest hour.</div>` : "")
      + (B.stale && !B.ended ? `<div class="warn">This BlueSky run is more than a day old.</div>` : "")
      + pmKey(SMOKE.bins || []);
  }
  $("smoke").value = ["firework", "bluesky", "eer"].includes(prefs.smoke) ? prefs.smoke : "off";
  $("smoke").onchange = () => {
    save();
    if (smokeOn() && !SMOKE) loadSmoke();
    showRdps();
    if (phone.matches) closeSettings();
  };
  clearInterval(smokeTimer);
  smokeTimer = setInterval(() => { if (smokeOn()) loadSmoke(); }, 15 * 60e3);
  if (smokeOn()) loadSmoke();
  // ---- fires: FIRMS satellite hotspots by age (Hotspots), and the CWFIS agency-reported fires (Reported fires) ----
  map.createPane("fires");
  map.getPane("fires").style.zIndex = 450;
  const fireCanvas = L.canvas({ pane: "fires", padding: 0.3 });
  const hotLayer = L.layerGroup(), repLayer = L.layerGroup();
  const FIRE_AGE = [[6, "#ff2d2d"], [24, "#ff9800"], [48, "#ffd54f"]];
  const fireColor = (hrs) => (FIRE_AGE.find(([h]) => hrs < h) || FIRE_AGE[2])[1];
  const ago = (t) => { const m = (Date.now() - new Date(t)) / 60e3; return m < 90 ? `${Math.round(m)} min ago` : `${Math.round(m / 60)} h ago`; };
  const fireT = (t) => new Date(t).toLocaleString("en-CA", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  const escF = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fireNote = { hot: "", rep: "" };
  const showFireNote = () => status([fireNote.hot, fireNote.rep].filter(Boolean).join(" · "));

  // hotspots: fetched for the view (padded); panning inside what was fetched needs no new request
  let hotReq = 0, hotBox = null, hotHrs = null;
  async function loadHotspots(force) {
    const hrs = $("fires").value;
    if (hrs === "off") { map.removeLayer(hotLayer); hotLayer.clearLayers(); hotBox = null; fireNote.hot = ""; showFireNote(); return; }
    if (!map.hasLayer(hotLayer)) hotLayer.addTo(map);
    if (!force && hotBox && hotHrs === hrs && hotBox.contains(map.getBounds())) return;
    const b = map.getBounds().pad(0.5), my = ++hotReq;
    const box = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(3)).join(",");
    let j;
    try { j = await (await fetch(`/api/fires?hours=${hrs}&bbox=${box}&reported=false`)).json(); }
    catch (_) { fireNote.hot = "Hotspots unavailable right now"; showFireNote(); return; }
    if (my !== hotReq || $("fires").value === "off") return;
    hotBox = b; hotHrs = hrs;
    hotLayer.clearLayers();
    const now = Date.now();
    for (const f of j.features) {
      const [lon, lat] = f.geometry.coordinates, p = f.properties;
      L.circleMarker([lat, lon], { renderer: fireCanvas, radius: 4, weight: 1, color: "#3a0000", fillColor: fireColor((now - new Date(p.time)) / 36e5), fillOpacity: 0.95 })
        .bindPopup(`<b>Satellite hotspot</b><br>${escF(fireT(p.time))} (${ago(p.time)})<br>${escF(p.sat)}${p.conf ? ` · ${escF(p.conf)} confidence` : ""}${p.frp != null ? `<br>Fire radiative power ${p.frp} MW` : ""}${p.day ? "" : " · night pass"}<br><span class="sub">A heat signal, not a confirmed fire. Source: NASA FIRMS.</span>`)
        .addTo(hotLayer);
    }
    const st = j.status?.firms || {};
    fireNote.hot = !st.enabled ? "Hotspots: FIRMS key not set on the server" : st.error ? `Hotspots: ${st.error}`
      : `Hotspots: ${j.features.length} in view (red <6 h, orange 6–24 h, yellow 24–48 h)`;
    showFireNote();
  }

  // reported fires: the agencies' current list, all of Canada (a few hundred at most), not time-limited
  let repReq = 0;
  async function loadReported() {
    if ($("repfires").value === "off") { map.removeLayer(repLayer); repLayer.clearLayers(); fireNote.rep = ""; showFireNote(); return; }
    if (!map.hasLayer(repLayer)) repLayer.addTo(map);
    const my = ++repReq;
    let j;
    try { j = await (await fetch("/api/fires?hotspots=false")).json(); }
    catch (_) { fireNote.rep = "Reported fires unavailable right now"; showFireNote(); return; }
    if (my !== repReq || $("repfires").value === "off") return;
    repLayer.clearLayers();
    for (const f of j.features) {
      const [lon, lat] = f.geometry.coordinates, p = f.properties;
      L.circleMarker([lat, lon], { renderer: fireCanvas, radius: 7, weight: 3, color: "#ffffff", fillColor: "#b71c1c", fillOpacity: 1 })
        .bindPopup(`<b>${escF(p.name)}</b>${p.agency ? ` (${escF(p.agency)})` : ""}<br>${p.stage ? escF(p.stage) : "Stage not given"}${p.ha != null ? ` · ${p.ha} ha` : ""}${p.start ? `<br>Started ${escF(String(p.start).slice(0, 10))}` : ""}<br><span class="sub">Reported by the fire agency. Source: CWFIS.</span>`)
        .addTo(repLayer);
    }
    const st = j.status?.reported || {};
    fireNote.rep = st.error && !j.features.length ? "Reported fires: CWFIS isn't answering" : `Reported fires: ${j.features.length} in Canada (ringed)`;
    showFireNote();
  }

  let hotMove = null;
  map.on("moveend", () => { if ($("fires").value !== "off") { clearTimeout(hotMove); hotMove = setTimeout(() => loadHotspots(false), 400); } });
  $("fires").value = ["24", "48"].includes(prefs.fires) ? prefs.fires : "off";
  $("repfires").value = prefs.repfires === "on" ? "on" : "off";
  $("fires").onchange = () => { save(); loadHotspots(true); if (window.matchMedia("(max-width: 700px)").matches) closeSettings(); };
  $("repfires").onchange = () => { save(); loadReported(); if (window.matchMedia("(max-width: 700px)").matches) closeSettings(); };
  setInterval(() => { if ($("fires").value !== "off") loadHotspots(true); }, 15 * 60e3);
  setInterval(() => { if ($("repfires").value !== "off") loadReported(); }, 30 * 60e3);
  loadHotspots(true); loadReported();

  $("wind").value = ["particles", "arrows"].includes(prefs.wind) ? prefs.wind : "off";
  $("precip").value = ["on", "snow"].includes(prefs.precip) ? prefs.precip : "off";
  $("basemap").onchange = () => {
    setBasemap(); save();
    const id = $("aoi").value;
    for (const [k, g] of Object.entries(shapes)) g.eachLayer((x) => {
      if (x instanceof L.Rectangle) x.setStyle(styleFor(k === id));
      else x.setStyle({ ...dotStyle(), fillColor: darkBase() ? "#1d2329" : "#fff" });
    });
    wind?.setLight(!isSat($("layer").value) && !darkBase());
    if (phone.matches) closeSettings();
  };
  $("wind").onchange = $("precip").onchange = () => { save(); showRdps(); if (phone.matches) closeSettings(); };

  // ---- ☰ Layers: the map layers panel. Wide screens: open by default and remembered; phones: closes after a pick ----
  const phone = window.matchMedia("(max-width: 700px)");
  const remember = () => {};
  function setSettings(open) {
    if (open) { alertsOpen = false; $("alerts").hidden = true; $("notify").hidden = true; }
    document.body.classList.toggle("settings-open", open);
    $("gear").classList.toggle("on", open);
    $("gear").setAttribute("aria-expanded", String(open));
  }
  function closeSettings() { setSettings(false); }
  $("gear").onclick = () => { const open = !document.body.classList.contains("settings-open"); setSettings(open); remember(open); };
  $("lp-close").onclick = () => { closeSettings(); remember(false); };
  map.on("click", () => { if (phone.matches) closeSettings(); });
  $("country2").onclick = () => { closeSettings(); $("country").click(); };
  // the Layers panel starts closed so the map and the forecast panel have the room; ☰ Layers opens it
  $("legend").onclick = () => $("legend").classList.toggle("collapsed");
  if (phone.matches) $("legend").classList.add("collapsed");
  // the header's height changes with the window; keep the map filling the rest
  window.addEventListener("resize", () => map.invalidateSize());

  // open on the user's area (their base, or the last one they looked at), close in; all of Canada if none yet
  // with its forecast panel open (opened first, so the zoom keeps the area clear of it)
  if ($("aoi").value) { openPanel(); showArea(true, true); } else { showCountry(); showArea(false); }
  showLayer();
  updatePdfLink();
  loadAlerts();
  loadRdpsMeta();
  loadGoes();
  loadSnow();
  if (linked) { openPanel(); showArea(true); }
})();
