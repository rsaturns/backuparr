"""The Unraid template must match the image it installs."""
import os
import re
import xml.etree.ElementTree as ET

import pytest

import backup
from scripts import sync_unraid_template as sync

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "templates", "backuparr.xml")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def template():
    return ET.parse(TEMPLATE).getroot()


def _configs(template, kind):
    return [c for c in template.findall("Config") if c.get("Type") == kind]


def test_template_url_points_at_this_file(template):
    assert template.findtext("TemplateURL") == (
        "https://raw.githubusercontent.com/rsaturns/backuparr/main/templates/backuparr.xml")


def test_ca_essentials_present(template):
    assert template.get("version") == "2"
    for tag in ("Name", "Repository", "Overview", "Category", "Icon", "WebUI", "Project", "Support"):
        assert (template.findtext(tag) or "").strip(), tag
    assert template.findtext("Repository").startswith("rsaturns/backuparr:")


def test_ports_match_image(template):
    exposed = re.search(r"^EXPOSE ([\d ]+)$", _read("Dockerfile"), re.M).group(1).split()
    assert sorted(port.get("Target") for port in _configs(template, "Port")) == sorted(exposed) == ["8990", "8991"]
    # The WebUI link opens HTTPS, which is on even with HTTPS only (#177).
    assert template.findtext("WebUI") == "https://[IP]:[PORT:8991]/"


def test_appdata_is_the_config_volume(template):
    appdata = next(c for c in _configs(template, "Path") if c.get("Name") == "Appdata")
    assert appdata.get("Target") == "/config/backuparr"
    assert appdata.get("Required") == "true"


def test_optional_app_folders_match_the_docs(template):
    docs = _read("docs", "getting-started", "docker.md")
    for path in _configs(template, "Path"):
        if path.get("Name") == "Appdata":
            continue
        assert path.get("Required") == "false" and path.get("Default") == ""
        assert f":{path.get('Target')}" in docs, path.get("Target")


def test_variables_are_documented(template):
    docs = _read("docs", "environment-variables.md")
    for var in _configs(template, "Variable"):
        assert f"`{var.get('Target')}`" in docs, var.get("Target")


def test_secrets_are_masked(template):
    masked = {c.get("Target") for c in template.findall("Config") if c.get("Mask") == "true"}
    assert masked == {"BACKUPARR_SECRETS_KEY", "RCLONE_CONFIG_PASS"}


def test_ca_profile_has_a_profile():
    profile = ET.parse(os.path.join(ROOT, "ca_profile.xml")).getroot()
    assert profile.tag == "CommunityApplications"
    assert (profile.findtext("Profile") or "").strip()


@pytest.mark.parametrize("value, level", [
    (None, "INFO"), ("", "INFO"), ("  ", "INFO"), ("debug", "DEBUG"),
    (" WARNING ", "WARNING"), ("verbose", "INFO"),
])
def test_log_level_tolerates_blank_and_unknown(value, level):
    assert backup._log_level(value) == level


# --- <Changes> and <Date> for Community Apps ---------------------------------

def test_template_shows_the_last_three_releases():
    """Fails until scripts/sync_unraid_template.py is run after a release's
    changelog is written."""
    with open(TEMPLATE, encoding="utf-8") as f:
        current = f.read()
    assert sync.render(current, _read("CHANGELOG.md")) == current, "run python scripts/sync_unraid_template.py"


def test_changes_and_date_parse_as_the_newest_releases(template):
    newest = sync.released_sections(_read("CHANGELOG.md"))[:3]
    changes = template.findtext("Changes")
    assert template.findtext("Date") == newest[0][1]
    assert [line for line in changes.splitlines() if line.startswith("### ")] == [f"### {v} ({d})" for v, d, _ in newest]
    assert changes.endswith("View all changes: https://github.com/rsaturns/backuparr/blob/main/CHANGELOG.md")


CHANGELOG_SAMPLE = """# Changelog

## [Unreleased]

### Added

- Not released yet.

## [3.0.0] - 2026-12-01

**A big release.**

### Added

- One **bold** thing with `code` & a <tag>.
- A wrapped entry
  that goes on.

### Fixed

- A fix.

## [2.9.0] - 2026-11-20

- Older.

## [2.8.0] - 2026-11-10

- Older still.

## [2.7.0] - 2026-11-01

- Too old to show.
"""


def test_only_released_versions_are_read_newest_first():
    sections = sync.released_sections(CHANGELOG_SAMPLE)
    assert [(v, d) for v, d, _ in sections] == [("3.0.0", "2026-12-01"), ("2.9.0", "2026-11-20"),
                                                ("2.8.0", "2026-11-10"), ("2.7.0", "2026-11-01")]
    assert sections[0][2] == ["One bold thing with code & a <tag>.", "A wrapped entry that goes on.", "A fix."]


def test_changes_hold_three_releases_and_a_link_to_the_rest():
    text = sync.changes_text(sync.released_sections(CHANGELOG_SAMPLE))
    assert text == (
        "### 3.0.0 (2026-12-01)\n- One bold thing with code & a <tag>.\n- A wrapped entry that goes on.\n- A fix.\n\n"
        "### 2.9.0 (2026-11-20)\n- Older.\n\n"
        "### 2.8.0 (2026-11-10)\n- Older still.\n\n"
        "View all changes: https://github.com/rsaturns/backuparr/blob/main/CHANGELOG.md")


def test_render_adds_the_tags_once_escaped_and_then_replaces_them():
    bare = '<Container version="2">\n  <Name>x</Name>\n  <Config Name="a"/>\n</Container>\n'
    once = sync.render(bare, CHANGELOG_SAMPLE)
    root = ET.fromstring(once)
    assert root.findtext("Date") == "2026-12-01" and "code & a <tag>" in root.findtext("Changes")
    assert once.index("<Changes>") < once.index("<Config")
    newer = CHANGELOG_SAMPLE.replace("## [Unreleased]", "## [Unreleased]\n\n## [3.0.1] - 2026-12-02\n\n- Patch.")
    twice = sync.render(once, newer)
    assert twice.count("<Changes>") == 1 and twice.count("<Date>") == 1
    assert ET.fromstring(twice).findtext("Date") == "2026-12-02"
