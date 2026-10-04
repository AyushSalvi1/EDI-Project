"""
Generate a deliberately messy 'real college' PDF for testing the importer.

Nothing here matches the expected template: header wording differs, column order
is shuffled, dates use 'DD Mon YYYY', working days are a range, the lab flag is
the letter 'P', rooms are implied, and there are 12 divisions numbered 1-12.
"""
import os
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

OUT = sys.argv[1] if len(sys.argv) > 1 else "messy_college.pdf"

styles = getSampleStyleSheet()
h1 = ParagraphStyle("H1", parent=styles["Title"], fontSize=17, textColor=colors.HexColor("#1e3a8a"))

story = [
    Paragraph("Sardar Vallabhbhai Patel Institute of Technology", h1),
    Paragraph("Department of Computer Engineering &nbsp;|&nbsp; Time Table Committee Report",
              styles["Normal"]),
    Spacer(1, 8 * mm),
]

# ---- Calendar block: label/value pairs with awkward wording ----------------
calendar_rows = [
    ["Academic Session", "Autumn Semester 2026"],
    ["Commences on", "03 Aug 2026"],
    ["Concludes on", "20 Nov 2026"],
    ["Week days", "Monday to Friday"],
    ["College Timing", "09:00 am to 05:00 pm"],
    ["Lecture duration (minutes)", "60"],
    ["Lunch break", "01:00 pm to 02:00 pm"],
]
cal = Table(calendar_rows, colWidths=[70 * mm, 95 * mm])
cal.setStyle(TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e0e7ff")),
    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 9),
    ("TOPPADDING", (0, 0), (-1, -1), 4),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
]))
story.append(cal)
story.append(Spacer(1, 8 * mm))

story.append(Paragraph("Faculty workload and subject allocation", styles["Heading2"]))
story.append(Spacer(1, 3 * mm))

# ---- Allocations: shuffled columns, odd headers, 12 divisions --------------
# Columns: Faculty | Subject | Yr | Div | Hrs/Sem | Max hrs/wk | Nature | Unavailable | Strength
alloc_data = [
    ["Dr. Meera Krishnan", "Data Structures", 2, 1, 45, 18, "Theory", "Mon-P2", 62],
    ["Dr. Meera Krishnan", "Programming Practical", 2, 1, 30, 18, "P", "", 62],
    ["Dr. Meera Krishnan", "Data Structures", 2, 2, 45, 18, "Theory", "", 62],
    ["Dr. Meera Krishnan", "Programming Practical", 2, 2, 30, 18, "P", "", 62],
    ["Prof. Anil Deshpande", "Discrete Mathematics", 1, 1, 60, 20, "Theory", "Fri-P5", 66],
    ["Prof. Anil Deshpande", "Discrete Mathematics", 1, 2, 60, 20, "Theory", "", 66],
    ["Prof. Anil Deshpande", "Discrete Mathematics", 1, 3, 60, 20, "Theory", "", 66],
    ["Prof. Anil Deshpande", "Engineering Physics Lab", 1, 1, 30, 20, "P", "", 66],
    ["Dr. Farah Qureshi", "Operating Systems", 3, 1, 45, 16, "Theory", "", 58],
    ["Dr. Farah Qureshi", "Operating Systems", 3, 2, 45, 16, "Theory", "", 58],
    ["Dr. Farah Qureshi", "Systems Lab", 3, 1, 30, 16, "P", "", 58],
    ["Prof. Ravi Menon", "Computer Networks", 4, 1, 45, 14, "Theory", "", 54],
    ["Prof. Ravi Menon", "Networks Lab", 4, 1, 30, 14, "P", "", 54],
    ["Prof. Kavita Bose", "Thermodynamics", 2, 3, 45, 18, "Theory", "", 60],
    ["Prof. Kavita Bose", "Thermodynamics", 3, 3, 45, 18, "Theory", "", 58],
]

alloc_header = ["Faculty Name", "Subject", "Yr", "Div", "Total Hours",
                "Max hrs/week", "Nature", "Unavailable", "Strength"]

alloc = Table([alloc_header] + alloc_data,
              colWidths=[38 * mm, 40 * mm, 10 * mm, 10 * mm, 18 * mm,
                         20 * mm, 16 * mm, 22 * mm, 18 * mm])
alloc.setStyle(TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#312e81")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
]))
story.append(alloc)
story.append(Spacer(1, 8 * mm))

story.append(Paragraph("Rooms available", styles["Heading2"]))
room_rows = [
    ["Room No.", "Seat Capacity", "Room Type"],
    ["LH-101", "70", "Classroom"],
    ["LH-102", "70", "Classroom"],
    ["LH-103", "70", "Classroom"],
    ["LH-104", "70", "Classroom"],
    ["LH-201", "70", "Classroom"],
    ["LH-202", "70", "Classroom"],
    ["LAB-A", "60", "Laboratory"],
    ["LAB-B", "60", "Laboratory"],
]
rooms = Table(room_rows, colWidths=[40 * mm, 35 * mm, 35 * mm])
rooms.setStyle(TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#312e81")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
]))
story.append(rooms)

doc = SimpleDocTemplate(OUT, pagesize=landscape(A4),
                        leftMargin=15 * mm, rightMargin=15 * mm,
                        topMargin=12 * mm, bottomMargin=12 * mm,
                        title="SVPIIT Time Table Committee Report")
doc.build(story)
print(f"wrote {OUT} ({os.path.getsize(OUT)/1024:.1f} KB)")