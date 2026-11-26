"""Tests for the docs architecture diagram generator.

The generator writes a light and a dark diagram for the docs site. The
dark one must use the dark palette and each app's dark-theme icon where it
has one. The generator needs Pillow (it inlines the PNG logo), which is not
a runtime dependency of the app, so these tests are skipped when Pillow is
not installed.
"""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("PIL", reason="gen_architecture_diagram.py needs Pillow")

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "gen_architecture_diagram.py"


def _load_diagram_module():
    spec = importlib.util.spec_from_file_location("gen_architecture_diagram", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


diagram = _load_diagram_module()


def _inner_svg(path):
    """The raw inner markup of an SVG icon, as the generator inlines it."""
    wrapped = diagram.inner_svg(path, 0, 0, 1)
    start = wrapped.index(">") + 1
    return wrapped[start:-len("</svg>")]


def test_light_and_dark_diagrams_differ():
    light = diagram.build(diagram.COLOR, diagram.CATEGORIES)
    dark = diagram.build(diagram.COLOR_DARK, diagram.CATEGORIES_DARK)
    assert light != dark


def test_dark_diagram_uses_dark_palette():
    dark = diagram.build(diagram.COLOR_DARK, diagram.CATEGORIES_DARK)
    assert f'fill="{diagram.COLOR_DARK["bg"]}"' in dark
    assert f'fill="{diagram.COLOR_DARK["text"]}"' in dark
    assert f'fill="{diagram.COLOR["bg"]}"' not in dark
    assert f'fill="{diagram.COLOR["text"]}"' not in dark


def test_dark_categories_mirror_light_categories():
    light_labels = [label for label, _ in diagram.CATEGORIES]
    dark_labels = [label for label, _ in diagram.CATEGORIES_DARK]
    assert dark_labels == light_labels
    for (_, light_apps), (_, dark_apps) in zip(
        diagram.CATEGORIES, diagram.CATEGORIES_DARK
    ):
        light_names = [name for name, _, _ in light_apps]
        dark_names = [name for name, _, _ in dark_apps]
        assert dark_names == light_names
        for (_, light_path, _), (_, dark_path, _) in zip(light_apps, dark_apps):
            assert dark_path == diagram._DARK_ICON.get(light_path, light_path)


def test_dark_diagram_uses_dark_icons():
    light = diagram.build(diagram.COLOR, diagram.CATEGORIES)
    dark = diagram.build(diagram.COLOR_DARK, diagram.CATEGORIES_DARK)
    assert diagram._DARK_ICON, "expected at least one app with a dark icon"
    for light_path, dark_path in diagram._DARK_ICON.items():
        light_inner = _inner_svg(light_path)
        dark_inner = _inner_svg(dark_path)
        assert light_inner.strip()
        assert dark_inner.strip()
        assert light_inner in light
        assert light_inner not in dark
        assert dark_inner in dark
        assert dark_inner not in light


def test_generator_documents_both_output_files():
    assert "docs/architecture-diagram.png" in diagram.__doc__
    assert "docs/architecture-diagram-dark.png" in diagram.__doc__
