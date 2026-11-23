"""Setup help on every card and theme-aware icons (#199): each app card's "?"
opens its app's docs page at "Setting it up", the Backuparr menu links the
docs, and an app with a dark-theme icon shows both for CSS to pick from."""
import os
import re
from html.parser import HTMLParser

from config_store import APP_META, DOCS_URL, SELF_APP, SETUP_SECTION, docs_page

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
ICONS = os.path.join(ROOT, "webui", "static", "icons")


def kramdown_id(heading):
    """The id GitHub Pages' kramdown gives a heading: lower case, punctuation
    dropped, spaces as hyphens."""
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", heading.strip().lower()))


def heading_ids(path):
    with open(path) as f:
        return {kramdown_id(m.group(1)) for m in re.finditer(r"^#{1,4} (.+)$", f.read(), re.M)}


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current, self.in_head, self.help, self.icons, self.menu = None, False, {}, {}, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "app-card" in (a.get("class") or "").split():
            self.current = a.get("data-app") or a.get("data-self-app")
        if "app-card-head" in (a.get("class") or "").split():
            self.in_head = True  # a card's icons come before its "?"
        if tag == "a" and a.get("class") == "card-help":
            self.help[self.current] = a
            self.in_head = False
        if tag == "img" and "app-icon" in (a.get("class") or "") and self.in_head:
            self.icons.setdefault(self.current, []).append(a)
        if tag == "a" and a.get("class") == "feedback-link":
            self.menu.append(a)


def page(client):
    parser = Links()
    parser.feed(client.get("/").get_data(as_text=True))
    return parser


def test_every_app_links_a_docs_page_with_a_setting_it_up_section():
    for meta in APP_META:
        path = os.path.join(DOCS, "apps", f"{docs_page(meta['id'])}.md")
        assert os.path.exists(path), meta["id"]
        assert SETUP_SECTION in heading_ids(path), f"{path} has no 'Setting it up' section"
        assert meta["help_url"] == f"{DOCS_URL}apps/{docs_page(meta['id'])}/#{SETUP_SECTION}"


def test_radarr_sonarr_lidarr_and_prowlarr_share_a_page_and_whisparr_has_its_own():
    assert {docs_page(a) for a in ("radarr", "sonarr", "lidarr", "prowlarr")} == {"servarr"}
    assert docs_page("whisparr") == "whisparr" and docs_page("qnap") == "qnap"


def test_backuparrs_card_links_its_own_backups_section():
    page_name, anchor = SELF_APP["help_url"].removeprefix(DOCS_URL).split("/#")
    assert anchor in heading_ids(os.path.join(DOCS, f"{page_name}.md"))


def test_each_card_shows_its_help_link_opening_the_docs_in_a_new_tab(authed_client):
    parsed = page(authed_client)
    assert set(parsed.help) == {m["id"] for m in APP_META} | {SELF_APP["id"]}
    for app_id, link in parsed.help.items():
        meta = SELF_APP if app_id == SELF_APP["id"] else next(m for m in APP_META if m["id"] == app_id)
        assert link["href"] == meta["help_url"]
        assert link["target"] == "_blank" and link["rel"] == "noopener noreferrer"
        assert link["aria-label"] == f"{meta['label']} setup guide, in the docs"


def test_the_backuparr_menu_links_the_docs_first(authed_client):
    links = page(authed_client).menu
    assert [link["href"] for link in links][0] == DOCS_URL
    assert len(links) == 3 and all(link["target"] == "_blank" for link in links)


def test_a_dark_theme_icon_is_shown_next_to_the_light_one(authed_client):
    parsed = page(authed_client)
    dark = [m for m in APP_META if m.get("icon_dark")]
    assert [m["id"] for m in dark] == ["qnap"]
    for meta in APP_META:
        assert os.path.exists(os.path.join(ICONS, meta["icon"]))
        icons = parsed.icons[meta["id"]]
        if meta.get("icon_dark"):
            assert os.path.exists(os.path.join(ICONS, meta["icon_dark"]))
            assert [(i["class"], i["src"].rsplit("/", 1)[-1]) for i in icons] == [
                ("app-icon icon-light", meta["icon"]), ("app-icon icon-dark", meta["icon_dark"])]
        else:
            assert [i["class"] for i in icons] == ["app-icon"]


def test_the_dark_icon_reaches_the_ui_metadata(authed_client):
    html = authed_client.get("/").get_data(as_text=True)
    assert '"icon_dark": "qnap-light.svg"' in html and '"help_url": "https://rsaturns.github.io/backuparr/apps/qnap/#setting-it-up"' in html
