"""Client-facing project report: headline stats, a Gantt chart and the task list, as a PDF.

It leaves out anything internal: budget, time tracked, invoices, priorities and notes.
"""
from datetime import date, timedelta
from html import escape
from io import BytesIO
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Flowable, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

REG, BOLD = "Helvetica", "Helvetica-Bold"
INK, SOFT, LINE, TINT = colors.HexColor("#16233A"), colors.HexColor("#5B6072"), colors.HexColor("#D5DAE3"), colors.HexColor("#F3F5F9")
OVERDUE = colors.HexColor("#D8383E")
STATUS_COLOURS = {
    "To Do": "#A8B3C7", "Planning": "#7A5CE0", "In Progress": "#2F5FA8", "At Risk": "#D8383E",
    "Update Required": "#E39A1E", "On Hold": "#8A8FA0", "Complete": "#2E9964",
}
PAGE = landscape(A4)
MARGIN = 14 * mm
WIDTH = PAGE[0] - 2 * MARGIN


def _colour(status):
    return colors.HexColor(STATUS_COLOURS.get(status, "#A8B3C7"))


def _day(value):
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _nice(day):
    return f"{day.day} {day.strftime('%b %Y')}" if day else "-"


def _fit(text, font, size, width):
    """The text, shortened with an ellipsis so it fits the width."""
    text = str(text or "")
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "...", font, size) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def _latin(text):
    """The built-in PDF fonts only cover Latin-1; swap anything else for '?' rather than a black box."""
    return str(text or "").encode("latin-1", "replace").decode("latin-1")


class Gantt(Flowable):
    """One page-worth of Gantt rows on a shared time axis."""
    LABEL, HEAD, ROW = 78 * mm, 12 * mm, 6.4 * mm

    def __init__(self, rows, first, last, today):
        super().__init__()
        self.rows, self.first, self.last, self.today = rows, first, last, today
        self.width, self.height = WIDTH, self.HEAD + self.ROW * len(rows)

    def wrap(self, *_):
        return self.width, self.height

    def _x(self, day):
        span = max((self.last - self.first).days + 1, 1)
        return self.LABEL + (self.width - self.LABEL) * (day - self.first).days / span

    def draw(self):
        c, top = self.canv, self.height
        days = (self.last - self.first).days + 1
        # Month band, then a line per week (per day on short charts).
        c.setFillColor(TINT)
        c.rect(0, top - self.HEAD, self.width, self.HEAD, stroke=0, fill=1)
        month = self.first.replace(day=1)
        while month <= self.last:
            following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            left, right = self._x(max(month, self.first)), self._x(min(following, self.last + timedelta(days=1)))
            c.setStrokeColor(LINE)
            c.setLineWidth(.5)
            c.line(left, 0, left, top)
            c.setFillColor(INK)
            c.setFont(BOLD, 8)
            label = month.strftime("%B %Y") if right - left > 70 else month.strftime("%b %y")
            if right - left > stringWidth(label, BOLD, 8) + 6:
                c.drawString(left + 3, top - 4.6 * mm, label)
            month = following
        step = 1 if days <= 45 else 7
        tick = self.first if step == 1 else self.first + timedelta(days=(7 - self.first.weekday()) % 7)
        c.setFont(REG, 6.5)
        while tick <= self.last:
            x = self._x(tick)
            c.setStrokeColor(colors.HexColor("#E7EAF0"))
            c.setLineWidth(.3)
            c.line(x, 0, x, top - self.HEAD / 2)
            if step == 7 or tick.weekday() == 0 or days <= 21:
                c.setFillColor(SOFT)
                c.drawString(x + 1.5, top - 10.2 * mm, str(tick.day))
            tick += timedelta(days=step)
        # Rows.
        for index, row in enumerate(self.rows):
            y = top - self.HEAD - self.ROW * (index + 1)
            if index % 2:
                c.setFillColor(colors.HexColor("#FAFBFD"))
                c.rect(0, y, self.width, self.ROW, stroke=0, fill=1)
            c.setStrokeColor(colors.HexColor("#E7EAF0"))
            c.setLineWidth(.3)
            c.line(0, y, self.width, y)
            indent = 4 * mm if row["level"] else 0
            font = REG if row["level"] else BOLD
            c.setFillColor(INK)
            c.setFont(font, 7.6)
            c.drawString(2 + indent, y + 2.1 * mm, _fit(row["title"], font, 7.6, self.LABEL - indent - 8))
            start, due = row["start"] or row["due"], row["due"] or row["start"]
            if not start:
                c.setFillColor(SOFT)
                c.setFont(REG, 6.5)
                c.drawString(self.LABEL + 4, y + 2.2 * mm, "Not scheduled")
                continue
            left, right = self._x(start), self._x(due + timedelta(days=1))
            c.setFillColor(_colour(row["status"]))
            c.setStrokeColor(OVERDUE)
            c.setLineWidth(1)
            c.roundRect(left, y + 1.5 * mm, max(right - left, 2.2), self.ROW - 3 * mm, 1.4, stroke=1 if row["overdue"] else 0, fill=1)
        # Frame, label divider and today.
        c.setStrokeColor(LINE)
        c.setLineWidth(.6)
        c.rect(0, 0, self.width, top, stroke=1, fill=0)
        c.line(self.LABEL, 0, self.LABEL, top)
        c.line(0, top - self.HEAD, self.width, top - self.HEAD)
        if self.first <= self.today <= self.last:
            x = self._x(self.today) + (self._x(self.today + timedelta(days=1)) - self._x(self.today)) / 2
            c.setStrokeColor(OVERDUE)
            c.setLineWidth(.9)
            c.setDash(2, 2)
            c.line(x, 0, x, top - self.HEAD)
            c.setDash()
            c.setFillColor(OVERDUE)
            c.setFont(BOLD, 6.5)
            c.drawCentredString(min(max(x, self.LABEL + 12), self.width - 12), -7.5, "Today")


class ProgressBar(Flowable):
    def __init__(self, fraction, width=WIDTH, height=3.2 * mm):
        super().__init__()
        self.fraction, self.width, self.height = max(0, min(1, fraction)), width, height

    def wrap(self, *_):
        return self.width, self.height

    def draw(self):
        self.canv.setFillColor(colors.HexColor("#E7EAF0"))
        self.canv.roundRect(0, 0, self.width, self.height, self.height / 2, stroke=0, fill=1)
        if self.fraction:
            self.canv.setFillColor(_colour("Complete"))
            self.canv.roundRect(0, 0, max(self.width * self.fraction, self.height), self.height, self.height / 2, stroke=0, fill=1)


class Legend(Flowable):
    def __init__(self, statuses):
        super().__init__()
        self.statuses, self.width, self.height = statuses, WIDTH, 5 * mm

    def wrap(self, *_):
        return self.width, self.height

    def draw(self):
        c, x = self.canv, 0
        c.setFont(REG, 7.5)
        for status in self.statuses:
            c.setFillColor(_colour(status))
            c.roundRect(x, 1, 8, 5.5, 1.2, stroke=0, fill=1)
            c.setFillColor(SOFT)
            c.drawString(x + 11, 1.4, status)
            x += 11 + stringWidth(status, REG, 7.5) + 12
        c.setStrokeColor(OVERDUE)
        c.setFillColor(colors.white)
        c.setLineWidth(1)
        c.roundRect(x, 1, 8, 5.5, 1.2, stroke=1, fill=1)
        c.setFillColor(SOFT)
        c.drawString(x + 11, 1.4, "Red outline: past its due date")


def build_project_report_pdf(company, project, tasks, today=None, logo_path=None):
    """Return a BytesIO holding the report.

    company: {'name', 'website'}; project: {'name', 'client', 'type', 'status', 'start', 'target', 'manager', 'description'};
    tasks: [{'title', 'status', 'start', 'due', 'level' (0 task, 1 subtask), 'assignees' [names], 'completed'}] in display order.
    """
    today = today or date.today()
    rows = []
    for task in tasks:
        start, due = _day(task.get("start")), _day(task.get("due"))
        if start and due and due < start:
            start, due = due, start
        done = task["status"] == "Complete"
        rows.append({**task, "title": _latin(task["title"]), "start": start, "due": due, "done": done,
                     "overdue": bool(due and due < today and not done)})
    total = len(rows)
    done = sum(row["done"] for row in rows)
    overdue = sum(row["overdue"] for row in rows)
    active = sum(row["status"] in ("In Progress", "At Risk", "Update Required") for row in rows)
    percent = round(100 * done / total) if total else 0

    h1 = ParagraphStyle("h1", fontName=BOLD, fontSize=20, leading=24, textColor=INK)
    h2 = ParagraphStyle("h2", fontName=BOLD, fontSize=11.5, leading=15, textColor=INK, spaceBefore=4, spaceAfter=5)
    body = ParagraphStyle("body", fontName=REG, fontSize=9, leading=13, textColor=INK)
    soft = ParagraphStyle("soft", parent=body, textColor=SOFT, fontSize=8.5, leading=12)
    right = ParagraphStyle("right", parent=soft, alignment=2)
    cell = ParagraphStyle("cell", parent=body, fontSize=8.3, leading=11)
    tile_value = ParagraphStyle("tile_value", fontName=BOLD, fontSize=19, leading=22, textColor=INK)
    tile_label = ParagraphStyle("tile_label", parent=soft, fontSize=8)
    text = lambda value: escape(_latin(value))

    story = []
    # Header: who the report is from, and when.
    mark = Paragraph(f"<b>{text(company.get('name'))}</b>", body)
    if logo_path and os.path.isfile(logo_path):
        try:
            mark = Image(logo_path, width=42 * mm, height=42 * mm * 329 / 2048, hAlign="LEFT")
        except Exception:
            pass
    header = Table([[mark, Paragraph(f"Project report<br/>Prepared {_nice(today)}", right)]], colWidths=[WIDTH * .6, WIDTH * .4])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                                ("LINEBELOW", (0, 0), (-1, -1), .8, LINE)]))
    story += [header, Spacer(1, 6 * mm), Paragraph(text(project["name"]), h1), Spacer(1, 1.5 * mm)]
    facts = [("Client", project.get("client")), ("Type", project.get("type")), ("Status", project.get("status")),
             ("Start", _nice(_day(project.get("start"))) if project.get("start") else None),
             ("Target", _nice(_day(project.get("target"))) if project.get("target") else None),
             ("Project lead", project.get("manager"))]
    story.append(Paragraph(" &nbsp;|&nbsp; ".join(f"{label}: <b>{text(value)}</b>" for label, value in facts if value), soft))
    if project.get("description"):
        story += [Spacer(1, 3 * mm), Paragraph(text(project["description"]).replace("\n", "<br/>"), body)]
    story.append(Spacer(1, 6 * mm))

    # Headline numbers.
    tiles = [(f"{percent}%", "Complete"), (str(total), "Tasks in total"), (str(done), "Finished"),
             (str(active), "In progress"), (str(total - done - active), "Not started or on hold"), (str(overdue), "Past due date")]
    tile_alert = ParagraphStyle("tile_alert", parent=tile_value, textColor=OVERDUE)
    grid = Table([[[Paragraph(value, tile_alert if label == "Past due date" and overdue else tile_value), Paragraph(label, tile_label)]
                   for value, label in tiles]],
                 colWidths=[WIDTH / len(tiles)] * len(tiles))
    grid.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), .6, LINE), ("INNERGRID", (0, 0), (-1, -1), .6, LINE),
                              ("BACKGROUND", (0, 0), (-1, -1), TINT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 7),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [grid, Spacer(1, 3 * mm), ProgressBar(done / total if total else 0), Spacer(1, 6 * mm)]

    # Tasks by status.
    counts = [(status, sum(row["status"] == status for row in rows)) for status in STATUS_COLOURS]
    counts = [(status, count) for status, count in counts if count]
    if counts:
        story.append(Paragraph("Tasks by status", h2))
        table = Table([[Paragraph(f'<font color="{STATUS_COLOURS[status]}">&#9632;</font> {status}', cell), str(count),
                        f"{round(100 * count / total)}%"] for status, count in counts], colWidths=[60 * mm, 18 * mm, 18 * mm], hAlign="LEFT")
        table.setStyle(TableStyle([("FONT", (1, 0), (-1, -1), REG, 8.5), ("TEXTCOLOR", (1, 0), (-1, -1), INK),
                                   ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, -2), .4, LINE),
                                   ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(table)

    # Gantt: one shared axis, split over pages when there are many rows.
    dated = [day for row in rows for day in (row["start"], row["due"]) if day]
    if dated:
        first, last = min(dated), max(dated)
        pad = max(1, (last - first).days // 30)
        first, last = first - timedelta(days=pad), last + timedelta(days=pad)
        per_page = int((PAGE[1] - 2 * MARGIN - 30 * mm - Gantt.HEAD) // Gantt.ROW)
        used = [status for status in STATUS_COLOURS if any(row["status"] == status for row in rows)]
        for offset in range(0, len(rows), per_page):
            story += [PageBreak(), Paragraph("Timeline" + (" (continued)" if offset else ""), h2),
                      Paragraph(f"{_nice(first)} to {_nice(last)}", soft), Spacer(1, 3 * mm),
                      Gantt(rows[offset:offset + per_page], first, last, today), Spacer(1, 3 * mm), Legend(used)]

    # Task list.
    if rows:
        story += [PageBreak(), Paragraph("Task list", h2)]
        data = [["Task", "Status", "Start", "Due", "Assigned to"]]
        styles = [("FONT", (0, 0), (-1, 0), BOLD, 8.3), ("TEXTCOLOR", (0, 0), (-1, 0), INK), ("BACKGROUND", (0, 0), (-1, 0), TINT),
                  ("LINEBELOW", (0, 0), (-1, -1), .4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                  ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
        for row in rows:
            title = ("&nbsp;&nbsp;&nbsp;&nbsp;" if row["level"] else "") + (text(row["title"]) if row["level"] else f"<b>{text(row['title'])}</b>")
            due = _nice(row["due"]) + (' <font color="#D8383E">(overdue)</font>' if row["overdue"] else "")
            data.append([Paragraph(title, cell), Paragraph(f'<font color="{STATUS_COLOURS.get(row["status"], "#A8B3C7")}">&#9632;</font> {text(row["status"])}', cell),
                         Paragraph(_nice(row["start"]), cell), Paragraph(due, cell),
                         Paragraph(text(", ".join(row.get("assignees") or [])) or "-", cell)])
        table = Table(data, colWidths=[WIDTH * .38, WIDTH * .14, WIDTH * .12, WIDTH * .17, WIDTH * .19], repeatRows=1)
        table.setStyle(TableStyle(styles))
        story.append(table)
    else:
        story.append(Paragraph("No tasks have been added to this project yet.", soft))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(REG, 7.5)
        canvas.setFillColor(SOFT)
        canvas.drawString(MARGIN, 8 * mm, _latin(" | ".join(part for part in (company.get("name"), company.get("website")) if part)))
        canvas.drawRightString(PAGE[0] - MARGIN, 8 * mm, f"{_latin(project['name'])} | Page {doc.page}")
        canvas.restoreState()

    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN + 3 * mm,
                      title=f"{_latin(project['name'])} - project report", author=_latin(company.get("name"))
                      ).build(story, onFirstPage=footer, onLaterPages=footer)
    buffer.seek(0)
    return buffer
