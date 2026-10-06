"""Structurally validate the generated timetable PDF layout using word coordinates."""
import sys
from collections import defaultdict

import pdfplumber

if len(sys.argv) < 2:
    print("Usage: python validate_pdf_layout.py <pdf_path>")
    sys.exit(1)

path = sys.argv[1]

with pdfplumber.open(path) as pdf:
    print(f"file: {path}\npages: {len(pdf.pages)}")
    for index, page in enumerate(pdf.pages):
        words = page.extract_words(use_text_flow=False)
        if not words:
            print(f"  page {index+1}: EMPTY")
            continue

        # Group words into rows by vertical position.
        rows = defaultdict(list)
        for w in words:
            rows[round(w["top"] / 6)].append(w)
        ordered = [sorted(v, key=lambda w: w["x0"]) for _, v in sorted(rows.items())]

        print(f"\n--- page {index+1} ({page.width:.0f}x{page.height:.0f}pt) ---")
        for r_i, row in enumerate(ordered[:4]):
            line = " | ".join(w["text"] for w in row)[:150]
            print(f"  row {r_i}: {line}")
        print(f"  ... {len(ordered)} text rows total")

        # Day header columns
        day_names = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
        header_row = next((r for r in ordered if any(w["text"] in day_names for w in r)), None)
        if header_row:
            xs = [round(w["x0"]) for w in header_row if w["text"] in day_names]
            print(f"  day header columns at x = {xs}")
            print(f"  day header spread = {max(xs)-min(xs) if len(xs)>1 else 0}pt")

        period_rows = [r for r in ordered if any(w["text"].startswith("Period ") for w in r)]
        print(f"  period rows detected: {len(period_rows)}")

        labs = sum(1 for w in words if "[LAB]" in w["text"])
        frees = sum(1 for w in words if w["text"] == "Free")
        subjects = sum(1 for w in words if w["text"] in {"Mathematics", "Physics", "Data", "Logic",
                                                        "Structures", "Programming"})
        print(f"  LAB badges={labs}  Free cells={frees}  subject words={subjects}")

        if index >= 2:
            print("  (stopping after first 3 pages)")
            break