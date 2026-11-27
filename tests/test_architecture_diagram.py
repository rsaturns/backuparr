"""The architecture diagram's size on its docs page (#272): each diagram's
image in docs/architecture.md carries the PNG's size, so the page keeps its
space while the lazy image loads. scripts/gen_architecture_diagram.py
renders the PNGs at twice their size and writes it there; this catches a
diagram regenerated or replaced without that step."""
import os
import re
import struct

DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
DIAGRAMS = ("architecture-diagram.png", "architecture-diagram-dark.png")


def png_size(path):
    """A PNG's width and height in pixels, from its IHDR chunk."""
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR", path
    return struct.unpack(">II", head[16:24])


def test_architecture_page_has_each_diagrams_size():
    with open(os.path.join(DOCS, "architecture.md")) as f:
        md = f.read()
    for name in DIAGRAMS:
        m = re.search(r"\{\{ '/" + re.escape(name) + r"' \| relative_url \}\}\)\{:[^}]*width=\"(\d+)\" height=\"(\d+)\"", md)
        assert m, f"architecture.md has no width/height for {name}"
        width, height = png_size(os.path.join(DOCS, name))
        assert (int(m.group(1)) * 2, int(m.group(2)) * 2) == (width, height), (
            f"{name} is {width}x{height}, so architecture.md needs {width // 2}x{height // 2}: "
            "run scripts/gen_architecture_diagram.py")


def test_both_diagrams_are_the_same_size():
    assert png_size(os.path.join(DOCS, DIAGRAMS[0])) == png_size(os.path.join(DOCS, DIAGRAMS[1]))
