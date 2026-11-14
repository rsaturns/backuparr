"""The firewalls' single-file backups (#153): OPNsense's and pfSense's
config.xml. A download is only kept if it's a whole one."""
import xml.etree.ElementTree as ET


def check(path, root_tag, error, app, label):
    """Raises error unless path parses to the end (a cut-off download
    doesn't) with root_tag as its root. Streamed, so a large file isn't held
    in memory; expat's own limits stop entity expansion."""
    root = None
    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            if root is None:
                root = element.tag
            elif event == "end":
                element.clear()
    except ET.ParseError as exc:
        raise error(f"{app}: the configuration isn't valid XML ({exc})") from None
    if root != root_tag:
        raise error(f"{app}: that isn't {label} configuration (no <{root_tag}> root) - check the URL")
