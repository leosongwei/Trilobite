#!/usr/bin/env python3
"""Regenerate the status favicons from the single SVG source.

Reads frontend/public/trilobite.svg, stamps the per-status badge circle
into a copy of the markup, rasterizes at 4x and downscales to 64x64.

Usage: uv run --with cairosvg --with Pillow frontend/scripts/gen-favicons.py
"""
import io
import pathlib

import cairosvg
from PIL import Image

PUBLIC = pathlib.Path(__file__).resolve().parent.parent / "public"

BADGE = (
    '<circle cx="51" cy="51" r="9.5" fill="{fill}" '
    'stroke="#1b1f23" stroke-width="1.5"/></svg>'
)
BADGE_COLORS = {
    "idle": "#6e7681",
    "running": "#3fb950",
    "pending": "#79b8ff",
}

svg = (PUBLIC / "trilobite.svg").read_text()
base = svg.replace("</svg>", "")
for status, color in BADGE_COLORS.items():
    marked = base + BADGE.format(fill=color)
    png = cairosvg.svg2png(bytestring=marked.encode(), output_width=256, output_height=256)
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    image.resize((64, 64), Image.LANCZOS).save(PUBLIC / f"favicon-{status}.png")
    print(f"favicon-{status}.png regenerated")
