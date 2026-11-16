"""Fire weather forecast PDF for one area, from CWFIS daily values (letter, landscape)."""
import io
from datetime import date, datetime

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# CWFIS fire danger classes: strong colour for the danger cell and bars, a light tint for the row
DANGER = {
    "Low": ("#2f6fd6", "#e3edfb"),
    "Moderate": ("#3c9a3f", "#e4f3e4"),
    "High": ("#e8c400", "#fff8d6"),
    "Very High": ("#f08a00", "#ffe9cc"),
    "Extreme": ("#d32f2f", "#fbdcdc"),
}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _f(v, nd=0):
    return "—" if v is None else f"{v:.{nd}f}"


def _wind(row):
    if row.get("ws") is None:
        return "—"
    d = row.get("wdir")
    return (f"{COMPASS[int((d + 11.25) // 22.5) % 16]} " if d is not None else "") + f"{row['ws']:.0f}"


ALERT_COLOURS = {"warning": "#c62828", "watch": "#e07b00", "statement": "#5b6670"}


def build(area, rows, today=None, issued=None, alerts_in_effect=()):
    today = today or date.today()
    issued = issued or datetime.now().astimezone()
    has_box = bool(area.get("bbox"))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.45 * inch, bottomMargin=0.45 * inch,
                            title=f"Fire Weather Forecast – {area['name']}", author="CWFIS fire weather forecast")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=17, spaceAfter=2)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#444444"))
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=10.5, leading=14)
    story = [
        Paragraph(f"Fire Weather Forecast — {area['name']}", h1),
        Paragraph(f"Issued {issued:%a %b %-d %Y %H:%M %Z} · Reference point {area['lat']:.4f}, {area['lon']:.4f}"
                  + (f" · {area['prov']}" if area.get("prov") else ""), small),
        Spacer(1, 8),
    ]

    if alerts_in_effect:
        ast = ParagraphStyle("al", parent=body, fontSize=10, leading=13)
        story.append(Paragraph("<b>ECCC alerts in effect</b>", body))
        for a in alerts_in_effect:
            col = ALERT_COLOURS.get(a.get("level"), "#5b6670")
            when = ""
            if a.get("issued"):
                try:
                    when = f" — issued {datetime.fromisoformat(a['issued'].replace('Z', '+00:00')).astimezone(issued.tzinfo):%a %b %-d %H:%M}"
                except ValueError:
                    pass
            story.append(Paragraph(f'<font color="{col}"><b>{_esc(a["name"])}</b></font>{when}', ast))
        story.append(Spacer(1, 8))

    fc = [r for r in rows if date.fromisoformat(r["date"]) >= today]
    if fc:
        worst = max(fc, key=lambda r: (r.get("fdr_max", r.get("fdr")) or -1, r.get("fwi_max", r.get("fwi")) or -1))
        lab = worst.get("fdr_max_label") or worst.get("fdr_label") or "unknown"
        fwi = worst.get("fwi_max") if has_box else worst.get("fwi")
        story.append(Paragraph(
            f"<b>Highest forecast danger: {lab}</b> on {date.fromisoformat(worst['date']):%a %b %-d}"
            f" (FWI {_f(fwi)}{' in the area' if has_box else ''}). "
            f"Forecast runs to {date.fromisoformat(fc[-1]['date']):%a %b %-d}.", body))
        story.append(Spacer(1, 8))

    head = ["Date", "", "Danger", "Temp\n°C", "RH\n%", "Wind\nkm/h", "Rain\nmm", "FFMC", "DMC", "DC", "ISI", "BUI", "FWI"]
    if has_box:
        head += ["Area max\nFWI", "Area max\ndanger"]
    data = [head]
    style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9.5),
        ("ALIGN", (3, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
        ("GRID", (0, 1), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
        ("FONT", (12, 1), (12, -1), "Helvetica-Bold", 9.5),
    ]
    for i, r in enumerate(rows, start=1):
        d = date.fromisoformat(r["date"])
        line = [f"{d:%a %b %-d}", "fcst" if d >= today else "obs", r.get("fdr_label") or "—",
                _f(r.get("temp")), _f(r.get("rh")), _wind(r), _f(r.get("precip"), 1),
                _f(r.get("ffmc"), 1), _f(r.get("dmc")), _f(r.get("dc")), _f(r.get("isi"), 1), _f(r.get("bui")), _f(r.get("fwi"), 1)]
        if has_box:
            line += [_f(r.get("fwi_max"), 1), r.get("fdr_max_label") or "—"]
        data.append(line)
        if r.get("fdr_label") in DANGER:
            strong, tint = DANGER[r["fdr_label"]]
            style += [("BACKGROUND", (0, i), (-1, i), colors.HexColor(tint)),
                      ("BACKGROUND", (2, i), (2, i), colors.HexColor(strong))]
            if r["fdr_label"] in ("Low", "Moderate", "Extreme"):
                style.append(("TEXTCOLOR", (2, i), (2, i), colors.white))
        if has_box and r.get("fdr_max_label") in DANGER:
            style.append(("BACKGROUND", (-1, i), (-1, i), colors.HexColor(DANGER[r["fdr_max_label"]][0])))
            if r["fdr_max_label"] in ("Low", "Moderate", "Extreme"):
                style.append(("TEXTCOLOR", (-1, i), (-1, i), colors.white))
        if d == today:
            style.append(("LINEABOVE", (0, i), (-1, i), 1.2, colors.black))
        if d < today:
            style.append(("TEXTCOLOR", (0, i), (1, i), colors.HexColor("#666666")))
    widths = [0.95, 0.42, 0.85, 0.5, 0.45, 0.7, 0.5, 0.55, 0.5, 0.5, 0.45, 0.5, 0.5] + ([0.7, 0.85] if has_box else [])
    t = Table(data, colWidths=[w * inch for w in widths], repeatRows=1)
    t.setStyle(TableStyle(style))
    story += [t, Spacer(1, 12)]

    story.append(_fwi_chart(rows, has_box))
    story.append(Spacer(1, 8))
    notes = [
        "Source: Canadian Wildland Fire Information System (CWFIS), Natural Resources Canada — daily 2 km grids at noon "
        "local standard time. Past dates are interpolated station observations; dates from today on are CWFIS forecasts "
        "(inferred from the date, as CWFIS doesn't label them).",
        "Values are for the 2 km cell at the reference point; where that cell has no data, the nearest cell within "
        "5 km is used." + (" Area max is the highest value of any cell inside the area's bounding box." if has_box else ""),
        "Wind is the noon 10 m speed with the direction it blows from. Automated guidance: check against the official "
        "forecast and local observations before making burn or training decisions.",
    ]
    if any(r.get("cell_km") for r in rows):
        km = max(r.get("cell_km") or 0 for r in rows)
        notes.insert(2, f"The reference point's cell had no data on some days; values came from a cell up to {km:.1f} km away.")
    for n in notes:
        story.append(Paragraph(n, small))
    doc.build(story)
    return buf.getvalue()


def _fwi_chart(rows, has_box):
    w, h = 9.9 * inch, 1.9 * inch
    dr = Drawing(w, h)
    vals = [(r.get("fwi_max") if has_box else r.get("fwi")) or 0 for r in rows]
    ch = VerticalBarChart()
    ch.x, ch.y, ch.width, ch.height = 40, 22, w - 60, h - 42
    ch.data = [vals]
    ch.categoryAxis.categoryNames = [f"{date.fromisoformat(r['date']):%a %-d}" for r in rows]
    ch.categoryAxis.labels.fontSize = 8
    ch.valueAxis.valueMin = 0
    ch.valueAxis.valueMax = max(35, (max(vals) if vals else 0) * 1.15)
    ch.valueAxis.labels.fontSize = 8
    ch.barWidth = 10
    ch.bars.strokeColor = None
    for i, r in enumerate(rows):
        lab = (r.get("fdr_max_label") if has_box else None) or r.get("fdr_label")
        ch.bars[(0, i)].fillColor = colors.HexColor(DANGER[lab][0]) if lab in DANGER else colors.HexColor("#999999")
    dr.add(ch)
    dr.add(String(40, h - 12, "FWI by day" + (" (area max)" if has_box else "") + ", coloured by fire danger",
                  fontName="Helvetica-Bold", fontSize=9))
    return dr
