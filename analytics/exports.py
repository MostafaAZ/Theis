"""PDF and Excel export stamped with filters, user and time (FR-65, UC-28)."""
import io

from django.utils import timezone


def _stamp(title, filters_text, user):
    return [title, f"Filters: {filters_text}",
            f"Exported by {user.get_full_name() or user.email} on "
            f"{timezone.localtime():%d %b %Y %H:%M} (Cairo)"]


def to_excel(title, filters_text, user, kpis, tables, analysis=None, charts=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    for i, line in enumerate(_stamp(title, filters_text, user), start=1):
        ws.cell(row=i, column=1, value=line).font = Font(bold=(i == 1), size=14 if i == 1 else 10)
    row = 5
    for label, value in kpis:
        ws.cell(row=row, column=1, value=label).font = Font(bold=True)
        ws.cell(row=row, column=2, value=str(value))
        row += 1
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 22
    if analysis:
        row += 1
        row = _excel_analysis(ws, row, analysis, charts or [])
    header_fill = PatternFill("solid", fgColor="DDE7F0")
    for n, t in enumerate(tables, start=1):
        sheet = wb.create_sheet(f"{n}. {t['title']}"[:31].replace("/", "-"))
        for i, line in enumerate(_stamp(t["title"], filters_text, user), start=1):
            sheet.cell(row=i, column=1, value=line).font = Font(bold=(i == 1))
        for c, h in enumerate(t["headers"], start=1):
            cell = sheet.cell(row=5, column=c, value=h)
            cell.font = Font(bold=True)
            cell.fill = header_fill
            sheet.column_dimensions[cell.column_letter].width = max(14, len(str(h)) + 4)
        for r, data in enumerate(t["rows"], start=6):
            for c, v in enumerate(data, start=1):
                sheet.cell(row=r, column=c, value=v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(title, filters_text, user, kpis, tables, analysis=None, charts=None):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm,
                            rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=title)
    styles = getSampleStyleSheet()
    story = []
    stamp = _stamp(title, filters_text, user)
    story.append(Paragraph(stamp[0], styles["Title"]))
    story.append(Paragraph(stamp[1], styles["Normal"]))
    story.append(Paragraph(stamp[2], styles["Normal"]))
    story.append(Spacer(1, 6 * mm))
    if kpis:
        kt = Table([[k, str(v)] for k, v in kpis], hAlign="LEFT")
        kt.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey)]))
        story += [kt, Spacer(1, 6 * mm)]
    if analysis:
        story += _pdf_analysis(analysis, styles)
    for c in charts or []:
        story += _pdf_chart(c, styles)
    cell = styles["BodyText"]
    cell.fontSize = 8
    cell.leading = 10
    for t in tables:
        story.append(Paragraph(t["title"], styles["Heading3"]))
        if not t["rows"]:
            story.append(Paragraph("No data for the selected filters.", styles["Italic"]))
            continue
        data = [[Paragraph(f"<b>{h}</b>", cell) for h in t["headers"]]]
        data += [[Paragraph(str(v), cell) for v in r] for r in t["rows"]]
        tb = Table(data, repeatRows=1, hAlign="LEFT")
        tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE7F0")),
                                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [tb, Spacer(1, 5 * mm)]
    doc.build(story)
    return buf.getvalue()


def _source(analysis):
    return ("AI analysis by Claude" if analysis.get("source") == "ai" else
            "Automated analysis")


def _excel_analysis(ws, row, analysis, charts):
    from openpyxl.styles import Alignment, Font

    def line(text, bold=False):
        nonlocal row
        cell = ws.cell(row=row, column=1, value=text)
        cell.font = Font(bold=bold)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        ws.row_dimensions[row].height = 15 * max(1, len(text) // 110 + 1)
        row += 1

    line(f"{_source(analysis)} ({analysis.get('perspective', '').upper()})", bold=True)
    line("1. What the dashboard shows", bold=True)
    line(analysis["summary"])
    for x in analysis.get("findings", []):
        line(f"• {x}")
    line("2. Impact on the company and KPIs", bold=True)
    for x in analysis.get("impacts", []):
        line(f"• {x}")
    line("3. Recommendations and 4. what they are based on", bold=True)
    for x in analysis.get("recommendations", []):
        line(f"• {x['text']} Based on: {x['basis']}")
    if analysis.get("risks"):
        line("Risks", bold=True)
        for x in analysis["risks"]:
            line(f"• {x}")
    for c in charts:
        line(c["headline"], bold=True)
        v = c["view"]
        line(f"What the chart shows: {v['explain']}")
        line(f"Impact on the company and KPIs: {v['impact']}")
        line(f"Recommendation: {v['recommendation']}")
        line(f"Based on: {v['basis']}")
    return row


def _pdf_analysis(analysis, styles):
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    body = styles["BodyText"]
    out = [Paragraph(f"{_source(analysis)}", styles["Heading2"]),
           Paragraph("1. What the dashboard shows", styles["Heading4"]),
           Paragraph(_esc(analysis["summary"]), body)]
    out += [Paragraph("• " + _esc(x), body) for x in analysis.get("findings", [])]
    out.append(Paragraph("2. Impact on the company and KPIs", styles["Heading4"]))
    out += [Paragraph("• " + _esc(x), body) for x in analysis.get("impacts", [])]
    out.append(Paragraph("3. Recommendations and 4. what they are based on",
                         styles["Heading4"]))
    out += [Paragraph(f"• {_esc(x['text'])} <i>Based on: {_esc(x['basis'])}</i>", body)
            for x in analysis.get("recommendations", [])]
    if analysis.get("risks"):
        out.append(Paragraph("Risks", styles["Heading4"]))
        out += [Paragraph("• " + _esc(x), body) for x in analysis["risks"]]
    out.append(Spacer(1, 5 * mm))
    return out


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _pdf_chart(c, styles):
    """A chart drawn with ReportLab graphics, its headline and its analysis."""
    from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, Spacer

    def compact(v):
        v = float(v)
        return (f"{v / 1e6:.1f}M" if abs(v) >= 1e6 else f"{v / 1e3:.0f}K" if abs(v) >= 1e4
                else f"{v:,.0f}")

    series = c["series"]
    horizontal = c["type"] == "hbar"
    order = list(range(len(c["labels"])))
    if horizontal:  # ReportLab draws the first category at the bottom
        order.reverse()
    data = [[float(s["data"][i]) for i in order] for s in series]
    n = len(c["labels"])
    width = 250 * mm
    height = (max(45, 9 * n) * mm) if horizontal else 70 * mm
    d = Drawing(width, height)
    labels = [lab if len(lab) <= 30 else lab[:29] + "…"
              for lab in (c["labels"][i] for i in order)]
    if c["type"] == "line":
        ch = HorizontalLineChart()
        ch.lines.strokeWidth = 1.6
        for i, s in enumerate(series):
            ch.lines[i].strokeColor = colors.HexColor(s.get("color", "#2a78d6"))
    else:
        ch = HorizontalBarChart() if horizontal else VerticalBarChart()
        ch.barSpacing = 1
        ch.groupSpacing = 6
        for i, s in enumerate(series):
            ch.bars[i].strokeColor = None
            ch.bars[i].fillColor = colors.HexColor(s.get("color", "#2a78d6"))
            cols = s.get("colors") or []
            for j, k in enumerate(order if cols else []):
                ch.bars[(i, j)].fillColor = colors.HexColor(cols[k])
    ch.data = data
    ch.x, ch.y = (60 * mm if horizontal else 18 * mm), 10 * mm
    ch.width = width - ch.x - 10 * mm
    ch.height = height - 18 * mm
    cat = ch.categoryAxis
    val = ch.valueAxis
    cat.categoryNames = labels
    cat.labels.fontSize = 7
    cat.strokeColor = colors.HexColor("#c3c2b7")
    if not horizontal:
        step = max(1, n // 12)
        cat.categoryNames = [lab if i % step == 0 else "" for i, lab in enumerate(labels)]
    val.valueMin = 0
    val.labels.fontSize = 7
    val.labelTextFormat = compact
    val.strokeColor = None
    val.visibleGrid = True
    val.gridStrokeColor = colors.HexColor("#e1e0d9")
    d.add(ch)
    if len(series) > 1:
        lg = Legend()
        lg.x, lg.y = width - 70 * mm, height - 2 * mm
        lg.fontSize = 7
        lg.alignment = "right"
        lg.colorNamePairs = [(colors.HexColor(s.get("color", "#2a78d6")), s["name"])
                             for s in series]
        d.add(lg)
    body = styles["BodyText"]
    parts = [Paragraph(_esc(c["headline"]), styles["Heading3"]),
             Paragraph(_esc(c["subtitle"]), styles["Italic"]), d,
             Paragraph("<b>What the chart shows:</b> " + _esc(c["view"]["explain"]), body),
             Paragraph("<b>Impact on the company and KPIs:</b> " + _esc(c["view"]["impact"]),
                       body)]
    if c["view"].get("recommendation"):
        parts.append(Paragraph("<b>Recommendation:</b> " + _esc(c["view"]["recommendation"]),
                               body))
    parts.append(Paragraph("<b>Based on:</b> " + _esc(c["view"]["basis"]), body))
    return [KeepTogether(parts), Spacer(1, 6 * mm)]
