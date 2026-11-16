/* Area panel: opens when an area is picked, with three tabs.
   Forecast     – Open-Meteo (ECCC GEM) hourly strip and 7-day outlook
   Fire weather – stacked hourly meteograms with one shared crosshair and readout
   FWI 3 days   – CWFIS daily FWI System values for today, tomorrow and the next day
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
  try { tab = localStorage.getItem("fwx-tab") || "fc"; range = +localStorage.getItem("fwx-range") || 72; } catch (_) {}

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

  document.addEventListener("area-selected", async (e) => {
    const { id, name } = e.detail;
    areaId = id;
    $("sh-title").textContent = name;
    $("sh-sub").textContent = "";
    sheet.classList.remove("min");
    setOpen(true);
    DATA = null;
    $("sh-body").innerHTML = `<p class="sub pad">Loading forecast…</p>`;
    try {
      const r = await fetch(`/api/area/${encodeURIComponent(id)}`);
      const d = await r.json();
      if (areaId !== id) return;  // a newer pick arrived while this one loaded
      DATA = d;
    } catch (err) {
      if (areaId === id) $("sh-body").innerHTML = `<p class="sub pad">Couldn't load the forecast. Try again in a minute.</p>`;
      return;
    }
    render();
  });

  function render() {
    sheet.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    charts.forEach((c) => c.destroy());
    charts = [];
    document.documentElement.style.setProperty("--sheet-h", `${sheet.getBoundingClientRect().height}px`);
    if (!DATA) return;
    const w = DATA.weather;
    if (w?.daily?.length) {
      const t = w.daily.find((x) => x.date === DATA.today);
      $("sh-sub").textContent = t ? `${wmo(t.code)[0]} ${r0(t.tmax)}° / ${r0(t.tmin)}°` : "";
    }
    const body = $("sh-body");
    if (tab === "fc") body.innerHTML = forecastTab();
    else if (tab === "fw") { body.innerHTML = fireTab(); drawCharts(); }
    else body.innerHTML = fwiTab();
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
      <td>${arrow(d.wd)} ${r0(d.ws_max)}${d.gust_max != null ? `<span class="s"> g${r0(d.gust_max)}</span>` : ""}</td>
      <td>${r0(d.rh_min)}<span class="s">%</span></td>
      <td>${d.fwi_peak != null ? `<b>${r0(d.fwi_peak)}</b>` : "–"}</td></tr>`).join("");
    return `<div class="hours" aria-label="Next 24 hours">${cells.join("")}</div>
      <table class="days"><thead><tr><th>Day</th><th></th><th>High / low</th><th>Rain</th><th>Wind km/h</th><th>Min RH</th><th title="Highest hourly FWI, computed from this forecast">Peak FWI</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="note">${esc(w.model)}. Wind arrows point the way the wind blows; g = gusts. Peak FWI is the highest hourly FWI computed from this forecast (see Fire weather). Times are local to the area.</p>`;
  }

  // ---- tab 2: fire weather meteograms ---------------------------------------------------------------
  function fireTab() {
    if (!DATA.weather) return err("weather");
    return `<div class="rng">${[[48, "48 h"], [72, "3 days"], [168, "7 days"]].map(([v, l]) =>
      `<button data-range="${v}" class="${range === v ? "on" : ""}">${l}</button>`).join("")}</div>
      <div class="readout" id="readout"></div>
      ${[["c-temp", "Temperature °C"], ["c-rh", "Relative humidity %"], ["c-wind", 'Wind km/h <span class="key"><i style="background:#2a78d6"></i>speed <i style="background:#eb6834"></i>gusts</span>'],
         ["c-rain", "Rain mm/h"], ["c-ffmc", "FFMC (hourly)"], ["c-fwi", "FWI (hourly)"]].map(([id, t]) =>
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
        <span>${arrow(h.wd[i])} ${compass(h.wd[i])} ${v("ws")}${h.gust[i] != null ? ` g${v("gust")}` : ""} km/h</span>
        <span>${h.precip[i] > 0 ? `${v("precip", r1)} mm` : "no rain"}</span>
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
        ${peak[r.date] != null ? `<div class="s" title="Highest hourly FWI computed from the Open-Meteo forecast">Hourly peak ≈ ${r0(peak[r.date])}</div>` : ""}
        <dl><dt>ISI</dt><dd>${r1(r.isi)}</dd><dt>BUI</dt><dd>${r0(r.bui)}</dd><dt>FFMC</dt><dd>${r1(r.ffmc)}</dd>
            <dt>DMC</dt><dd>${r0(r.dmc)}</dd><dt>DC</dt><dd>${r0(r.dc)}</dd></dl>
        <div class="wx">${r0(r.temp)}°C · RH ${r0(r.rh)}%<br>${compass(r.wdir)} ${r0(r.ws)} km/h${r.precip ? ` · ${r1(r.precip)} mm` : ""}</div></div>`;
    }).join("");
    return `<div class="fwis">${cards}</div>
      <p class="note">CWFIS daily values at noon local standard time, at the area's reference point${hasBox ? "; area max is the worst 2 km cell in the area's box" : ""}.
      The forecast PDF has the full run of days.</p>`;
  }
})();
