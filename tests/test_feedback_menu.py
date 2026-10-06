import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / ".github" / "ISSUE_TEMPLATE"
REPO_PREFIX = "https://github.com/rsaturns/backuparr/issues/new"


@pytest.fixture
def index_html(isolated_webui, authed_client):
    return authed_client.get("/").get_data(as_text=True)


def feedback_links(html):
    menu = re.search(r'id="feedback-menu".*?</div>', html, re.S).group(0)
    return re.findall(r'<a [^>]*href="([^"]+)"[^>]*>([^<]+)</a>', menu, re.S)


def test_menu_offers_bug_report_and_feature_request(index_html):
    labels = [label.strip() for _href, label in feedback_links(index_html)]
    assert labels == ["Report a bug", "Request a feature"]


def test_links_point_at_templates_that_exist(index_html):
    links = feedback_links(index_html)
    assert len(links) == 2
    for href, _label in links:
        assert href.startswith(REPO_PREFIX)
        template = parse_qs(urlparse(href).query)["template"][0]
        assert (TEMPLATES / template).is_file()


def test_links_open_safely_in_a_new_tab(index_html):
    menu = re.search(r'id="feedback-menu".*?</div>', index_html, re.S).group(0)
    assert menu.count('target="_blank"') == 2
    assert menu.count('rel="noopener noreferrer"') == 2
