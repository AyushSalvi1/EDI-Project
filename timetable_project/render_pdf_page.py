"""Render a PDF page to PNG so the layout can be inspected visually."""
import sys

import pypdfium2 as pdfium

src = sys.argv[1]
out = sys.argv[2]
page_number = int(sys.argv[3]) if len(sys.argv) > 3 else 0
scale = float(sys.argv[4]) if len(sys.argv) > 4 else 1.4

pdf = pdfium.PdfDocument(src)
page = pdf[page_number]
bitmap = page.render(scale=scale)
bitmap.to_pil().save(out)
print(f"rendered {src} page {page_number + 1}/{len(pdf)} -> {out}")