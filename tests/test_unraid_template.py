"""The Unraid template must match the image it installs."""
import os
import re
import xml.etree.ElementTree as ET

import pytest

import backup

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


def test_port_matches_image(template):
    exposed = re.search(r"^EXPOSE (\d+)$", _read("Dockerfile"), re.M).group(1)
    (port,) = _configs(template, "Port")
    assert port.get("Target") == exposed
    assert f"[PORT:{exposed}]" in template.findtext("WebUI")


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
