#!/usr/bin/env python3
"""Regenerates webui/static/architecture-diagram.svg, the README's
architecture reference diagram (services -> Backuparr -> destinations).
Re-run this after adding/removing an app or destination, or after any of
the icon/logo assets it embeds change. Requires Pillow (`pip install
pillow`); not a runtime dependency of the app itself.
"""
import base64
import io
import os
import re

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(REPO, "webui", "static", "icons")
STATIC = os.path.join(REPO, "webui", "static")

COLOR = {
    "bg": "#f5f6f8",
    "card": "#ffffff",
    "card_hub": "#eef2ff",
    "border": "#e2e5ea",
    "border_hub": "#3b6bf6",
    "text": "#1a1d23",
    "muted": "#6b7280",
    "accent": "#3b6bf6",
}

W = 1200
# Vertical space reserved above the content for the caption, and below it
# as a bottom margin - kept separate from centering math (rather than
# just centering across the full canvas) so a longer SOURCES list doesn't
# creep up and collide with the caption text.
CONTENT_TOP = 90
BOTTOM_MARGIN = 40

SOURCES = [
    ("Radarr", os.path.join(ICONS, "radarr.svg"), "svg"),
    ("Sonarr", os.path.join(ICONS, "sonarr.svg"), "svg"),
    ("Prowlarr", os.path.join(ICONS, "prowlarr.svg"), "svg"),
    ("Profilarr", os.path.join(ICONS, "profilarr.svg"), "svg"),
    ("Bazarr", os.path.join(ICONS, "bazarr.svg"), "svg"),
    ("Tdarr", os.path.join(ICONS, "tdarr.png"), "png"),
    ("SABnzbd", os.path.join(ICONS, "sabnzbd.svg"), "svg"),
    ("Tautulli", os.path.join(ICONS, "tautulli.svg"), "svg"),
]

DESTS = [
    ("Local storage", None, "local", "zero setup"),
    ("Google Drive", os.path.join(ICONS, "google-drive.svg"), "svg", "OAuth connect"),
    ("Microsoft OneDrive", os.path.join(ICONS, "microsoft-onedrive.svg"), "svg", "rclone authorize"),
]

CARD_W, CARD_H, CARD_GAP, ICON_SIZE = 230, 60, 16, 32
DEST_W, DEST_H, DEST_GAP, DEST_ICON = 250, 90, 42, 38
HUB_W, HUB_H = 240, 220
LEFT_X = 40
HUB_X = (W - HUB_W) // 2
RIGHT_X = W - 40 - DEST_W

# Standard Material "folder" glyph - stands in for Local storage, which has
# no brand icon of its own the way the other destinations do.
FOLDER_PATH = "M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"


def inner_svg(path, x, y, size):
    """Strips the outer <svg ...>...</svg> wrapper from an icon file and
    re-wraps its inner content in a fresh nested <svg> positioned/sized for
    the diagram - avoids id/class collisions between icons entirely (each
    icon file already namespaces its own ids/classes by filename)."""
    with open(path) as f:
        content = f.read()
    inner = re.search(r"<svg[^>]*>(.*)</svg>\s*$", content, re.DOTALL).group(1)
    return f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 512 512">{inner}</svg>'


def image_tag(path, x, y, size, max_px):
    """Downscales a raster asset and inlines it as a data: URI, so the
    diagram is a single self-contained file with no external references."""
    img = Image.open(path).convert("RGBA")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return f'<image x="{x}" y="{y}" width="{size}" height="{size}" href="{uri}" preserveAspectRatio="xMidYMid meet"/>'


def icon(path, kind, x, y, size, max_px=128):
    if kind == "svg":
        return inner_svg(path, x, y, size)
    if kind == "png":
        return image_tag(path, x, y, size, max_px)
    if kind == "local":
        scale = size / 24
        return f'<g transform="translate({x},{y}) scale({scale})"><path d="{FOLDER_PATH}" fill="{COLOR["accent"]}"/></g>'
    raise ValueError(kind)


def build():
    src_block_h = len(SOURCES) * CARD_H + (len(SOURCES) - 1) * CARD_GAP
    dest_block_h = len(DESTS) * DEST_H + (len(DESTS) - 1) * DEST_GAP
    # All three columns share one common vertical center line (content_h/2
    # below CONTENT_TOP), sized to whichever column is tallest - currently
    # always the source list, but this stays correct if that changes.
    content_h = max(src_block_h, dest_block_h, HUB_H)
    H = CONTENT_TOP + content_h + BOTTOM_MARGIN

    def centered_start(block_h):
        return CONTENT_TOP + (content_h - block_h) // 2

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'''font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">''',
        f'''<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="{COLOR['accent']}"/>
  </marker>
  <filter id="shadow" x="-30%" y="-30%" width="160%" height="160%">
    <feDropShadow dx="0" dy="1" stdDeviation="2.2" flood-color="#10182b" flood-opacity="0.10"/>
  </filter>
</defs>''',
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="{COLOR["bg"]}"/>',
        f'<text x="40" y="42" font-size="13" letter-spacing="1.5" font-weight="600" fill="{COLOR["muted"]}">BACKUPARR &#8212; ARCHITECTURE</text>',
        f'<text x="40" y="64" font-size="13" fill="{COLOR["muted"]}">Each app\'s own backup API in, rclone out - config files are never read directly.</text>',
    ]

    src_start_y = centered_start(src_block_h)
    src_anchor = []
    for i, (label, path, kind) in enumerate(SOURCES):
        y = src_start_y + i * (CARD_H + CARD_GAP)
        parts.append(f'<rect x="{LEFT_X}" y="{y}" width="{CARD_W}" height="{CARD_H}" rx="12" fill="{COLOR["card"]}" stroke="{COLOR["border"]}" stroke-width="1.4" filter="url(#shadow)"/>')
        ix, iy = LEFT_X + 16, y + (CARD_H - ICON_SIZE) / 2
        parts.append(icon(path, kind, ix, iy, ICON_SIZE))
        parts.append(f'<text x="{ix + ICON_SIZE + 14}" y="{y + CARD_H / 2 + 5}" font-size="16" font-weight="600" fill="{COLOR["text"]}">{label}</text>')
        src_anchor.append((LEFT_X + CARD_W, y + CARD_H / 2))

    hub_y = centered_start(HUB_H)
    parts.append(f'<rect x="{HUB_X}" y="{hub_y}" width="{HUB_W}" height="{HUB_H}" rx="16" fill="{COLOR["card_hub"]}" stroke="{COLOR["border_hub"]}" stroke-width="2.2" filter="url(#shadow)"/>')
    logo_size = 76
    logo_x, logo_y = HUB_X + (HUB_W - logo_size) / 2, hub_y + 26
    parts.append(image_tag(os.path.join(STATIC, "logo.png"), logo_x, logo_y, logo_size, 256))
    parts.append(f'<text x="{HUB_X + HUB_W / 2}" y="{logo_y + logo_size + 34}" font-size="22" font-weight="700" text-anchor="middle" fill="{COLOR["text"]}">Backuparr</text>')
    ty = logo_y + logo_size + 56
    for line in ("Triggers each app's backup,", "uploads it via rclone, prunes", "old backups on schedule"):
        parts.append(f'<text x="{HUB_X + HUB_W / 2}" y="{ty}" font-size="13" text-anchor="middle" fill="{COLOR["muted"]}">{line}</text>')
        ty += 17
    hub_in = (HUB_X, hub_y + HUB_H / 2)
    hub_out = (HUB_X + HUB_W, hub_y + HUB_H / 2)

    dest_start_y = centered_start(dest_block_h)
    dest_anchor = []
    for i, (label, path, kind, sub) in enumerate(DESTS):
        y = dest_start_y + i * (DEST_H + DEST_GAP)
        parts.append(f'<rect x="{RIGHT_X}" y="{y}" width="{DEST_W}" height="{DEST_H}" rx="12" fill="{COLOR["card"]}" stroke="{COLOR["border"]}" stroke-width="1.4" filter="url(#shadow)"/>')
        ix, iy = RIGHT_X + 18, y + (DEST_H - DEST_ICON) / 2
        parts.append(icon(path, kind, ix, iy, DEST_ICON))
        parts.append(f'<text x="{ix + DEST_ICON + 16}" y="{y + DEST_H / 2 - 2}" font-size="16" font-weight="600" fill="{COLOR["text"]}">{label}</text>')
        parts.append(f'<text x="{ix + DEST_ICON + 16}" y="{y + DEST_H / 2 + 18}" font-size="12.5" fill="{COLOR["muted"]}">{sub}</text>')
        dest_anchor.append((RIGHT_X, y + DEST_H / 2))

    # Converging source lines share one unified arrowhead at the merge
    # point instead of each carrying its own marker-end - six overlapping
    # arrowheads at slightly different angles reads as a jagged mess, not
    # an arrow. The lines stop short of hub_in; a single triangle fills
    # the gap.
    for ax, ay in src_anchor:
        bx, by = hub_in
        c1x, c2x = ax + (bx - ax) * 0.55, ax + (bx - ax) * 0.85
        parts.append(f'<path d="M{ax},{ay} C{c1x},{ay} {c2x},{by} {bx - 14},{by}" fill="none" stroke="{COLOR["accent"]}" stroke-width="2.2" stroke-opacity="0.55"/>')
    hx, hy = hub_in
    parts.append(f'<path d="M{hx - 14},{hy - 7} L{hx},{hy} L{hx - 14},{hy + 7} Z" fill="{COLOR["accent"]}"/>')

    for ax, ay in dest_anchor:
        bx, by = hub_out
        c1x, c2x = bx + (ax - bx) * 0.15, bx + (ax - bx) * 0.5
        parts.append(f'<path d="M{bx},{by} C{c1x},{by} {c2x},{ay} {ax - 6},{ay}" fill="none" stroke="{COLOR["accent"]}" stroke-width="2.4" marker-end="url(#arrow)"/>')

    parts.append("</svg>")
    return "\n".join(parts)


if __name__ == "__main__":
    out_path = os.path.join(STATIC, "architecture-diagram.svg")
    with open(out_path, "w") as f:
        f.write(build())
    print("wrote", out_path)
