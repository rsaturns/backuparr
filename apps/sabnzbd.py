"""Backup/restore driver for SABnzbd's config API.

BACKUP LIMITATION: SABnzbd's only config-read API mode, get_config, masks
every password field (most importantly your Usenet server password) to a
literal "**********" before returning it - this is hardcoded in SABnzbd's
own source for its public API and there is no API mode that returns the
real value.

RESTORE: verified against SABnzbd's own source (sabnzbd/config.py,
sabnzbd/api.py). Two mechanisms:

- Plain sections (misc, logging, ...) are flat keyword: value maps; each
  key is restored individually via mode=set_config&section=<s>&keyword=<k>
  &value=<v>. Confirmed via ConfigCollection.set_config(): it looks up
  exactly one Option by (section, keyword) and calls set_dict() on it,
  which for a plain Option just does self.set(values["value"]).

- servers is special-cased server side (handle_server_api in api.py):
  mode=set_config&section=servers&keyword=<name>&<field>=<value> looks up
  the existing ConfigServer by name and calls its set_dict(kwargs), which
  updates only the fields present in kwargs (confirmed: Option.get_from_dict
  raises KeyError for an absent field, and ConfigServer.set_dict() catches
  that and just skips it - so omitting "password" from the call leaves
  whatever password is already configured untouched, rather than clearing
  it). That's what makes it safe to restore a server's non-secret fields
  (host/port/username/connections/...) without a password, and to only
  send "password" when we actually have a real value for it.

categories/rss/sorters are also special-cased server side with their own
(unverified) shapes and are deliberately not auto-restored here - they're
usually small enough to recreate by hand.
"""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

MASKED = "*" * 10  # OptionPassword.get_stars(), see sabnzbd/config.py

# Section types that use a special server-side handler with a shape this
# module doesn't attempt to reconstruct (see module docstring).
UNHANDLED_SPECIAL_SECTIONS = {"categories", "rss", "sorters"}

_WARNING = (
    "SABnzbd config backup - IMPORTANT\n"
    "==================================\n"
    "SABnzbd's API always masks password fields (e.g. Usenet server "
    "passwords) as '**********' - this file does NOT contain your real "
    "password. `restore.py sabnzbd` will prompt you for each Usenet "
    "server's password interactively and restore the rest of the config "
    "automatically; categories/RSS feeds/sorters are not auto-restored "
    "(see README) and any other field that shows as '**********' below "
    "needs to be re-entered by hand in SABnzbd's Settings UI.\n"
)


class SabnzbdError(RuntimeError):
    pass


class SabnzbdApp:
    def __init__(self, url, api_key, timeout=30):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _call(self, params):
        payload = {"apikey": self.api_key, "output": "json", **params}
        res = requests.post(f"{self.url}/sabnzbd/api", data=payload, timeout=self.timeout)
        res.raise_for_status()
        data = res.json()
        if isinstance(data, dict) and data.get("status") is False:
            raise SabnzbdError(f"sabnzbd: API call failed ({params.get('mode')}): {data.get('error')}")
        return data

    def test_connection(self):
        # mode=version deliberately skips API-key checks in SABnzbd (used for
        # the login page), so it can't tell us if the key is right - use
        # get_config instead, which does enforce it.
        self.get_config()
        return "sabnzbd reachable, API key OK"

    def get_config(self):
        data = self._call({"mode": "get_config"})
        if "config" not in data:
            raise SabnzbdError(f"sabnzbd: unexpected get_config response: {data}")
        return data

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        config = self.get_config()
        with open(os.path.join(dest_dir, "_READ_ME_FIRST.txt"), "w") as f:
            f.write(_WARNING)
        with open(os.path.join(dest_dir, "sabnzbd_config.json"), "w") as f:
            json.dump(config, f, indent=2)
        logger.warning("sabnzbd: backed up via API - server passwords are masked, see _READ_ME_FIRST.txt")
        return dest_dir

    def set_value(self, section, keyword, value):
        self._call({"mode": "set_config", "section": section, "keyword": keyword, "value": value})

    def set_server(self, name, fields):
        """fields: any subset of ConfigServer's keys (host/port/username/
        password/connections/ssl/priority/...). Fields not included are left
        untouched on an existing server."""
        self._call({"mode": "set_config", "section": "servers", "keyword": name, **fields})

    def restore(self, config, password_prompt):
        """password_prompt(server_name, server_dict) -> str or falsy to skip.

        Returns a summary dict: servers_restored, servers_missing_password,
        misc_keys_restored, sections_skipped.
        """
        summary = {
            "servers_restored": [],
            "servers_missing_password": [],
            "misc_keys_restored": [],
            "sections_skipped": [],
        }
        cfg = config.get("config", {})

        for server in cfg.get("servers", []):
            name = server.get("name")
            if not name:
                logger.warning("sabnzbd: server entry with no name, skipping: %s", server)
                continue
            fields = {k: v for k, v in server.items() if k not in ("name", "password") and v is not None}
            if server.get("password") == MASKED:
                password = password_prompt(name, server)
                if password:
                    fields["password"] = password
                else:
                    summary["servers_missing_password"].append(name)
            self.set_server(name, fields)
            summary["servers_restored"].append(name)

        for section, value in cfg.items():
            if section == "servers":
                continue
            if section in UNHANDLED_SPECIAL_SECTIONS:
                if value:
                    summary["sections_skipped"].append(section)
                continue
            if not isinstance(value, dict):
                continue
            for keyword, val in value.items():
                if val == MASKED:
                    logger.warning("sabnzbd: %s.%s is masked and not a server password, skipping", section, keyword)
                    continue
                self.set_value(section, keyword, val)
                summary["misc_keys_restored"].append(f"{section}.{keyword}")

        return summary
