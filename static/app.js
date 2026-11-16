/* CWFIS fire weather forecast: area, date and layer pickers over a map of the CWFIS daily grids */
(async function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const status = (t) => { $("status").textContent = t || ""; };
  const iso = (d) => d.toISOString().slice(0, 10);
  const today = iso(new Date(Date.now() - new Date().getTimezoneOffset() * 60000));
  const prefs = (() => { try { return JSON.parse(localStorage.getItem("cwfis-prefs") || "{}"); } catch (_) { return {}; } })();
  const save = () => { try { localStorage.setItem("cwfis-prefs", JSON.stringify({ aoi: $("aoi").value, layer: $("layer").value })); } catch (_) {} };

  const map = L.map("map", { zoomControl: true }).setView([55, -97], 4);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: "© OpenStreetMap contributors · Fire weather © NRCan CWFIS",
  }).addTo(map);

  // ---- areas -----------------------------------------------------------------------------------
  const { default: defAoi, areas } = await (await fetch("/api/areas")).json();
  const byId = Object.fromEntries(areas.map((a) => [a.id, a]));
  $("aoi").innerHTML = areas.map((a) => `<option value="${a.id}">${a.name}${a.prov ? ` (${a.prov})` : ""}</option>`).join("");
  $("aoi").value = byId[prefs.aoi] ? prefs.aoi : defAoi || areas[0].id;
  // every area: its box (or a dot where it has none) with a label; click one to select it
  const areaLayer = L.layerGroup().addTo(map);
  const shapes = {};
  const styleFor = (on) => on ? { color: "#e8590c", weight: 3, fill: true, fillColor: "#e8590c", fillOpacity: 0.12 }
                              : { color: "#111", weight: 1.5, fill: true, fillColor: "#111", fillOpacity: 0.06 };
  for (const a of areas) {
    const g = L.featureGroup().addTo(areaLayer);
    if (a.bbox) {
      const [w, s, e, n] = a.bbox;
      L.rectangle([[s, w], [n, e]], styleFor(false)).addTo(g);
    }
    // the dot keeps small areas visible at country scale
    L.circleMarker([a.lat, a.lon], { radius: 4, color: "#111", weight: 1.5, fillColor: "#fff", fillOpacity: 1 }).addTo(g)
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
    const order = [sel, ...areas.map((a) => a.id).filter((id) => id !== sel)];
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
  function showArea(fly) {
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
    if (fly) {
      if (a.bbox) map.fitBounds(L.latLngBounds([[a.bbox[1], a.bbox[0]], [a.bbox[3], a.bbox[2]]]).pad(1.5), { maxZoom: 10 });
      else map.setView([a.lat, a.lon], 9);
    }
  }

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
  $("layer").innerHTML = info.layers.map((l) => `<option value="${l.id}">${l.title}</option>`).join("");
  $("layer").value = layerById[prefs.layer] ? prefs.layer : info.layers.some((l) => l.id === "fwi") ? "fwi" : info.layers[0]?.id;

  function setDateRange() {
    const l = layerById[$("layer").value];
    if (!l) return;
    $("date").min = l.start; $("date").max = l.end;
    if (!$("date").value) $("date").value = today <= l.end ? today : l.end;
    if ($("date").value > l.end) $("date").value = l.end;
    if ($("date").value < l.start) $("date").value = l.start;
  }

  let wms = null;
  function showLayer() {
    const l = layerById[$("layer").value];
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
    $("legend-title").textContent = l.title;
    $("legend-img").src = `${info.wms}?service=WMS&version=1.3.0&request=GetLegendGraphic&format=image/png&layer=${l.id}`;
    $("legend-date").textContent = `${time}${time >= today ? " (forecast)" : " (observed)"}`;
    $("legend").hidden = false;
    areaLayer.eachLayer((g) => g.bringToFront());
    shapes[$("aoi").value]?.bringToFront();
  }

  function updatePdfLink() {
    const d = $("date").value || today;
    const start = iso(new Date(Date.parse(d) - 86400e3));
    $("pdf").href = `/api/forecast.pdf?aoi=${encodeURIComponent($("aoi").value)}&start=${start}`;
  }

  $("aoi").onchange = () => { save(); showArea(true); updatePdfLink(); };
  $("country").onclick = showCountry;
  $("layer").onchange = () => { save(); showLayer(); };
  $("date").onchange = () => { showLayer(); updatePdfLink(); };
  $("pdf").onclick = () => status("Building the PDF — CWFIS takes 10–30 s for a full forecast…");
  window.addEventListener("focus", () => { if ($("status").textContent.startsWith("Building")) status(""); });

  showCountry();
  showArea(false);
  showLayer();
  updatePdfLink();
})();
