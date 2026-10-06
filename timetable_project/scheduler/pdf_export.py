"""
PDF generation for timetables, faculty load reports and scheduling issues.

Uses ReportLab so no system libraries are required. Everything is laid out to
handle arbitrarily many divisions: the full-institution report paginates with
one grid per page plus a cover sheet and an index, so a 30-division college
produces a clean 32-page document rather than one unreadable table.
"""

from collections import defaultdict
from io import BytesIO
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    Image,
)

from .models import (
    DAY_CHOICES,
    Assignment,
    SchedulingIssue,
    Teacher,
    TimetableEntry,
    TimeSlot,
    YearDivision,
)

# Path to VIT logo
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VIT_LOGO_PATH = os.path.join(BASE_DIR, "static", "scheduler", "vit_logo.png")

INDIGO_DARK = colors.HexColor("#312e81")
SLATE_900 = colors.HexColor("#0f172a")
SLATE_200 = colors.HexColor("#e2e8f0")
SLATE_500 = colors.HexColor("#64748b")
INDIGO = colors.HexColor("#4f46e5")

LAB_FILL = colors.HexColor("#f3e8ff")
LAB_BORDER = colors.HexColor("#d8b4fe")
THEORY_FILL = colors.HexColor("#eef2ff")
THEORY_BORDER = colors.HexColor("#c7d2fe")
FREE_FILL = colors.HexColor("#f8fafc")

DAY_LABELS = dict(DAY_CHOICES)
DAY_SEQUENCE = [key for key, _ in DAY_CHOICES]


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "TTTitle", parent=base["Title"], fontSize=22, leading=26,
            textColor=INDIGO_DARK, alignment=TA_CENTER, spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "TTSubtitle", parent=base["Normal"], fontSize=10.5, leading=14,
            textColor=SLATE_500, alignment=TA_CENTER,
        ),
        "h2": ParagraphStyle(
            "TTH2", parent=base["Heading2"], fontSize=13, leading=16,
            textColor=INDIGO_DARK, spaceBefore=10, spaceAfter=6,
        ),
        "cell": ParagraphStyle("TTCell", parent=base["Normal"], fontSize=7.2, leading=8.8),
        "cellSmall": ParagraphStyle(
            "TTSmall", parent=base["Normal"], fontSize=6.2, leading=7.4,
            textColor=SLATE_500,
        ),
        "cellSubject": ParagraphStyle(
            "TTSubject", parent=base["Normal"], fontSize=7.4, leading=8.8,
            fontName="Helvetica-Bold", textColor=SLATE_900,
        ),
        "index": ParagraphStyle("TTIndex", parent=base["Normal"], fontSize=9, leading=13),
    }


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _ordered_days_and_periods(time_slots):
    days = [d for d in DAY_SEQUENCE if any(s.day == d for s in time_slots)]
    periods = sorted({s.period_number for s in time_slots})
    period_info = {}
    for slot in time_slots:
        period_info.setdefault(
            slot.period_number,
            f"{slot.start_time.strftime('%H:%M')}-{slot.end_time.strftime('%H:%M')}",
        )
    return days, periods, period_info


def _entries_grid(entries, time_slots, show_division=False, show_teacher=True, styles=None,
                  page_width=None):
    """Build the ReportLab table for one Day x Period grid."""
    styles = styles or _styles()
    days, periods, period_info = _ordered_days_and_periods(time_slots)

    grid = {(e.time_slot.period_number, e.time_slot.day): e for e in entries}
    cell_count = len(days) or 1

    header = [Paragraph("<b>Period / Time</b>", styles["cellSubject"])]
    for day in days:
        header.append(Paragraph(f"<b>{DAY_LABELS.get(day, day)}</b>", styles["cell"]))

    rows = [header]
    cell_kinds = []  # flat list of "lab" / "theory" / "free", row-major

    for period in periods:
        label = Paragraph(
            f"<b>Period {period}</b><br/>"
            f"<font size=6 color='#64748b'>{_escape(period_info.get(period, ''))}</font>",
            styles["cell"],
        )
        row = [label]
        for day in days:
            entry = grid.get((period, day))
            if entry is None:
                row.append(Paragraph("<i>Free</i>", styles["cellSmall"]))
                cell_kinds.append("free")
                continue
            subject = entry.assignment.subject
            bits = [_escape(subject.name) + (" <b>[LAB]</b>" if subject.is_lab else "")]
            if show_division:
                bits.append(f'<font color="#64748b">{_escape(entry.assignment.division.name)}</font>')
            if show_teacher:
                bits.append(f'<font color="#64748b">{_escape(entry.assignment.teacher.name)}</font>')
            bits.append(f'<font color="#312e81">{_escape(entry.room.name)}</font>')
            row.append(Paragraph("<br/>".join(bits), styles["cell"]))
            cell_kinds.append("lab" if subject.is_lab else "theory")
        rows.append(row)

    if page_width is None:
        page_width = landscape(A3)[0]
    usable = page_width - 28 * mm  # matches the document margins
    period_col = 26 * mm
    day_col = max(20 * mm, (usable - period_col) / cell_count)

    table = Table(rows, colWidths=[period_col] + [day_col] * len(days))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SLATE_900),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.8, SLATE_500),  # Thicker grid lines
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#f1f5f9")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 1), (-1, -1), 5),
        ("RIGHTPADDING", (0, 1), (-1, -1), 5),
        ("ROWBACKGROUNDS", (1, 1), (-1, -1), [colors.white, colors.HexColor("#fbfcfe")]),
        ("BOX", (0, 0), (-1, -1), 1.2, SLATE_900),  # Outer border
        ("INNERGRID", (0, 0), (-1, -1), 0.6, SLATE_200),  # Inner grid lines
    ]))

    # Colour each day cell according to its subject type.
    for index, kind in enumerate(cell_kinds):
        row_index = index // cell_count + 1  # +1 skips the header row
        column = index % cell_count + 1     # +1 skips the period label column
        if kind == "lab":
            fill, border = LAB_FILL, LAB_BORDER
        elif kind == "theory":
            fill, border = THEORY_FILL, THEORY_BORDER
        else:
            fill, border = FREE_FILL, None
        commands = [("BACKGROUND", (column, row_index), (column, row_index), fill)]
        if border is not None:
            commands.append(("BOX", (column, row_index), (column, row_index), 0.8, border))
        table.setStyle(TableStyle(commands))

    return table


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(SLATE_500)
    canvas.drawString(14 * mm, 9 * mm, "Automatic Timetable Generator - Google OR-Tools CP-SAT")
    canvas.drawRightString(doc.pagesize[0] - 14 * mm, 9 * mm, f"Page {doc.page}")
    canvas.setStrokeColor(SLATE_200)
    canvas.line(14 * mm, 12 * mm, doc.pagesize[0] - 14 * mm, 12 * mm)
    canvas.restoreState()


def build_division_timetable_pdf(semester, division, show_division=False):
    """One page containing a single division's weekly grid."""
    buffer = BytesIO()
    page_size = landscape(A3)
    doc = SimpleDocTemplate(
        buffer, pagesize=page_size,
        leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=16 * mm,
        title=f"{division.name} Timetable",
    )
    styles = _styles()
    time_slots = list(TimeSlot.objects.all())
    entries = list(
        TimetableEntry.objects.filter(semester=semester, assignment__division=division)
        .select_related("assignment__subject", "assignment__teacher", "room", "time_slot")
    )

    # Add VIT logo at the top
    story = []
    if os.path.exists(VIT_LOGO_PATH):
        logo = Image(VIT_LOGO_PATH, width=80*mm, height=32*mm)
        logo.hAlign = 'CENTER'
        story.append(logo)
        story.append(Spacer(1, 6))

    story.extend([
        Paragraph(_escape(division.name), styles["title"]),
        Paragraph(
            f"{_escape(semester.name)} &nbsp;|&nbsp; {_escape(semester.start_date.strftime('%d %b %Y'))}"
            f" - {_escape(semester.end_date.strftime('%d %b %Y'))}"
            f" &nbsp;|&nbsp; Strength: {division.strength} &nbsp;|&nbsp; {len(entries)} periods/week",
            styles["subtitle"],
        ),
        Spacer(1, 8),
    ])

    if entries:
        story.append(_entries_grid(
            entries, time_slots, show_division=show_division, show_teacher=True,
            styles=styles, page_width=page_size[0],
        ))
    else:
        story.append(Paragraph("No classes have been scheduled for this division yet.", styles["subtitle"]))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buffer.seek(0)
    return buffer


def build_full_institution_pdf(semester):
    """
    Cover sheet + index + one page per division. Scales to any division count
    because each division starts on a fresh page.
    """
    buffer = BytesIO()
    page_size = landscape(A3)
    doc = SimpleDocTemplate(
        buffer, pagesize=page_size,
        leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=16 * mm,
        title=f"{semester.name} Complete Timetable",
    )
    styles = _styles()
    time_slots = list(TimeSlot.objects.all())

    divisions = list(
        YearDivision.objects.filter(assignments__semester=semester)
        .distinct().order_by("year", "division_number")
    )

    all_entries = list(
        TimetableEntry.objects.filter(semester=semester)
        .select_related("assignment__subject", "assignment__teacher",
                        "assignment__division", "room", "time_slot")
    )
    by_division = defaultdict(list)
    for entry in all_entries:
        by_division[entry.assignment.division_id].append(entry)

    total_hours = len(all_entries)

    story = []
    
    # Add VIT logo on cover page
    if os.path.exists(VIT_LOGO_PATH):
        logo = Image(VIT_LOGO_PATH, width=100*mm, height=40*mm)
        logo.hAlign = 'CENTER'
        story.append(logo)
        story.append(Spacer(1, 10 * mm))

    story.extend([
        Spacer(1, 20 * mm),
        Paragraph("COMPLETE COLLEGE TIMETABLE", styles["title"]),
        Paragraph(_escape(semester.name), ParagraphStyle(
            "Semi", parent=styles["subtitle"], fontSize=14, textColor=INDIGO)),
        Spacer(1, 10 * mm),
    ])

    summary_rows = [
        ["Divisions", str(len(divisions))],
        ["Scheduled periods / week", str(total_hours)],
        ["Working days per week", str(len({s.day for s in time_slots}))],
        ["Periods per day", str(len({s.period_number for s in time_slots}))],
        ["Faculty members", str(
            Teacher.objects.filter(assignments__semester=semester).distinct().count())],
        ["Semester starts", semester.start_date.strftime("%d %B %Y")],
        ["Semester ends", semester.end_date.strftime("%d %B %Y")],
    ]
    summary = Table(summary_rows, colWidths=[70 * mm, 55 * mm], hAlign="CENTER")
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2ff")),
        ("TEXTCOLOR", (0, 0), (0, -1), INDIGO_DARK),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.4, SLATE_200),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(summary)
    story.append(Spacer(1, 14 * mm))

    if divisions:
        story.append(Paragraph("Divisions in this document", styles["h2"]))
        per_row = 4
        index_rows = []
        for i in range(0, len(divisions), per_row):
            index_rows.append([
                Paragraph(f"{d.name}<br/><font size=6.5 color='#64748b'>"
                          f"{len(by_division.get(d.id, []))} periods</font>", styles["index"])
                for d in divisions[i:i + per_row]
            ])
        index_table = Table(index_rows, colWidths=[(page_size[0] - 28 * mm) / per_row] * per_row)
        index_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, SLATE_200),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(index_table)

    for division in divisions:
        story.append(PageBreak())
        
        # Add VIT logo on each division page
        if os.path.exists(VIT_LOGO_PATH):
            logo = Image(VIT_LOGO_PATH, width=80*mm, height=32*mm)
            logo.hAlign = 'CENTER'
            story.append(logo)
            story.append(Spacer(1, 4))
        
        story.append(Paragraph(_escape(division.name), styles["title"]))
        story.append(Paragraph(
            f"{_escape(semester.name)} &nbsp;|&nbsp; Strength: {division.strength}"
            f" &nbsp;|&nbsp; {len(by_division.get(division.id, []))} periods/week",
            styles["subtitle"]))
        story.append(Spacer(1, 7))
        entries = by_division.get(division.id, [])
        if entries:
            story.append(_entries_grid(
                entries, time_slots, show_teacher=True, styles=styles, page_width=page_size[0]
            ))
        else:
            story.append(Paragraph("No classes scheduled for this division.", styles["subtitle"]))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buffer.seek(0)
    return buffer


def build_teacher_load_pdf(semester):
    """Faculty workload report: assigned vs. scheduled hours per teacher."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=16 * mm,
        title=f"{semester.name} Faculty Load",
    )
    styles = _styles()

    assignments = list(
        Assignment.objects.filter(semester=semester)
        .select_related("teacher", "subject", "division")
        .order_by("teacher__name")
    )

    scheduled_per_teacher = defaultdict(int)
    for entry in TimetableEntry.objects.filter(semester=semester).select_related("assignment"):
        scheduled_per_teacher[entry.assignment.teacher_id] += 1

    requested_per_teacher = defaultdict(int)
    divisions_per_teacher = defaultdict(set)
    subjects_per_teacher = defaultdict(set)
    for a in assignments:
        requested_per_teacher[a.teacher_id] += a.weekly_hours()
        divisions_per_teacher[a.teacher_id].add(a.division.name)
        subjects_per_teacher[a.teacher_id].add(a.subject.name)

    story = []
    
    # Add VIT logo
    if os.path.exists(VIT_LOGO_PATH):
        logo = Image(VIT_LOGO_PATH, width=80*mm, height=32*mm)
        logo.hAlign = 'CENTER'
        story.append(logo)
        story.append(Spacer(1, 6))

    story.extend([
        Paragraph("FACULTY WORKLOAD REPORT", styles["title"]),
        Paragraph(_escape(semester.name), styles["subtitle"]),
        Spacer(1, 8),
    ])

    rows = [[
        Paragraph("<b>Teacher</b>", styles["cell"]),
        Paragraph("<b>Divisions</b>", styles["cell"]),
        Paragraph("<b>Subjects</b>", styles["cell"]),
        Paragraph("<b>Assigned h/wk</b>", styles["cell"]),
        Paragraph("<b>Scheduled h/wk</b>", styles["cell"]),
        Paragraph("<b>Contract max</b>", styles["cell"]),
        Paragraph("<b>Status</b>", styles["cell"]),
    ]]

    over_limit = 0
    seen = set()
    for assignment in assignments:
        if assignment.teacher_id in seen:
            continue
        seen.add(assignment.teacher_id)
        teacher = assignment.teacher
        requested = requested_per_teacher[teacher.id]
        scheduled = scheduled_per_teacher.get(teacher.id, 0)
        limit = teacher.max_hours_per_week or 0
        over = requested > limit
        if over:
            over_limit += 1
        rows.append([
            Paragraph(_escape(teacher.name), styles["cellSubject"]),
            Paragraph(str(len(divisions_per_teacher[teacher.id])), styles["cell"]),
            Paragraph(str(len(subjects_per_teacher[teacher.id])), styles["cell"]),
            Paragraph(str(requested), styles["cell"]),
            Paragraph(str(scheduled), styles["cell"]),
            Paragraph(str(limit), styles["cell"]),
            Paragraph(
                f'<font color="{"#e11d48" if over else "#059669"}">'
                f'{"OVER LIMIT" if over else "OK"}</font>', styles["cell"]),
        ])

    table = Table(rows, repeatRows=1, colWidths=[52 * mm, 20 * mm, 22 * mm, 24 * mm, 26 * mm, 24 * mm, 24 * mm])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), SLATE_900),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.8, SLATE_500),  # Thicker grid
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("BOX", (0, 0), (-1, -1), 1.2, SLATE_900),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, SLATE_200),
    ]
    table.setStyle(TableStyle(style))

    story.append(table)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"{len(seen)} faculty member(s). {over_limit} over their contractual weekly limit.",
        styles["subtitle"]))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buffer.seek(0)
    return buffer


def build_issues_pdf(semester):
    """Report of every class that could not be fully scheduled, with reasons."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=16 * mm,
        title=f"{semester.name} Scheduling Issues",
    )
    styles = _styles()

    issues = list(
        SchedulingIssue.objects.filter(semester=semester)
        .select_related("assignment__division", "assignment__subject", "assignment__teacher")
        .order_by("assignment__division__year", "assignment__division__division_number")
    )

    story = []
    
    # Add VIT logo
    if os.path.exists(VIT_LOGO_PATH):
        logo = Image(VIT_LOGO_PATH, width=80*mm, height=32*mm)
        logo.hAlign = 'CENTER'
        story.append(logo)
        story.append(Spacer(1, 6))

    story.extend([
        Paragraph("SCHEDULING ISSUE REPORT", styles["title"]),
        Paragraph(_escape(semester.name), styles["subtitle"]),
        Spacer(1, 8),
    ])

    if not issues:
        story.append(Paragraph(
            "No issues: every requested hour was scheduled successfully.", styles["subtitle"]))
    else:
        rows = [[
            Paragraph("<b>Division</b>", styles["cell"]),
            Paragraph("<b>Subject</b>", styles["cell"]),
            Paragraph("<b>Teacher</b>", styles["cell"]),
            Paragraph("<b>Req.</b>", styles["cell"]),
            Paragraph("<b>Got</b>", styles["cell"]),
            Paragraph("<b>Short</b>", styles["cell"]),
            Paragraph("<b>Reason & suggested action</b>", styles["cell"]),
        ]]
        for issue in issues:
            short = issue.hours_requested - issue.hours_scheduled
            rows.append([
                Paragraph(_escape(issue.assignment.division.name), styles["cellSubject"]),
                Paragraph(_escape(issue.assignment.subject.name), styles["cell"]),
                Paragraph(_escape(issue.assignment.teacher.name), styles["cell"]),
                Paragraph(str(issue.hours_requested), styles["cell"]),
                Paragraph(str(issue.hours_scheduled), styles["cell"]),
                Paragraph(f'<font color="#e11d48"><b>{short}</b></font>', styles["cell"]),
                Paragraph(
                    f"{_escape(issue.reason)}<br/><font color='#059669'><b>Fix:</b> "
                    f"{_escape(issue.suggestion)}</font>", styles["cell"]),
            ])

        table = Table(
            rows, repeatRows=1,
            colWidths=[34 * mm, 30 * mm, 30 * mm, 12 * mm, 12 * mm, 12 * mm, 145 * mm],
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), SLATE_900),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.8, SLATE_500),  # Thicker grid
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (3, 1), (5, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fff7ed")]),
            ("BOX", (0, 0), (-1, -1), 1.2, SLATE_900),
            ("INNERGRID", (0, 0), (-1, -1), 0.6, SLATE_200),
        ]))
        story.append(table)
        story.append(Spacer(1, 8))
        total_short = sum(i.hours_requested - i.hours_scheduled for i in issues)
        story.append(Paragraph(
            f"{len(issues)} issue(s), {total_short} period(s) of curriculum not placed.",
            styles["subtitle"]))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buffer.seek(0)
    return buffer