"""Copies the last three releases from CHANGELOG.md into the Unraid template's
<Changes>, and the newest release's date into <Date>, so Community Apps shows
what's new. Run it in each release's changelog commit:

    python scripts/sync_unraid_template.py           # rewrite the template
    python scripts/sync_unraid_template.py --check   # exit 1 if it's out of date

tests/test_unraid_template.py fails while the template is out of date.
"""
import os
import re
import sys
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
TEMPLATE = os.path.join(ROOT, "templates", "backuparr.xml")
ALL_CHANGES_URL = "https://github.com/rsaturns/backuparr/blob/main/CHANGELOG.md"
RELEASES_SHOWN = 3

_SECTION = re.compile(r"^## \[(\d+\.\d+\.\d+[^\]]*)\] - (\d{4}-\d{2}-\d{2})\s*$", re.M)


def released_sections(changelog):
    """[(version, date, [entry, ...]), ...], newest first. [Unreleased] is
    skipped; entries are the "- " lines, wrapped ones joined, markdown bold
    and code marks removed."""
    found = list(_SECTION.finditer(changelog))
    sections = []
    for i, match in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(changelog)
        body = changelog[match.end():end]
        body = body.split("\n## ", 1)[0]  # stop at any other level-2 heading
        entries, in_entry = [], False
        for line in body.splitlines():
            if line.startswith("- "):
                entries.append(line[2:].strip())
                in_entry = True
            elif in_entry and line.startswith("  ") and line.strip():
                entries[-1] += " " + line.strip()  # an entry wrapped onto more lines
            else:
                in_entry = False
        entries = [re.sub(r"\*\*|`", "", e) for e in entries]
        sections.append((match.group(1), match.group(2), entries))
    return sections


def changes_text(sections):
    blocks = []
    for version, date, entries in sections[:RELEASES_SHOWN]:
        blocks.append("\n".join([f"### {version} ({date})", *(f"- {e}" for e in entries)]))
    blocks.append(f"View all changes: {ALL_CHANGES_URL}")
    return "\n\n".join(blocks)


def _set_tag(template, tag, text):
    element = f"<{tag}>{escape(text)}</{tag}>"
    pattern = re.compile(rf"<{tag}>.*?</{tag}>", re.S)
    if pattern.search(template):
        return pattern.sub(lambda _: element, template, count=1)
    # New tags go before the first <Config>, after the app's description.
    at = template.index("  <Config ")
    return f"{template[:at]}  {element}\n{template[at:]}"


def render(template, changelog):
    sections = released_sections(changelog)
    if not sections:
        raise SystemExit("CHANGELOG.md has no released versions")
    template = _set_tag(template, "Changes", changes_text(sections))
    return _set_tag(template, "Date", sections[0][1])


def main(argv):
    with open(TEMPLATE, encoding="utf-8") as f:
        current = f.read()
    with open(CHANGELOG, encoding="utf-8") as f:
        updated = render(current, f.read())
    if "--check" in argv:
        if updated != current:
            sys.exit("templates/backuparr.xml is out of date: run python scripts/sync_unraid_template.py")
        return
    if updated != current:
        with open(TEMPLATE, "w", encoding="utf-8") as f:
            f.write(updated)
        print("templates/backuparr.xml updated")


if __name__ == "__main__":
    main(sys.argv[1:])
