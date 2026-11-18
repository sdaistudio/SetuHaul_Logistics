"""Render the v2 Word hand-ins to HTML fragments (needs pandoc; run locally).

    python tools/render_v2.py

Writes v2/html/<name>.html (body fragments) and v2/html/media/<name>/...
build_site.py wraps them in the site layout, so CI does not need pandoc.
Re-run after editing any file in v2/deliverables/.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "v2" / "deliverables"
OUT = ROOT / "v2" / "html"


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "media").mkdir(parents=True)
    for docx in sorted(SRC.glob("0*.docx")):
        name = docx.stem
        media = OUT / "media" / name
        h = subprocess.run(["pandoc", str(docx), "-t", "html5", "--extract-media", str(media)],
                           capture_output=True, text=True, check=True).stdout
        h = h.replace(str(media) + "/media/", f"media/{name}/media/").replace(str(media) + "/", f"media/{name}/")
        h = re.sub(r'<img ([^>]*?)style="[^"]*"', r"<img \1", h)  # let CSS size the images
        (OUT / f"{name}.html").write_text(h, encoding="utf-8")
        print("rendered", name, len(h) // 1024, "KB")


if __name__ == "__main__":
    main()
