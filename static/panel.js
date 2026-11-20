/* Area panel: opens when an area is picked, with four tabs.
   Forecast     – Open-Meteo (ECCC GEM) hourly strip and 7-day outlook
   Fire weather – stacked hourly meteograms with one shared crosshair and readout
   FWI 3 days   – CWFIS daily FWI System values for today, tomorrow and the next day
   Burn windows – each forecast hour checked against prescribed burn limits: within / near / outside limits
   Listens for the "area-selected" event from app.js. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const r0 = (v) => (v == null ? "–" : Math.round(v));
  const r1 = (v) => (v == null ? "–" : (Math.round(v * 10) / 10).toFixed(1));
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const dayOf = (iso) => new Date(`${iso.slice(0, 10)}T12:00`);
  const dayName = (iso) => DOW[dayOf(iso).getDay()];
  const dayLong = (iso) => { const d = dayOf(iso); return `${DOW[d.getDay()]} ${MON[d.getMonth()]} ${d.getDate()}`; };
  const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const compass = (d) => (d == null ? "" : COMPASS[Math.round(d / 22.5) % 16]);
  // arrow points the way the wind blows TO (direction FROM + 180)
  const arrow = (d) => (d == null ? "" : `<span class="warr" style="transform:rotate(${(d + 180) % 360}deg)">↑</span>`);

  // WMO weather codes (Open-Meteo) -> symbol + words
  const WMO = {
    0: ["☀️", "Clear"], 1: ["🌤️", "Mainly clear"], 2: ["⛅", "Partly cloudy"], 3: ["☁️", "Overcast"],
    45: ["🌫️", "Fog"], 48: ["🌫️", "Freezing fog"], 51: ["🌦️", "Light drizzle"], 53: ["🌦️", "Drizzle"], 55: ["🌧️", "Heavy drizzle"],
    56: ["🌧️", "Freezing drizzle"], 57: ["🌧️", "Freezing drizzle"], 61: ["🌦️", "Light rain"], 63: ["🌧️", "Rain"], 65: ["🌧️", "Heavy rain"],
    66: ["🌧️", "Freezing rain"], 67: ["🌧️", "Freezing rain"], 71: ["🌨️", "Light snow"], 73: ["🌨️", "Snow"], 75: ["❄️", "Heavy snow"],
    77: ["🌨️", "Snow grains"], 80: ["🌦️", "Showers"], 81: ["🌧️", "Showers"], 82: ["⛈️", "Heavy showers"], 85: ["🌨️", "Snow showers"],
    86: ["❄️", "Snow showers"], 95: ["⛈️", "Thunderstorm"], 96: ["⛈️", "Thunderstorm, hail"], 99: ["⛈️", "Thunderstorm, hail"],
  };
  const wmo = (c) => WMO[c] || ["·", ""];

  // CWFIS danger classes, and the FWI bands of the CWFIS map legend
  const DANGER = { Low: "#2f6fd6", Moderate: "#3c9a3f", High: "#e8c400", "Very High": "#f08a00", Extreme: "#d32f2f" };
  const DARK_TEXT = new Set(["High", "Very High"]);
  const FWI_BANDS = [[0, 5, "rgba(47,111,214,.14)"], [5, 15, "rgba(255,236,94,.30)"], [15, 22, "rgba(255,196,94,.32)"],
                     [22, 29, "rgba(240,138,0,.24)"], [29, 999, "rgba(211,47,47,.20)"]];
  const SERIES = "#2a78d6", SERIES2 = "#eb6834", INK2 = "#52514e", GRID = "rgba(0,0,0,.07)";

  let DATA = null, areaId = null, tab = "fc", range = 72, charts = [], cursor = null;
  let rxRange = 3, rxPick = null, rxEdit = false;
  try {
    tab = localStorage.getItem("fwx-tab") || "fc"; range = +localStorage.getItem("fwx-range") || 72;
    rxRange = +localStorage.getItem("fwx-rx-range") || 3;
  } catch (_) {}

  // ---- the sheet -------------------------------------------------------------------------------------
  const sheet = $("sheet");
  // the map keeps the selected area clear of the panel: below it on phones, beside it on wider screens
  function sheetPadding() {
    if (!document.body.classList.contains("sheet-open")) return { left: 0, bottom: 0 };
    const r = sheet.getBoundingClientRect();
    return window.matchMedia("(max-width: 700px)").matches ? { left: 0, bottom: r.height } : { left: r.width + 12, bottom: 0 };
  }
  function setOpen(open) {
    document.body.classList.toggle("sheet-open", open);
    sheet.hidden = !open;
    document.documentElement.style.setProperty("--sheet-h", `${open ? sheet.getBoundingClientRect().height : 0}px`);
  }
  function setMin(min) {
    sheet.classList.toggle("min", min);
    document.documentElement.style.setProperty("--sheet-h", `${sheet.getBoundingClientRect().height}px`);
    if (!min) render();
  }
  window.fwxSheet = { padding: sheetPadding };

  $("sh-close").onclick = (e) => { e.stopPropagation(); setOpen(false); };
  $("sh-head").onclick = () => setMin(!sheet.classList.contains("min"));
  sheet.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => {
    tab = b.dataset.tab;
    try { localStorage.setItem("fwx-tab", tab); } catch (_) {}
    if (sheet.classList.contains("min")) setMin(false); else render();
  }));

  // ---- where the numbers come from and how fresh they are, under the tabs ----------------------------
  const STALE_MIN = 90;  // older than this and the panel says so in red
  function tzAbbr(tz) {
    try { return new Intl.DateTimeFormat("en-CA", { timeZone: tz, timeZoneName: "short" }).formatToParts(new Date()).find((x) => x.type === "timeZoneName").value; }
    catch (_) { return tz; }
  }
  function provenance() {
    const el = $("sh-prov");
    if (!DATA) { el.textContent = ""; el.className = "prov"; return; }
    const tz = DATA.weather?.timezone, fetched = new Date(DATA.fetched);
    const ageMin = (Date.now() - fetched) / 60e3;
    const at = fetched.toLocaleTimeString("en-CA", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz || undefined });
    const problems = Object.values(DATA.errors || {});
    if (DATA.refreshFailed) problems.push("refresh failed; showing the last good data");
    const stale = ageMin > STALE_MIN || problems.length > 0;
    const parts = [`Forecast fetched ${at}${tz ? ` ${tzAbbr(tz)}` : ""}${ageMin > 60 ? ` (${Math.round(ageMin / 60)} h ago)` : ""}`];
    if (DATA.cwfis_last) parts.push(`CWFIS daily to ${dayLong(DATA.cwfis_last)}`);
    if (tz) parts.push(`times are ${tzAbbr(tz)}`);
    el.className = "prov" + (stale ? " stale" : "");
    el.innerHTML = (stale ? `<b>⚠ ${ageMin > STALE_MIN ? "Out of date" : "Incomplete"}</b> · ` : "") + esc(parts.join(" · "))
      + (problems.length ? ` · ${esc(problems.join("; "))}` : "");
  }
  async function load(id, quiet) {
    try {
      const r = await fetch(`/api/area/${encodeURIComponent(id)}`, { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      if (areaId !== id) return;  // a newer pick arrived while this one loaded
      DATA = d;
    } catch (err) {
      if (areaId !== id) return;
      if (quiet && DATA) { DATA.refreshFailed = true; provenance(); return; }
      $("sh-body").innerHTML = `<p class="sub pad">Couldn't load the forecast. Try again in a minute.</p>`;
      return;
    }
    const y = $("sh-body").scrollTop;
    render();
    if (quiet) $("sh-body").scrollTop = y;
  }
  // keep an open panel current: refetch every 30 min, and the age line ticks over every minute
  setInterval(() => { if (!sheet.hidden && areaId) load(areaId, true); }, 30 * 60e3);
  setInterval(provenance, 60e3);

  document.addEventListener("area-selected", async (e) => {
    const { id, name } = e.detail;
    areaId = id;
    rxPick = null;
    $("sh-title").textContent = name;
    $("sh-sub").textContent = "";
    sheet.classList.remove("min");
    setOpen(true);
    DATA = null;
    provenance();
    $("sh-body").innerHTML = `<p class="sub pad">Loading forecast…</p>`;
    await load(id, false);
  });

  function render() {
    sheet.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    charts.forEach((c) => c.destroy());
    charts = [];
    document.documentElement.style.setProperty("--sheet-h", `${sheet.getBoundingClientRect().height}px`);
    provenance();
    if (!DATA) return;
    const w = DATA.weather;
    if (w?.daily?.length) {
      const t = w.daily.find((x) => x.date === DATA.today);
      $("sh-sub").textContent = t ? `${wmo(t.code)[0]} ${r0(t.tmax)}° / ${r0(t.tmin)}°` : "";
    }
    const body = $("sh-body");
    if (tab === "fc") body.innerHTML = forecastTab();
    else if (tab === "fw") { body.innerHTML = fireTab(); drawCharts(); }
    else if (tab === "rx") { body.innerHTML = rxTab(); wireRx(); }
    else body.innerHTML = fwiTab();
    body.querySelectorAll("[data-rxrange]").forEach((b) => (b.onclick = () => {
      rxRange = +b.dataset.rxrange;
      try { localStorage.setItem("fwx-rx-range", rxRange); } catch (_) {}
      render();
    }));
    body.querySelectorAll("[data-range]").forEach((b) => (b.onclick = () => {
      range = +b.dataset.range;
      try { localStorage.setItem("fwx-range", range); } catch (_) {}
      render();
    }));
  }

  const err = (k) => (DATA.errors?.[k] ? `<p class="sub pad">${esc(DATA.errors[k])}.</p>` : "");

  // ---- tab 1: forecast -------------------------------------------------------------------------------
  function forecastTab() {
    const w = DATA.weather;
    if (!w) return err("weather");
    const h = w.hourly;
    const i0 = Math.max(0, h.time.findIndex((t) => t >= w.now));
    const cells = [];
    for (let i = i0; i < Math.min(i0 + 24, h.time.length); i++) {
      const hr = h.time[i].slice(11, 13);
      cells.push(`<div class="hr${hr === "00" ? " midnight" : ""}">
        <div class="t">${hr === "00" ? dayName(h.time[i]) : i === i0 ? "Now" : `${+hr}h`}</div>
        <div class="ic" title="${esc(wmo(h.code[i])[1])}">${wmo(h.code[i])[0]}</div>
        <div class="v">${r0(h.temp[i])}°</div>
        <div class="s">${arrow(h.wd[i])} ${r0(h.ws[i])}</div>
        <div class="s rain">${h.precip[i] > 0 ? `${r1(h.precip[i])}` : ""}</div></div>`);
    }
    const days = w.daily.filter((d) => d.date >= DATA.today);
    const rows = days.map((d) => `<tr${d.date === DATA.today ? ' class="today"' : ""}>
      <td><b>${d.date === DATA.today ? "Today" : dayName(d.date)}</b><div class="s">${MON[dayOf(d.date).getMonth()]} ${dayOf(d.date).getDate()}</div></td>
      <td class="ic" title="${esc(wmo(d.code)[1])}">${wmo(d.code)[0]}</td>
      <td><b>${r0(d.tmax)}°</b> <span class="s">${r0(d.tmin)}°</span></td>
      <td>${d.precip > 0 ? `${r1(d.precip)}<span class="s"> mm</span>` : `<span class="s">–</span>`}</td>
      <td>${arrow(d.wd)} ${r0(d.ws_max)}${d.gust_max != null ? `<span class="s">, gusts ${r0(d.gust_max)}</span>` : ""}</td>
      <td>${r0(d.rh_min)}<span class="s">%</span></td>
      <td>${d.fwi_peak != null ? `<b>${r0(d.fwi_peak)}</b>` : "–"}</td></tr>`).join("");
    return `<div class="units">Next 24 h · temperature °C · wind km/h · precipitation mm</div>
      <div class="hours" aria-label="Next 24 hours">${cells.join("")}</div>
      <table class="days"><thead><tr><th>Day</th><th></th><th>High / low °C</th><th title="Total for the day, water equivalent: snow counted as melted water">Precip mm</th><th>Wind km/h</th><th>Min RH</th><th title="Highest hourly FWI calculated by FWX from this forecast; not an official CWFIS value">Peak FWI <span class="calc">calc.</span></th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="note">${esc(w.model)}. Wind arrows point the way the wind blows. Precipitation is water equivalent: snow is counted as melted water (about 1 mm per cm of fresh snow).
      Peak FWI is calculated by FWX from this forecast (see Fire weather), not an official CWFIS value.</p>`;
  }

  // ---- tab 2: fire weather meteograms ---------------------------------------------------------------
  function fireTab() {
    if (!DATA.weather) return err("weather");
    return `<div class="ident calc">Calculated hourly by FWX from the ${esc(DATA.weather.model)} forecast · not an official CWFIS product</div>
      <div class="rng">${[[48, "48 h"], [72, "3 days"], [168, "7 days"]].map(([v, l]) =>
      `<button data-range="${v}" class="${range === v ? "on" : ""}">${l}</button>`).join("")}</div>
      <div class="readout" id="readout"></div>
      ${[["c-temp", "Temperature °C"], ["c-rh", "Relative humidity %"], ["c-wind", 'Wind km/h <span class="key"><i style="background:#2a78d6"></i>speed <i style="background:#eb6834"></i>gusts</span>'],
         ["c-rain", "Precipitation mm/h (water equivalent)"], ["c-ffmc", 'FFMC, hourly <span class="calc">calc.</span>'], ["c-fwi", 'FWI, hourly <span class="calc">calc.</span>']].map(([id, t]) =>
        `<div class="cbox"><div class="ct">${t}</div><div class="cw"><canvas id="${id}"></canvas></div></div>`).join("")}
      <p class="note">Hourly weather: ${esc(DATA.weather.model)}. Hourly FFMC, ISI and FWI are calculated here: they start from CWFIS's
      daily FFMC at noon${DATA.weather.fire_start ? ` (${esc(dayLong(DATA.weather.fire_start))})` : ""} and step hour by hour through this forecast,
      with CWFIS's daily BUI. A guide to the day's swing, not an official CWFIS product. FWI shading follows the CWFIS map legend.</p>`;
  }

  function drawCharts() {
    const h = DATA.weather.hourly, now = DATA.weather.now;
    const iNow = Math.max(0, h.time.findIndex((t) => t >= now));
    const a = Math.max(0, iNow - 6), b = Math.min(h.time.length, iNow + range);
    const T = h.time.slice(a, b);
    const cut = (k) => h[k].slice(a, b);
    const labels = T.map((t) => (t.slice(11, 13) === "00" ? dayName(t) : t.slice(11, 13) === "12" ? "12h" : ""));
    cursor = iNow - a;

    // shared crosshair + "now" marker
    const cross = {
      id: "cross",
      afterDatasetsDraw(c) {
        const { ctx, chartArea: ar, scales: { x } } = c;
        ctx.save();
        const xn = x.getPixelForValue(iNow - a);
        ctx.strokeStyle = "rgba(232,89,12,.7)"; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(xn, ar.top); ctx.lineTo(xn, ar.bottom); ctx.stroke();
        if (cursor != null) {
          const xc = x.getPixelForValue(cursor);
          ctx.setLineDash([]); ctx.strokeStyle = "rgba(11,11,11,.55)";
          ctx.beginPath(); ctx.moveTo(xc, ar.top); ctx.lineTo(xc, ar.bottom); ctx.stroke();
        }
        ctx.restore();
      },
    };
    const bands = {
      id: "bands",
      beforeDatasetsDraw(c) {
        if (c.canvas.id !== "c-fwi") return;
        const { ctx, chartArea: ar, scales: { y } } = c;
        ctx.save();
        for (const [lo, hi, col] of FWI_BANDS) {
          const y0 = Math.min(ar.bottom, y.getPixelForValue(lo)), y1 = Math.max(ar.top, y.getPixelForValue(Math.min(hi, y.max)));
          if (y1 < y0) { ctx.fillStyle = col; ctx.fillRect(ar.left, y1, ar.right - ar.left, y0 - y1); }
        }
        ctx.restore();
      },
    };
    const midnights = T.map((t) => t.slice(11, 13) === "00");
    const base = (last) => ({
      animation: false, responsive: true, maintainAspectRatio: false, events: ["mousemove", "mouseout", "touchstart", "touchmove", "click"],
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      layout: { padding: { right: 4 } },
      scales: {
        x: { grid: { color: (ctx) => (midnights[ctx.index] ? "rgba(0,0,0,.18)" : "transparent") }, border: { display: false },
             ticks: { display: last, autoSkip: false, maxRotation: 0, color: INK2, font: { size: 10 }, callback: (v, i) => labels[i] } },
        y: { grid: { color: GRID }, border: { display: false }, ticks: { color: INK2, font: { size: 10 }, maxTicksLimit: 4 } },
      },
      onHover: (e, _els, c) => { const i = Math.round(c.scales.x.getValueForPixel(e.x)); if (i >= 0 && i < T.length && i !== cursor) { cursor = i; refresh(); } },
    });
    const line = (data, color, extra = {}) => ({ data, borderColor: color, borderWidth: 2, pointRadius: 0, tension: 0.25, spanGaps: true, ...extra });
    const mk = (id, datasets, opts = {}, last = false) => {
      const o = base(last);
      if (opts.y) Object.assign(o.scales.y, opts.y);
      charts.push(new Chart($(id), { type: opts.type || "line", data: { labels, datasets }, options: o, plugins: [cross, bands] }));
    };
    mk("c-temp", [line(cut("temp"), SERIES)]);
    mk("c-rh", [line(cut("rh"), SERIES)], { y: { min: 0, max: 100 } });
    mk("c-wind", [line(cut("ws"), SERIES), line(cut("gust"), SERIES2)], { y: { min: 0 } });
    mk("c-rain", [{ data: cut("precip"), backgroundColor: SERIES, borderRadius: { topLeft: 2, topRight: 2 }, barPercentage: 0.8, categoryPercentage: 1 }],
       { type: "bar", y: { min: 0, suggestedMax: 1 } });
    mk("c-ffmc", [line(cut("ffmc"), SERIES)], { y: { suggestedMin: 60, max: 100 } });
    mk("c-fwi", [line(cut("fwi"), SERIES)], { y: { min: 0, suggestedMax: 20 } }, true);

    function refresh() {
      charts.forEach((c) => c.draw());
      const i = a + cursor;
      const t = h.time[i];
      const v = (k, f = r0) => f(h[k][i]);
      $("readout").innerHTML = `<b>${dayName(t)} ${t.slice(11, 16)}</b>
        <span>${wmo(h.code[i])[0]} ${v("temp")}°C</span><span>RH ${v("rh")}%</span>
        <span>${arrow(h.wd[i])} ${compass(h.wd[i])} ${v("ws")} km/h${h.gust[i] != null ? `, gusts ${v("gust")}` : ""}</span>
        <span>${h.precip[i] > 0 ? `precip ${v("precip", r1)} mm` : "no precip"}</span>
        <span>FFMC ${v("ffmc", r1)}</span><span>ISI ${v("isi", r1)}</span><span><b>FWI ${v("fwi", r1)}</b></span>`;
    }
    refresh();
  }

  // ---- tab 3: CWFIS FWI for today, tomorrow and the next day ------------------------------------------
  function fwiTab() {
    if (!DATA.fwi_days?.length) return err("cwfis") || `<p class="sub pad">CWFIS has no values for these days yet.</p>`;
    const peak = Object.fromEntries((DATA.weather?.daily || []).map((d) => [d.date, d.fwi_peak]));
    const hasBox = !!DATA.area.bbox;
    const cards = DATA.fwi_days.map((r, k) => {
      const lab = r.fdr_label || "–";
      const col = DANGER[lab] || "#8a959f";
      return `<div class="fwic">
        <div class="d">${k === 0 ? "Today" : k === 1 ? "Tomorrow" : dayName(r.date)}<span class="s"> ${esc(dayLong(r.date).slice(4))}</span></div>
        <div class="badge" style="background:${col};color:${DARK_TEXT.has(lab) ? "#1d2329" : "#fff"}">${esc(lab)}</div>
        <div class="big">${r1(r.fwi)}<span>FWI</span></div>
        ${hasBox && r.fwi_max != null ? `<div class="s">Area max ${r1(r.fwi_max)}${r.fdr_max_label && r.fdr_max_label !== lab ? ` · ${esc(r.fdr_max_label)}` : ""}</div>` : ""}
        ${peak[r.date] != null ? `<div class="s" title="Highest hourly FWI calculated by FWX from the forecast; not a CWFIS value">Hourly peak ≈ ${r0(peak[r.date])} <span class="calc">calc.</span></div>` : ""}
        <dl><dt>ISI</dt><dd>${r1(r.isi)}</dd><dt>BUI</dt><dd>${r0(r.bui)}</dd><dt>FFMC</dt><dd>${r1(r.ffmc)}</dd>
            <dt>DMC</dt><dd>${r0(r.dmc)}</dd><dt>DC</dt><dd>${r0(r.dc)}</dd></dl>
        <div class="wx">${r0(r.temp)}°C · RH ${r0(r.rh)}%<br>${compass(r.wdir)} ${r0(r.ws)} km/h${r.precip ? ` · precip ${r1(r.precip)} mm` : ""}</div></div>`;
    }).join("");
    return `<div class="ident official">Official CWFIS daily values · noon local standard time</div>
      <div class="fwis">${cards}</div>
      <p class="note">CWFIS daily values at noon local standard time, at the area's reference point${hasBox ? "; area max is the worst 2 km cell in the area's box" : ""}.
      The forecast PDF has the full run of days.</p>`;
  }

  // ---- tab 4: Rx — prescribed burn GO / CAUTION / NO-GO, hour by hour --------------------------------
  // Default limits follow the fire-modeling viewer's prescription (Annex A). Stability, ventilation index and
  // AQHI aren't here: they need RDPS profiles and AQ forecasts this site doesn't pull. Limits are saved per area.
  // CAUTION: within every limit, but within a parameter's margin of one of them.
  const RX_DEFAULT = {
    start: 11, end: 17,
    temp: { on: true, min: 10, max: 24 },
    rh: { on: true, min: 20, max: 45 },
    wspd: { on: true, max: 22 },
    gust: { on: false, max: 30 },
    wdir: { on: false, from: 180, to: 270 },
    rain: { on: false, max: 0 },
    ffmc: { on: true, min: 77, max: 90 },
    isi: { on: true, min: 4, max: 7 },
    fwi: { on: true, max: 12 },
    dmc: { on: true, max: 25 },
    dc: { on: true, max: 210 },
    bui: { on: true, max: 30 },
  };
  const between = (v, lo, hi) => (lo == null || v >= lo) && (hi == null || v <= hi);
  const inDir = (d, from, to) => (from <= to ? d >= from && d <= to : d >= from || d <= to);  // wraps past north
  const angDist = (a, b) => Math.abs(((a - b + 540) % 360) - 180);
  // -> "ok" | "near" | "out"; margin m on each side that has a limit
  const band = (v, lo, hi, m) => !between(v, lo, hi) ? "out" : (lo != null && v < lo + m) || (hi != null && v > hi - m) ? "near" : "ok";
  const RX_ROWS = [
    { k: "temp", label: "Temperature", unit: "°C", m: 2, get: (i, h) => h.temp[i], f: r1,
      test: (v, c, m) => band(v, c.min, c.max, m), limit: (c) => `${c.min} to ${c.max} °C` },
    { k: "rh", label: "Relative humidity", unit: "%", m: 5, get: (i, h) => h.rh[i], f: r0,
      test: (v, c, m) => band(v, c.min, c.max, m), limit: (c) => `${c.min} to ${c.max} %` },
    { k: "wspd", label: "Wind speed", unit: "km/h", m: 3, get: (i, h) => h.ws[i], f: r0,
      test: (v, c, m) => band(v, null, c.max, m), limit: (c) => `≤ ${c.max} km/h` },
    { k: "gust", label: "Wind gusts", unit: "km/h", m: 5, get: (i, h) => h.gust[i], f: r0,
      test: (v, c, m) => band(v, null, c.max, m), limit: (c) => `≤ ${c.max} km/h` },
    { k: "wdir", label: "Wind direction", unit: "°", m: 15, get: (i, h) => h.wd[i], f: (v) => `${compass(v)} ${r0(v)}°`,
      test: (v, c, m) => (!inDir(v, c.from, c.to) ? "out" : Math.min(angDist(v, c.from), angDist(v, c.to)) < m ? "near" : "ok"),
      limit: (c) => `${c.from}° to ${c.to}° (${compass(c.from)} to ${compass(c.to)})` },
    { k: "rain", label: "Precipitation in the hour", unit: "mm", m: 0, get: (i, h) => h.precip[i], f: r1,
      test: (v, c) => (v > c.max ? "out" : "ok"), limit: (c) => (c.max ? `≤ ${c.max} mm` : "none") },
    { k: "ffmc", label: "FFMC", unit: "", m: 1, hourly: "model", get: (i, h) => h.ffmc[i], f: r1,
      test: (v, c, m) => band(v, c.min, c.max, m), limit: (c) => `${c.min} to ${c.max}` },
    { k: "isi", label: "ISI", unit: "", m: 0.5, hourly: "model", get: (i, h) => h.isi[i], f: r1,
      test: (v, c, m) => band(v, c.min, c.max, m), limit: (c) => `${c.min} to ${c.max}` },
    { k: "fwi", label: "FWI", unit: "", m: 1, hourly: "model", get: (i, h) => h.fwi[i], f: r1,
      test: (v, c, m) => (v >= c.max ? "out" : v > c.max - m ? "near" : "ok"), limit: (c) => `< ${c.max}` },
    { k: "dmc", label: "DMC", unit: "", m: 2, daily: true, get: (i, h, d) => d?.dmc, f: r0,
      test: (v, c, m) => (v >= c.max ? "out" : v > c.max - m ? "near" : "ok"), limit: (c) => `< ${c.max}` },
    { k: "dc", label: "DC", unit: "", m: 15, daily: true, get: (i, h, d) => d?.dc, f: r0,
      test: (v, c, m) => (v >= c.max ? "out" : v > c.max - m ? "near" : "ok"), limit: (c) => `< ${c.max}` },
    { k: "bui", label: "BUI", unit: "", m: 3, daily: true, get: (i, h, d) => d?.bui, f: r0,
      test: (v, c, m) => (v >= c.max ? "out" : v > c.max - m ? "near" : "ok"), limit: (c) => `< ${c.max}` },
  ];
  // results are stated against the limits checked, not as a go/no-go call
  const ST = { go: ["Within limits", "#2f9e44"], caution: ["Near limits", "#f2c200"], nogo: ["Outside limits", "#d6336c"], unk: ["No data", "#c3c8cd"] };
  const RX_FIRST = 6, RX_LAST = 21;  // hours assessed each day (ignition period is inside this)

  const rxKey = () => `fwx-rx:${areaId}`;
  let RX = null, rxFor = null;
  function rx() {
    if (rxFor === areaId && RX) return RX;
    rxFor = areaId;
    try {
      const s = JSON.parse(localStorage.getItem(rxKey()) || "null");
      if (s) return (RX = Object.fromEntries(Object.entries(RX_DEFAULT).map(([k, v]) => [k, typeof v === "object" ? { ...v, ...(s[k] || {}) } : (s[k] ?? v)])));
    } catch (_) {}
    return (RX = structuredClone(RX_DEFAULT));
  }
  const saveRx = () => { try { localStorage.setItem(rxKey(), JSON.stringify(RX)); } catch (_) {} };

  /** -> {status, cells: {k: {v, r}}} for hourly index i */
  function rxHour(i) {
    const h = DATA.weather.hourly, c = rx();
    const codes = Object.fromEntries((DATA.daily_codes || []).map((d) => [d.date, d]));
    const day = codes[h.time[i].slice(0, 10)];
    const cells = {};
    for (const row of RX_ROWS) {
      if (!c[row.k].on) continue;
      const v = row.get(i, h, day);
      cells[row.k] = v == null ? { v: null, r: null } : { v, r: row.test(v, c[row.k], row.m) };
    }
    const rs = Object.values(cells).map((x) => x.r);
    const status = rs.includes("out") ? "nogo" : rs.includes(null) ? "unk" : rs.includes("near") ? "caution" : "go";
    return { status, cells };
  }
  const hh = (n) => `${String(n).padStart(2, "0")}00`;
  function windowsOf(list) {  // [{hr, status}] -> "1100–1400, 1600"
    const out = [];
    for (const e of list) {
      const last = out[out.length - 1];
      if (e.status === "go" && last && last[1] === e.hr - 1) last[1] = e.hr;
      else if (e.status === "go") out.push([e.hr, e.hr]);
    }
    return out.map(([a, b]) => (a === b ? hh(a) : `${hh(a)}–${hh(b)}`)).join(", ");
  }

  function rxTab() {
    const w = DATA.weather;
    if (!w) return err("weather");
    const h = w.hourly, c = rx(), today = w.now.slice(0, 10);
    const dates = [...new Set(h.time.map((t) => t.slice(0, 10)))].filter((d) => d >= today).slice(0, rxRange);
    const nowHr = +w.now.slice(11, 13);
    const idx = Object.fromEntries(h.time.map((t, i) => [t, i]));
    const rows = dates.map((d) => {
      const evals = [];
      const cells = [];
      for (let hr = 0; hr < 24; hr++) {
        const i = idx[`${d}T${String(hr).padStart(2, "0")}:00`];
        const assessed = i != null && hr >= RX_FIRST && hr <= RX_LAST;
        const ign = hr >= c.start && hr <= c.end;
        const past = d === today && hr < nowHr;
        let st = "off";
        if (assessed) { st = rxHour(i).status; if (ign) evals.push({ hr, status: st, i }); }
        cells.push(`<button type="button" class="rxc ${st}${ign ? " ign" : ""}${past ? " past" : ""}${rxPick === i ? " pick" : ""}"
          ${assessed ? `data-i="${i}"` : "disabled"} title="${hh(hr)}${assessed ? ` · ${ST[st][0]}` : ""}"></button>`);
      }
      const go = evals.filter((e) => e.status === "go").length, cau = evals.filter((e) => e.status === "caution").length;
      // what's stopping the ignition hours: the limits outside most often
      const fails = {};
      for (const e of evals) for (const [k, x] of Object.entries(rxHour(e.i).cells)) if (x.r === "out") fails[k] = (fails[k] || 0) + 1;
      const lim = Object.entries(fails).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k]) => RX_ROWS.find((r) => r.k === k).label);
      const verdict = go ? "go" : cau ? "caution" : evals.every((e) => e.status === "unk") ? "unk" : "nogo";
      return `<div class="rxrow">
        <div class="rxd"><b>${d === today ? "Today" : dayName(d)}</b><span class="s">${MON[dayOf(d).getMonth()]} ${dayOf(d).getDate()}</span></div>
        <div class="rxbar">${cells.join("")}</div>
        <div class="rxv"><span class="tag" style="background:${ST[verdict][1]}">${verdict === "unk" ? "?" : ST[verdict][0]}</span></div>
        <div class="rxs">${go ? `<b>${go}</b> of ${evals.length} ignition hours within limits: ${windowsOf(evals)}` : cau ? `No ignition hour fully within limits; ${cau} near limits` : verdict === "unk" ? "Not enough forecast data" : "No ignition hour within limits"}${lim.length ? ` · <span class="lim">Limiting: ${esc(lim.join(", "))}</span>` : ""}</div>
      </div>`;
    }).join("");
    const ticks = [0, 6, 12, 18].map((x) => `<span style="left:${(x / 24) * 100}%">${hh(x)}</span>`).join("");
    // the ignition period drawn as a labelled bracket over the hour columns, start to end inclusive
    const L = (c.start / 24) * 100, W = ((c.end - c.start + 1) / 24) * 100;
    const ign = `<div class="ignlab" style="left:${L + W / 2}%">Ignition ${hh(c.start)}–${hh(c.end)}</div><div class="ignband" style="left:${L}%;width:${W}%"></div>`;
    return `<div class="rng">${[[3, "3 days"], [7, "7 days"]].map(([v, l]) => `<button data-rxrange="${v}" class="${rxRange === v ? "on" : ""}">${l}</button>`).join("")}
        <span class="rxkey">${["go", "caution", "nogo", "unk"].map((k) => `<i style="background:${ST[k][1]}"></i>${ST[k][0]}`).join("")}</span></div>
      <div class="readout rxread" id="rx-read">${rxReadout()}</div>
      <div class="rxgrid"><div class="rxrow rxhead"><div></div><div class="rxticks">${ticks}</div></div>
        <div class="rxrow rxhead"><div></div><div class="rxign">${ign}</div></div>${rows}</div>
      <button type="button" class="rxedit-t" id="rx-edit-t" aria-expanded="${rxEdit}">${rxEdit ? "▾" : "▸"} Prescription limits</button>
      ${rxEdit ? rxEditor() : `<p class="rxlims"><b>Ignition ${hh(c.start)}–${hh(c.end)}</b> · ${esc(RX_ROWS.filter((r) => c[r.k].on).map((r) => `${r.label} ${r.limit(c[r.k])}`).join(" · "))}</p>`}
      <p class="note">Each hour from ${hh(RX_FIRST)} to ${hh(RX_LAST)} is checked against every enabled limit. The bracket and the outlined cells mark the ignition period.
      <b>Within limits</b>: inside every limit checked. <b>Near limits</b>: inside all, but within the margin of at least one. <b>Outside limits</b>: outside at least one. Default limits follow CFFM recommendations and can be changed.
      Grey: some value isn't forecast (hourly FFMC, ISI and FWI start from CWFIS's latest noon; DMC, DC and BUI only go as far as the CWFIS forecast).
      Weather is ${esc(w.model)}; FFMC, ISI and FWI are the hourly values from the Fire weather tab. Atmospheric stability, ventilation and air quality aren't checked here.
      Limits are saved in this browser for this area. A planning aid only: the burn boss's go/no-go call stands on observed weather.</p>`;
  }

  function rxReadout() {
    if (rxPick == null || !DATA.weather.hourly.time[rxPick]) return `<span class="sub">Tap an hour to see its values against the limits.</span>`;
    const h = DATA.weather.hourly, t = h.time[rxPick], e = rxHour(rxPick);
    const mark = { ok: "✓", near: "!", out: "✗", null: "–" };
    return `<b>${dayName(t)} ${t.slice(11, 16)} <span class="tag" style="background:${ST[e.status][1]}">${ST[e.status][0]}</span></b>` +
      RX_ROWS.filter((r) => e.cells[r.k]).map((r) => {
        const x = e.cells[r.k];
        return `<span class="rv ${x.r || "na"}" title="${esc(r.label)}: ${esc(r.limit(rx()[r.k]))}">${mark[x.r]} ${esc(r.label.replace("Relative humidity", "RH").replace(" in the hour", ""))} ${x.v == null ? "n/a" : `${r.f(x.v)}${r.unit && r.k !== "wdir" ? ` ${r.unit}` : ""}`}</span>`;
      }).join("");
  }

  function rxEditor() {
    const c = rx();
    const num = (k, f, v, step = 1) => `<input type="number" step="${step}" data-k="${k}" data-f="${f}" value="${v}">`;
    const rows = RX_ROWS.map((r) => {
      const x = c[r.k];
      let lim;
      if (r.k === "wdir") lim = `${num(r.k, "from", x.from, 5)}° to ${num(r.k, "to", x.to, 5)}°`;
      else if ("min" in x) lim = `${num(r.k, "min", x.min, r.k === "isi" ? 0.5 : 1)} to ${num(r.k, "max", x.max, r.k === "isi" ? 0.5 : 1)}`;
      else lim = `${r.k === "rain" ? "≤" : ["fwi", "dmc", "dc", "bui"].includes(r.k) ? "<" : "≤"} ${num(r.k, "max", x.max, r.k === "rain" ? 0.1 : 1)}`;
      return `<tr class="${x.on ? "" : "off"}"><td><label><input type="checkbox" data-k="${r.k}" data-f="on" ${x.on ? "checked" : ""}> ${esc(r.label)}</label></td>
        <td>${lim} <span class="s">${esc(r.unit)}</span></td><td class="s">${r.m ? `±${r.m}` : "–"}</td><td class="s">${r.daily ? "Daily (CWFIS)" : r.hourly ? "Hourly (calc.)" : "Hourly"}</td></tr>`;
    }).join("");
    return `<div class="rxed">
      <p>Ignition period ${num("start", "", c.start)} to ${num("end", "", c.end)} h
        <button type="button" id="rx-reset" class="lnk">Reset to defaults</button></p>
      <table><thead><tr><th>Parameter</th><th>Limit</th><th title="Near limits when within this much of a limit">Margin</th><th>Checked</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function wireRx() {
    const body = $("sh-body");
    body.querySelectorAll(".rxc[data-i]").forEach((b) => {
      const pick = () => { rxPick = +b.dataset.i; body.querySelectorAll(".rxc.pick").forEach((x) => x.classList.remove("pick")); b.classList.add("pick"); $("rx-read").innerHTML = rxReadout(); };
      b.onclick = pick;
      b.onmouseenter = pick;
    });
    $("rx-edit-t").onclick = () => { rxEdit = !rxEdit; render(); };
    if (!rxEdit) return;
    body.querySelectorAll(".rxed input").forEach((el) => (el.onchange = () => {
      const c = rx(), k = el.dataset.k, f = el.dataset.f;
      if (k === "start" || k === "end") c[k] = Math.min(23, Math.max(0, Math.round(+el.value)));
      else if (f === "on") c[k].on = el.checked;
      else if (el.value !== "") c[k][f] = +el.value;
      saveRx();
      const y = body.scrollTop;
      render();
      body.scrollTop = y;
    }));
    $("rx-reset").onclick = () => { RX = structuredClone(RX_DEFAULT); saveRx(); render(); };
  }
})();
