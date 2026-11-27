#!/usr/bin/env python3
"""Regenerates docs/architecture-diagram.png and
docs/architecture-diagram-dark.png, the docs site's architecture reference
diagram (apps -> Backuparr -> destinations) in its light and dark themes
(#272). It lives with the docs, so the site shows the diagram of the branch
it's published from.

The apps come from config_store: a box per Settings category, holding the
icons of its apps (#163), so a new app only adds an icon to its box. Re-run
this after adding/removing an app or destination, or after any of the
icon/logo assets it embeds change.

The two differ only in their colours, the web UI's for each theme, and in
the icons: the dark one uses an app's dark-theme icon (APP_META icon_dark)
and turns the one-colour black ones white, as the web UI does. Both come
from webui/static/style.css, so the diagram follows the web UI. It also
writes the diagrams' size into docs/architecture.md, so the page keeps
their space while they load (tests/test_architecture_diagram.py checks it).

The diagram is drawn as an SVG, then rendered to a PNG at twice its size by
Chromium. A PNG shows the same everywhere: GitHub serves an SVG opened on
its own with a policy that blocks the raster icons inside it (Backuparr's
logo, Tdarr, LazyLibrarian).

Run it with Backuparr's requirements installed, plus Pillow and Playwright
(`pip install pillow playwright`), which aren't runtime dependencies of the
app itself, and a Chromium: Playwright's (`playwright install chromium`), or
a system one named in CHROMIUM (e.g. CHROMIUM=/usr/bin/chromium).
"""
import base64
import io
import os
import re
import sys

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from config_store import APP_CATEGORIES, APP_META, SELF_APP  # noqa: E402
ICONS = os.path.join(REPO, "webui", "static", "icons")
STATIC = os.path.join(REPO, "webui", "static")
DOCS = os.path.join(REPO, "docs")


def _style_css():
    """The web UI's style.css, without its comments."""
    with open(os.path.join(STATIC, "style.css")) as f:
        return re.sub(r"/\*.*?\*/", "", f.read(), flags=re.S)


def _tokens(css, selector):
    """The colours style.css sets for one theme: the custom properties in the
    first block for selector."""
    m = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", css)
    if not m:
        raise SystemExit(f"style.css has no {selector} block - update _tokens()")
    return {k: v.strip() for k, v in re.findall(r"(--[\w-]+):\s*([^;]+);", m.group(1))}


def _colors(tokens, own):
    """A theme's colours: the web UI's, plus the diagram's own (the
    Backuparr box's tint and the shadow, which style.css has no colour for)."""
    try:
        web = {"bg": tokens["--bg"], "card": tokens["--bg-elevated"], "border": tokens["--border"],
               "text": tokens["--text"], "muted": tokens["--text-muted"], "accent": tokens["--accent"],
               "border_hub": tokens["--accent"]}
    except KeyError as e:
        raise SystemExit(f"style.css has no {e} - update _colors()") from None
    return {**web, **own}


def _inverted_in_dark(css):
    """Black, one-colour icons, which the web UI's dark theme turns white
    with a filter: invert() rule in style.css: white in the dark diagram
    too. The device's dark theme and the toggle's each have such rules; they
    must name the same icons."""
    def icons(theme_selector):
        names = set()
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if theme_selector in selectors and re.search(r"filter:[^;]*invert\(", body):
                names |= set(re.findall(r'img\[src\$="/([^"/]+)"\]', selectors))
        return names

    by_device = icons(':root:not([data-theme="light"])')
    by_toggle = icons(':root[data-theme="dark"]')
    if not by_toggle or by_device != by_toggle:
        raise SystemExit(f"style.css's dark-theme invert rules don't match (device {sorted(by_device)}, "
                         f"toggle {sorted(by_toggle)}) - update _inverted_in_dark()")
    return by_toggle


STYLE_CSS = _style_css()

# Each theme's file name and colours.
THEMES = {
    "light": ("architecture-diagram.png", _colors(_tokens(STYLE_CSS, ":root"), {
        "card_hub": "#eef2ff",
        "shadow": "#10182b",
        "shadow_opacity": 0.10,
    })),
    "dark": ("architecture-diagram-dark.png", _colors(_tokens(STYLE_CSS, ':root[data-theme="dark"]'), {
        "card_hub": "#1a2236",
        "shadow": "#000000",
        "shadow_opacity": 0.35,
    })),
}

INVERT_IN_DARK = _inverted_in_dark(STYLE_CSS)

W = 1200
# Space reserved above/below content for the caption and bottom margin,
# kept separate from centering math so a longer SOURCES list doesn't
# collide with the caption.
CONTENT_TOP = 90
BOTTOM_MARGIN = 40


def _kind(icon_file):
    return "png" if icon_file.endswith(".png") else "svg"


def _icon_path(icon_file):
    """App icons are in icons/; Backuparr's own logo is in static/."""
    in_icons = os.path.join(ICONS, icon_file)
    return in_icons if os.path.exists(in_icons) else os.path.join(STATIC, icon_file)


# (category label, [(app label, icon file, dark-theme icon file)]) in
# Settings' order, with Backuparr's own backup first in its category as on
# the Settings tab; a category with no apps yet isn't drawn.
CATEGORIES = [
    (c["label"], [(m["label"], m["icon"], m.get("icon_dark") or m["icon"])
                  for m in ([SELF_APP] if SELF_APP["category"] == c["id"] else []) + APP_META
                  if m["category"] == c["id"]])
    for c in APP_CATEGORIES
]
CATEGORIES = [(label, apps) for label, apps in CATEGORIES if apps]

DESTS = [
    ("Local storage", None, "local", "zero setup"),
    ("Google Drive", os.path.join(ICONS, "google-drive.svg"), "svg", "OAuth connect"),
    ("Microsoft OneDrive", os.path.join(ICONS, "microsoft-onedrive.svg"), "svg", "rclone authorize"),
    ("Dropbox", os.path.join(ICONS, "dropbox.svg"), "svg", "rclone authorize"),
]

# A category's box: its name, then its apps' icons in rows of ICONS_PER_ROW.
BOX_W, BOX_GAP, BOX_PAD, BOX_TITLE_H = 324, 16, 18, 40
ICON_SIZE, ICON_GAP, ICONS_PER_ROW = 38, 12, 6
DEST_W, DEST_H, DEST_GAP, DEST_ICON = 250, 90, 42, 38
HUB_W, HUB_H = 240, 220
LEFT_X = 40
HUB_X = (W - HUB_W) // 2
RIGHT_X = W - 40 - DEST_W

# Standard Material "folder" glyph - stands in for Local storage, which has
# no brand icon of its own the way the other destinations do.
FOLDER_PATH = "M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"


def inner_svg(path, x, y, size, invert=False):
    """Strips the outer <svg ...>...</svg> wrapper from an icon file and
    re-wraps its inner content in a fresh nested <svg> positioned/sized for
    the diagram - avoids id/class collisions between icons entirely (each
    icon file already namespaces its own ids/classes by filename)."""
    with open(path) as f:
        content = f.read()
    inner = re.search(r"<svg[^>]*>(.*)</svg>\s*$", content, re.DOTALL).group(1)
    style = ' style="filter:invert(1)"' if invert else ""
    return f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 512 512"{style}>{inner}</svg>'


def image_tag(path, x, y, size, max_px):
    """Downscales a raster asset and inlines it as a data: URI, so the
    diagram is a single self-contained file with no external references."""
    img = Image.open(path).convert("RGBA")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return f'<image x="{x}" y="{y}" width="{size}" height="{size}" href="{uri}" preserveAspectRatio="xMidYMid meet"/>'


def icon(path, kind, x, y, size, color, invert=False, max_px=128):
    if kind == "svg":
        return inner_svg(path, x, y, size, invert)
    if kind == "png":
        return image_tag(path, x, y, size, max_px)
    if kind == "local":
        scale = size / 24
        return f'<g transform="translate({x},{y}) scale({scale})"><path d="{FOLDER_PATH}" fill="{color["accent"]}"/></g>'
    raise ValueError(kind)


def box_height(apps):
    rows = -(-len(apps) // ICONS_PER_ROW)
    return BOX_TITLE_H + rows * ICON_SIZE + (rows - 1) * ICON_GAP + BOX_PAD


def build(theme):
    """The diagram's SVG in the "light" or "dark" theme."""
    color = THEMES[theme][1]
    dark = theme == "dark"
    src_block_h = sum(box_height(apps) for _, apps in CATEGORIES) + (len(CATEGORIES) - 1) * BOX_GAP
    dest_block_h = len(DESTS) * DEST_H + (len(DESTS) - 1) * DEST_GAP
    # All three columns share one vertical center line, sized to the tallest.
    content_h = max(src_block_h, dest_block_h, HUB_H)
    H = CONTENT_TOP + content_h + BOTTOM_MARGIN

    def centered_start(block_h):
        return CONTENT_TOP + (content_h - block_h) // 2

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'''font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">''',
        f'''<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="{color['accent']}"/>
  </marker>
  <filter id="shadow" x="-30%" y="-30%" width="160%" height="160%">
    <feDropShadow dx="0" dy="1" stdDeviation="2.2" flood-color="{color['shadow']}" flood-opacity="{color['shadow_opacity']}"/>
  </filter>
</defs>''',
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="{color["bg"]}"/>',
        f'<text x="40" y="42" font-size="13" letter-spacing="1.5" font-weight="600" fill="{color["muted"]}">BACKUPARR &#8212; ARCHITECTURE</text>',
        f'<text x="40" y="64" font-size="13" fill="{color["muted"]}">Each app\'s own backup in - over its API or web UI, or from its mounted folder - and rclone out.</text>',
    ]

    y = centered_start(src_block_h)
    src_anchor = []
    for label, apps in CATEGORIES:
        h = box_height(apps)
        parts.append(f'<rect x="{LEFT_X}" y="{y}" width="{BOX_W}" height="{h}" rx="12" fill="{color["card"]}" stroke="{color["border"]}" stroke-width="1.4" filter="url(#shadow)"/>')
        parts.append(f'<text x="{LEFT_X + BOX_PAD}" y="{y + 27}" font-size="15" font-weight="600" fill="{color["text"]}">{label}</text>')
        for i, (app_label, icon_light, icon_dark) in enumerate(apps):
            row, col = divmod(i, ICONS_PER_ROW)
            ix = LEFT_X + BOX_PAD + col * (ICON_SIZE + ICON_GAP)
            iy = y + BOX_TITLE_H + row * (ICON_SIZE + ICON_GAP)
            icon_file = icon_dark if dark else icon_light
            img = icon(_icon_path(icon_file), _kind(icon_file), ix, iy, ICON_SIZE, color,
                       invert=dark and icon_file in INVERT_IN_DARK)
            parts.append(f'<g><title>{app_label}</title>{img}</g>')
        src_anchor.append((LEFT_X + BOX_W, y + h / 2))
        y += h + BOX_GAP

    hub_y = centered_start(HUB_H)
    parts.append(f'<rect x="{HUB_X}" y="{hub_y}" width="{HUB_W}" height="{HUB_H}" rx="16" fill="{color["card_hub"]}" stroke="{color["border_hub"]}" stroke-width="2.2" filter="url(#shadow)"/>')
    logo_size = 76
    logo_x, logo_y = HUB_X + (HUB_W - logo_size) / 2, hub_y + 26
    parts.append(image_tag(os.path.join(STATIC, "logo.png"), logo_x, logo_y, logo_size, 256))
    parts.append(f'<text x="{HUB_X + HUB_W / 2}" y="{logo_y + logo_size + 34}" font-size="22" font-weight="700" text-anchor="middle" fill="{color["text"]}">Backuparr</text>')
    ty = logo_y + logo_size + 56
    for line in ("Backs up each app, checks", "and encrypts it, uploads it", "with rclone, prunes old ones"):
        parts.append(f'<text x="{HUB_X + HUB_W / 2}" y="{ty}" font-size="13" text-anchor="middle" fill="{color["muted"]}">{line}</text>')
        ty += 17
    hub_in = (HUB_X, hub_y + HUB_H / 2)
    hub_out = (HUB_X + HUB_W, hub_y + HUB_H / 2)

    dest_start_y = centered_start(dest_block_h)
    dest_anchor = []
    for i, (label, path, kind, sub) in enumerate(DESTS):
        y = dest_start_y + i * (DEST_H + DEST_GAP)
        parts.append(f'<rect x="{RIGHT_X}" y="{y}" width="{DEST_W}" height="{DEST_H}" rx="12" fill="{color["card"]}" stroke="{color["border"]}" stroke-width="1.4" filter="url(#shadow)"/>')
        ix, iy = RIGHT_X + 18, y + (DEST_H - DEST_ICON) / 2
        parts.append(icon(path, kind, ix, iy, DEST_ICON, color))
        parts.append(f'<text x="{ix + DEST_ICON + 16}" y="{y + DEST_H / 2 - 2}" font-size="16" font-weight="600" fill="{color["text"]}">{label}</text>')
        parts.append(f'<text x="{ix + DEST_ICON + 16}" y="{y + DEST_H / 2 + 18}" font-size="12.5" fill="{color["muted"]}">{sub}</text>')
        dest_anchor.append((RIGHT_X, y + DEST_H / 2))

    # Converging lines share one arrowhead at the merge point instead of each
    # carrying its own marker-end; lines stop short of hub_in for the gap.
    for ax, ay in src_anchor:
        bx, by = hub_in
        c1x, c2x = ax + (bx - ax) * 0.55, ax + (bx - ax) * 0.85
        parts.append(f'<path d="M{ax},{ay} C{c1x},{ay} {c2x},{by} {bx - 14},{by}" fill="none" stroke="{color["accent"]}" stroke-width="2.2" stroke-opacity="0.55"/>')
    hx, hy = hub_in
    parts.append(f'<path d="M{hx - 14},{hy - 7} L{hx},{hy} L{hx - 14},{hy + 7} Z" fill="{color["accent"]}"/>')

    for ax, ay in dest_anchor:
        bx, by = hub_out
        c1x, c2x = bx + (ax - bx) * 0.15, bx + (ax - bx) * 0.5
        parts.append(f'<path d="M{bx},{by} C{c1x},{by} {c2x},{ay} {ax - 6},{ay}" fill="none" stroke="{color["accent"]}" stroke-width="2.4" marker-end="url(#arrow)"/>')

    parts.append("</svg>")
    return "\n".join(parts)


def write_size(md_path, name, width, height):
    """Writes a diagram's size (in CSS pixels) into its image's attributes in
    architecture.md, so the page keeps its space while the lazy image loads."""
    with open(md_path) as f:
        md = f.read()
    pattern = r"(\{\{ '/" + re.escape(name) + r"' \| relative_url \}\}\)\{:[^}]*?)width=\"\d+\" height=\"\d+\""
    md, n = re.subn(pattern, rf'\g<1>width="{width}" height="{height}"', md)
    if n != 1:
        raise SystemExit(f"{md_path}: no width/height for {name} - update write_size()")
    with open(md_path, "w") as f:
        f.write(md)


def render_png(svg, out_path):
    """Renders the SVG with Chromium at 2x, for sharp text on high-DPI screens."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
        page = browser.new_page(viewport={"width": W, "height": 100}, device_scale_factor=2)
        page.set_content(f'<html><body style="margin:0">{svg}</body></html>')
        page.locator("svg").first.screenshot(path=out_path)
        browser.close()


if __name__ == "__main__":
    md_path = os.path.join(DOCS, "architecture.md")
    for theme, (name, _) in THEMES.items():
        out_path = os.path.join(DOCS, name)
        render_png(build(theme), out_path)
        width, height = Image.open(out_path).size
        write_size(md_path, name, width // 2, height // 2)
        print("wrote", out_path, f"({width // 2}x{height // 2})")
