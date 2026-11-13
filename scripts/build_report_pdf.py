"""Build docs/REPORT.pdf from docs/REPORT.md (Markdown -> HTML -> headless Chrome PDF).

    uv run --with playwright --with markdown python scripts/build_report_pdf.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import markdown
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

CSS = """
@page { size: A4; margin: 16mm 14mm 18mm 14mm; }
body { font-family: 'DejaVu Sans', Arial, sans-serif; font-size: 9.6pt; line-height: 1.42; color: #1f2328; }
h1 { font-size: 19pt; color: #0b3d91; border-bottom: 2px solid #0b3d91; padding-bottom: 4px; margin-top: 0; }
h2 { font-size: 13.5pt; color: #0b3d91; margin-top: 18px; border-bottom: 1px solid #d0d7de; padding-bottom: 2px;
     page-break-after: avoid; }
h3 { font-size: 11pt; color: #24292f; margin-top: 14px; page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 10px; font-size: 8.4pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #d0d7de; padding: 3px 5px; vertical-align: top; text-align: left; }
th { background: #eef3fb; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 8.2pt; background: #f3f5f7; padding: 0 2px;
       border-radius: 3px; word-break: break-word; }
pre { background: #f6f8fa; border: 1px solid #d0d7de; padding: 7px; font-size: 7.6pt; white-space: pre-wrap;
      page-break-inside: avoid; }
pre code { background: none; padding: 0; }
img { max-width: 100%; display: block; margin: 8px auto; border: 1px solid #d0d7de; page-break-inside: avoid; }
img[alt="Component architecture"] { max-height: 205mm; width: auto; }
img[alt="Request sequence"] { max-height: 150mm; width: auto; }
ul, ol { margin: 4px 0 6px 18px; padding: 0; }
a { color: #0b5cd5; text-decoration: none; }
"""


async def main() -> None:
    md = (DOCS / "REPORT.md").read_text()
    body = markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])
    html = f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>"
    tmp = DOCS / ".report.html"
    tmp.write_text(html)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path="/opt/google/chrome/chrome", headless=True)
            page = await browser.new_page()
            await page.goto(tmp.as_uri(), wait_until="load")
            await page.pdf(
                path=str(DOCS / "REPORT.pdf"), format="A4", print_background=True,
                display_header_footer=True,
                header_template="<div style='font-size:7px;width:100%;text-align:right;padding-right:14mm;color:#888'>"
                                "MLOps Assignment 03 - Syed Kalbe Raza (22i-0794)</div>",
                footer_template="<div style='font-size:7px;width:100%;text-align:center;color:#888'>"
                                "<span class='pageNumber'></span> / <span class='totalPages'></span></div>",
                margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"},
            )
            await browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    print("wrote", DOCS / "REPORT.pdf")


if __name__ == "__main__":
    asyncio.run(main())
