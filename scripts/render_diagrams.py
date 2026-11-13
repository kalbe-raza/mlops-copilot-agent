"""Render the Mermaid diagrams in architecture.md to PNG files in docs/img/ (needs Playwright + network).

    uv run --with playwright python scripts/render_diagrams.py
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
NAMES = ["architecture-components", "request-sequence", "training-dag", "provenance-chain"]
PAGE = """<html><head><script src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script></head>
<body style="margin:0;background:#fff"><div id="d" style="display:inline-block;padding:16px"></div><script>
mermaid.initialize({startOnLoad:false, theme:'default', flowchart:{htmlLabels:true}, sequence:{useMaxWidth:false}});
mermaid.render('g', %s).then(r => { document.getElementById('d').innerHTML = r.svg; window.done = true; })
  .catch(e => { document.body.innerText = 'ERR ' + e; window.done = true; });
</script></body></html>"""


async def main() -> None:
    blocks = re.findall(r"```mermaid\n(.*?)```", (ROOT / "architecture.md").read_text(), re.S)
    out = ROOT / "docs" / "img"
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path="/opt/google/chrome/chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1800, "height": 1200}, device_scale_factor=2)
        for name, src in zip(NAMES, blocks, strict=True):
            await page.set_content(PAGE % json.dumps(src))
            await page.wait_for_function("window.done === true", timeout=30000)
            text = await page.inner_text("body")
            if text.startswith("ERR"):
                raise SystemExit(f"{name}: {text}")
            await page.locator("#d").screenshot(path=str(out / f"{name}.png"))
            print("rendered", name)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
