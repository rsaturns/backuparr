"""Persistent config for Backuparr - a single JSON file on a volume,
shared by backup.py and the web UI."""
import copy
import json
import logging
import os
import re

from croniter import croniter

import atomic_file
import secrets_crypto
from apps.http_headers import RequestHeadersError, validate_request_headers

log = logging.getLogger("backuparr.config_store")

CONFIG_PATH = os.environ.get("BACKUPARR_CONFIG", "/config/backuparr/config.json")


class ConfigError(RuntimeError):
    """config.json exists but can't be used (hand-edited into invalid JSON,
    truncated by a full disk, ...). The message says what to do about it."""


APP_NAMES = ["radarr", "sonarr", "lidarr", "whisparr", "prowlarr", "profilarr", "bazarr", "tdarr", "sabnzbd", "nzbget", "tautulli", "jellyfin", "emby", "plex", "seerr", "lazylibrarian", "pihole", "vaultwarden", "opnsense", "pfsense", "immich", "homeassistant", "unifi", "unraid", "bitwarden", "synology"]

# encrypt: whether its backups are encrypted (#147) - on unless turned off,
# and ignored for an app whose APP_META sets always_encrypted.
DEFAULT_APP = {"enabled": False, "url": "", "api_key": "", "username": "", "password": "", "encrypt": True}

# Apps that can run more than once - e.g. a 4K Radarr beside the HD one. The
# first instance keeps the bare app id, so its config, remote folder and
# archive names never change; each extra one is "<app>-<suffix>" (radarr-4k),
# an id fixed when it's added because it names the instance's backup folder.
# Only its display name can change later.
MULTI_INSTANCE_APPS = ("radarr", "sonarr", "lidarr", "whisparr", "pihole", "opnsense", "pfsense", "immich", "homeassistant", "unifi", "unraid", "synology")
MAX_EXTRA_INSTANCES = 10
_EXTRA_INSTANCE_ID = re.compile(rf"({'|'.join(MULTI_INSTANCE_APPS)})-([a-z0-9]+(?:-[a-z0-9]+)*)")
_MAX_INSTANCE_ID_LENGTH = 40
# Apps Prowlarr discovery can fill in: its Applications, then its download
# clients. Each one's Prowlarr implementation, lowercased, is its app id.
DISCOVERABLE_APPS = ("radarr", "sonarr", "lidarr", "whisparr", "lazylibrarian", "sabnzbd", "nzbget")
# An extra instance also has a display name, e.g. "Radarr 4K".
DEFAULT_INSTANCE = {**DEFAULT_APP, "name": ""}

# Radarr, Sonarr, Lidarr, Whisparr and Prowlarr: their own login, only for
# downloading backups when Authentication Required is Enabled (apps/servarr.py).
# "login_only" fields stay hidden on the card until a test finds a login, or
# once they hold a value, and aren't asked for by the Restore tab.
SERVARR_LOGIN_FIELDS = [
    {"name": "username", "label": "Username", "type": "text", "login_only": True,
     "help": "the app's own login"},
    {"name": "password", "label": "Password", "type": "password", "login_only": True,
     "help": "the app's own login"},
]
# Apps whose "password" extra field is encrypted at rest.
PASSWORD_APPS = ("bazarr", "nzbget", "radarr", "sonarr", "lidarr", "whisparr", "prowlarr", "pfsense", "unifi", "unraid", "synology")
# Other extra fields encrypted at rest, by app type.
SECRET_EXTRA_FIELDS = {"opnsense": ("api_secret",), "synology": ("device_token",)}

# Settings, History and the Restore and Run & Status pickers group the apps
# under these, in this order (#148). A category with no apps isn't shown, so
# the later ones wait for their apps. A category with an "icon" shows that
# image (style.css picks it per theme) in place of an emoji (#189). The
# pickers' group labels show just the name.
APP_CATEGORIES = [
    {"id": "media-management", "label": "Media Management", "emoji": "🎬"},
    {"id": "downloaders", "label": "Downloaders", "emoji": "⬇️"},
    {"id": "media-servers", "label": "Media Servers", "emoji": "▶️"},
    {"id": "network", "label": "Network", "emoji": "🌐"},
    {"id": "security", "label": "Security", "emoji": "🛡️"},
    {"id": "home-automation", "label": "Home Automation", "emoji": "🏠"},
    {"id": "server", "label": "Server", "icon": "server"},
]
# Each app's category: a new app adds one line here.
_APP_CATEGORY = {
    "prowlarr": "media-management", "radarr": "media-management", "sonarr": "media-management",
    "lidarr": "media-management", "whisparr": "media-management", "profilarr": "media-management",
    "bazarr": "media-management", "tdarr": "media-management", "lazylibrarian": "media-management",
    "sabnzbd": "downloaders", "nzbget": "downloaders",
    "plex": "media-servers", "jellyfin": "media-servers", "emby": "media-servers", "tautulli": "media-servers",
    "seerr": "media-servers", "immich": "media-servers",
    "pihole": "network", "opnsense": "network", "pfsense": "network", "unifi": "network",
    "bitwarden": "security", "vaultwarden": "security",
    "homeassistant": "home-automation",
    "synology": "server", "unraid": "server",
}

# Drives the web UI's forms generically. restore_warning is shown on the
# Restore tab before an app's restore. manual_restore: an app Backuparr can't
# restore itself - the Restore tab offers the backup's download, its "steps"
# (one or more titled lists) and a link to its "docs" page (#151).
# always_encrypted: True for an app whose backups hold secrets that must never
# sit readable on a destination (#147) - its card has no "Encrypt backup"
# toggle. pins_certificate: its self-signed HTTPS certificate can be trusted
# on the card and is then pinned (trusted_cert, apps/cert_pin.py, #153).
#
# extra_fields are text or password inputs, unless "type" says otherwise
# (#155): "switch" (on/off, "default"), "select" ("choices", "default"),
# "app_list" (the apps a Test connection listed, [{"slug", "name"}], shown
# while the select before it is "chosen") or "hidden" (set by a Test
# connection: one of "choices", or any short text without them, e.g.
# Synology's 2FA device token). A "locked" switch is shown on and saves
# nothing; a "not_saved" field is sent with a Test connection only (a 2FA
# code). "option": a backup option, never asked for by the Restore tab.
# "only_install": shown once a Test connection (install_type) found that
# kind of install.
APP_META = [
    {"id": "radarr", "label": "Radarr", "icon": "radarr.svg", "key_required": True, "url_placeholder": "http://radarr:7878", "extra_fields": SERVARR_LOGIN_FIELDS, "multi_instance": True, "max_extra_instances": MAX_EXTRA_INSTANCES},
    {"id": "sonarr", "label": "Sonarr", "icon": "sonarr.svg", "key_required": True, "url_placeholder": "http://sonarr:8989", "extra_fields": SERVARR_LOGIN_FIELDS, "multi_instance": True, "max_extra_instances": MAX_EXTRA_INSTANCES},
    {"id": "lidarr", "label": "Lidarr", "icon": "lidarr.svg", "key_required": True, "url_placeholder": "http://lidarr:8686", "extra_fields": SERVARR_LOGIN_FIELDS, "multi_instance": True, "max_extra_instances": MAX_EXTRA_INSTANCES},
    {"id": "whisparr", "label": "Whisparr", "icon": "whisparr.svg", "key_required": True, "url_placeholder": "http://whisparr:6969", "extra_fields": SERVARR_LOGIN_FIELDS, "multi_instance": True, "max_extra_instances": MAX_EXTRA_INSTANCES},
    {"id": "prowlarr", "label": "Prowlarr", "icon": "prowlarr.svg", "key_required": True, "url_placeholder": "http://prowlarr:9696", "extra_fields": SERVARR_LOGIN_FIELDS},
    {
        "id": "profilarr",
        "label": "Profilarr",
        "icon": "profilarr.svg",
        "key_required": True,
        "url_placeholder": "http://profilarr:6868",
        "extra_fields": [],
        # Profilarr's restore is a form on its own Backups page, applied when
        # it restarts - no API. The Restore tab offers the download and these
        # steps instead (#151), from https://profilarr.com/installation/backups.
        "manual_restore": {
            "docs": "https://rsaturns.github.io/backuparr/apps/profilarr/",
            "steps": [
                {"title": "Restore in Profilarr", "steps": [
                    "Download the backup.",
                    "In Profilarr, open Settings > Backups. The restore replaces all of Profilarr's data, so click Create Backup first if you might need it.",
                    "Click Upload Backup and pick the downloaded file.",
                    "Click Restore on its row, then restart Profilarr (for example docker restart profilarr).",
                    "If Profilarr asks, create an account on its setup page. Then add back what its backups leave out: the Arr instances, notification services and API keys.",
                ]},
            ],
        },
    },
    {
        "id": "bazarr",
        "label": "Bazarr",
        "icon": "bazarr.svg",
        "key_required": True,
        "url_placeholder": "http://bazarr:6767",
        "extra_fields": [
            {"name": "username", "label": "Basic auth username", "type": "text",
             "help": "Only if Settings > General > Security is set to Basic"},
            {"name": "password", "label": "Basic auth password", "type": "password",
             "help": "Only if Settings > General > Security is set to Basic"},
        ],
    },
    {
        "id": "tdarr",
        "label": "Tdarr",
        "icon": "tdarr.png",
        "key_required": False,
        "url_placeholder": "http://192.168.1.10:8266",
        "extra_fields": [],
        "key_help": "Tools > API Keys - needed when Tdarr's authentication is on, the default from Tdarr 3",
        "restore_warning": {"lead": "Destructive:", "text": "this wipes each Tdarr database collection before repopulating it from the backup.", "danger": True},
    },
    {"id": "sabnzbd", "label": "SABnzbd", "icon": "sabnzbd.svg", "key_required": True, "url_placeholder": "http://sabnzbd:8080", "extra_fields": []},
    {
        "id": "nzbget",
        "label": "NZBGet",
        "icon": "nzbget.svg",
        # NZBGet has no API key: its JSON-RPC API takes the username and
        # password of the Control user - see apps/nzbget.py.
        "key_required": False,
        "no_api_key": True,
        "url_placeholder": "http://nzbget:6789",
        "extra_fields": [
            {"name": "username", "label": "Username", "type": "text",
             "help": "Settings > Security > ControlUsername"},
            {"name": "password", "label": "Password", "type": "password",
             "help": "Settings > Security > ControlPassword"},
        ],
        "restore_warning": {"lead": "Replaces NZBGet's settings", "text": "with the backup's - news servers and their passwords, categories, paths, extension settings, and the username and password. NZBGet reloads to apply them; the queue and history are kept. If the backup's username or password differ from the ones in Settings, update them there afterwards."},
    },
    {"id": "tautulli", "label": "Tautulli", "icon": "tautulli.svg", "key_required": True, "url_placeholder": "http://tautulli:8181", "extra_fields": []},
    {
        "id": "jellyfin",
        "label": "Jellyfin",
        "icon": "jellyfin.svg",
        "key_required": True,
        "url_placeholder": "http://jellyfin:8096",
        "key_help": "Dashboard > API Keys",
        # Jellyfin's API can't download a backup, so its backups folder is
        # mounted into this container - see apps/jellyfin.py.
        "extra_fields": [
            {"name": "backup_dir", "label": "Backups folder", "type": "text", "required": True,
             "placeholder": "/mnt/jellyfin-backups",
             "help": "Jellyfin's backups folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Replaces Jellyfin's database and settings", "text": "with the backup's - users, watch history, libraries and API keys. Anything added since is lost. Jellyfin restarts to apply it."},
    },
    {
        "id": "emby",
        "label": "Emby",
        "icon": "emby.svg",
        "key_required": True,
        "url_placeholder": "http://emby:8096",
        "key_help": "Settings > Advanced > API Keys",
        # Emby has no backup API, so its data folder is mounted into this
        # container - see apps/emby.py.
        "extra_fields": [
            {"name": "data_dir", "label": "Data folder", "type": "text", "required": True,
             "placeholder": "/mnt/emby",
             "help": "Emby's /config folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Stop Emby first", "text": "- the restore is refused while it's running. It replaces Emby's databases and settings with the backup's - users, watch history, playlists, collections and API keys. Anything added since is lost. Start Emby again afterwards."},
    },
    {
        "id": "plex",
        "label": "Plex",
        "icon": "plex.svg",
        "key_required": True,
        "key_label": "Plex token",
        "url_placeholder": "http://plex:32400",
        # The database comes from Plex's API; the server's settings and
        # identity (Preferences.xml) from its mounted data folder - see
        # apps/plex.py.
        "extra_fields": [
            {"name": "data_dir", "label": "Data folder", "type": "text", "required": True,
             "placeholder": "/mnt/plex",
             "help": "Plex's \"Plex Media Server\" folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Stop Plex first", "text": "- the restore is refused while it's running. It replaces Plex's library database and settings with the backup's - libraries, playlists, collections, every user's watched state, and the server's identity, so a rebuilt server comes back as the same server with its shared users. Anything added since is lost. Never run the original server at the same time. Start Plex again afterwards."},
    },
    {
        "id": "seerr",
        "label": "Seerr",
        "icon": "seerr.svg",
        "key_required": True,
        "url_placeholder": "http://seerr:5055",
        "key_help": "Settings > General > API Key",
        # Seerr has no backup API, so its config folder is mounted into this
        # container - see apps/seerr.py.
        "extra_fields": [
            {"name": "data_dir", "label": "Data folder", "type": "text", "required": True,
             "placeholder": "/mnt/seerr",
             "help": "Seerr's /app/config folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Stop Seerr first", "text": "- the restore is refused while it's running. It replaces Seerr's database and settings with the backup's - users, requests, issues, the media server and Radarr/Sonarr connections, notifications and the API key. Anything added since is lost. Start Seerr again afterwards."},
    },
    {
        "id": "lazylibrarian",
        "label": "LazyLibrarian",
        "icon": "lazylibrarian.png",
        "key_required": True,
        "url_placeholder": "http://lazylibrarian:5299",
        "key_help": "Config > Interface > API > API Key",
        # LazyLibrarian's backup API only says where it wrote the file, so its
        # data folder is mounted into this container - see apps/lazylibrarian.py.
        "extra_fields": [
            {"name": "data_dir", "label": "Data folder", "type": "text", "required": True,
             "placeholder": "/mnt/lazylibrarian",
             "help": "LazyLibrarian's /config folder as mounted here; read-write to restore"},
        ],
        "restore_warning": {"lead": "Stop LazyLibrarian first", "text": "- the restore is refused while it's running. It replaces LazyLibrarian's database and settings with the backup's, its API key included. Anything added since is lost. Start LazyLibrarian again afterwards."},
    },
    {
        "id": "pihole",
        "label": "Pi-hole",
        "icon": "pi-hole.svg",
        "key_required": True,
        # Pi-hole v6's Teleporter, over its API - see apps/pihole.py. The
        # archive holds its password hashes and 2FA secret.
        "key_label": "App password",
        "key_help": "Settings > Web interface / API > Configure app password (Expert mode)",
        "url_placeholder": "http://pihole",
        "extra_fields": [],
        "multi_instance": True,
        "max_extra_instances": MAX_EXTRA_INSTANCES,
        "always_encrypted": True,
        "restore_warning": {"lead": "Replaces Pi-hole's settings", "text": "with the backup's - DNS and DHCP settings, local DNS records, adlists, domains, groups and clients, DHCP leases, and its web password, app password and 2FA. Settings set by environment variables (FTLCONF_...) are kept. Pi-hole restarts to apply it. If the backup's app password differs from the one in Settings, update it there afterwards."},
    },
    {
        "id": "opnsense",
        "label": "OPNsense",
        "icon": "opnsense.svg",
        "key_required": True,
        # GET /api/core/backup/download/this with an API key and secret of a
        # user holding only "Diagnostics: Configuration History" and "Lobby:
        # Dashboard" (the version) - see apps/opnsense.py. config.xml holds
        # private keys and password hashes.
        "key_help": "a user with Diagnostics: Configuration History and Lobby: Dashboard - see the docs",
        "url_placeholder": "https://192.168.1.1",
        "extra_fields": [
            {"name": "api_secret", "label": "API secret", "type": "password", "required": True,
             "help": "from the same API key file"},
        ],
        "multi_instance": True,
        "max_extra_instances": MAX_EXTRA_INSTANCES,
        "always_encrypted": True,
        "pins_certificate": True,
        "manual_restore": {
            "docs": "https://rsaturns.github.io/backuparr/apps/opnsense/",
            "steps": [
                {"title": "Firewall still running", "steps": [
                    "Download the backup.",
                    "In OPNsense, open System > Configuration > Backups.",
                    "Under Restore, leave Restore areas at All and Reboot after a successful restore ticked.",
                    "Choose the downloaded file and click Restore configuration. The firewall reboots.",
                    "If OPNsense asks you to assign the interfaces again, do so.",
                ]},
                {"title": "New install", "steps": [
                    "Download the backup and rename it config.xml.",
                    "On a second USB stick formatted FAT32, make a folder named conf and put config.xml in it.",
                    "Boot the OPNsense installer with both sticks plugged in.",
                    "At \"Press any key to start the configuration importer\", press a key and enter the second stick's device name (for example da1).",
                    "Log in as installer with the backup's root password and install.",
                ]},
            ],
        },
    },
    {
        "id": "pfsense",
        "label": "pfSense",
        "icon": "pfsense.svg",
        # No official API: the web UI's own backup page, signed in the way
        # Netgate documents for scripted backups - see apps/pfsense.py.
        "key_required": False,
        "no_api_key": True,
        "url_placeholder": "https://192.168.1.1",
        "extra_fields": [
            {"name": "username", "label": "Username", "type": "text", "required": True,
             "help": "a user with WebCfg - Diagnostics: Backup & Restore and WebCfg - System: Login / Logout / Dashboard - see the docs"},
            {"name": "password", "label": "Password", "type": "password", "required": True},
        ],
        "multi_instance": True,
        "max_extra_instances": MAX_EXTRA_INSTANCES,
        "always_encrypted": True,
        "pins_certificate": True,
        "manual_restore": {
            "docs": "https://rsaturns.github.io/backuparr/apps/pfsense/",
            "steps": [
                {"title": "Firewall still running", "steps": [
                    "Download the backup.",
                    "In pfSense, open Diagnostics > Backup & Restore.",
                    "Under Restore Backup, leave Restore area at All, choose the downloaded file and click Restore Configuration. The firewall reboots.",
                ]},
                {"title": "New install", "steps": [
                    "Download the backup and rename it config.xml.",
                    "On a USB stick formatted FAT or FAT32, make a folder named config and put config.xml in it.",
                    "Install pfSense. Then plug in the stick and boot the firewall: pfSense copies the configuration from it.",
                    "Remove the stick once the firewall is up, or pfSense copies it again at every boot.",
                ]},
            ],
        },
    },
    {
        "id": "unifi",
        "label": "UniFi",
        "icon": "ubiquiti-unifi.svg",
        # No documented backup API: UniFi OS's own backup (everything, a Super
        # Admin) or the Network app's (Network only), signed in as a local
        # admin; the standalone UniFi Network Server only has the Network
        # one - see apps/unifi.py. The backups hold Wi-Fi passwords, VPN keys
        # and admin logins. A Test connection finds the install type, which
        # picks the options shown.
        "key_required": False,
        "no_api_key": True,
        "url_placeholder": "https://192.168.1.1",
        "extra_fields": [
            {"name": "username", "label": "Username", "type": "text", "required": True,
             "help": "a local admin - see the docs for its role"},
            {"name": "password", "label": "Password", "type": "password", "required": True},
            {"name": "backup", "label": "Backup", "type": "select", "default": "everything", "option": True,
             "only_install": "unifi-os", "help": "everything needs a Super Admin",
             "choices": [{"value": "everything", "label": "Everything"}, {"value": "network", "label": "Network only"}]},
            {"name": "history", "label": "History", "type": "select", "default": "settings", "option": True,
             "help": "statistics kept with the Network settings",
             "show_if": [{"install_type": ["standalone"]}, {"install_type": ["unifi-os"], "backup": ["network"]}],
             "choices": [{"value": "settings", "label": "Settings only"}, {"value": "7", "label": "Last 7 days"},
                         {"value": "30", "label": "Last 30 days"}, {"value": "90", "label": "Last 90 days"},
                         {"value": "365", "label": "Last 365 days"}, {"value": "all", "label": "Everything"}]},
            {"name": "install_type", "type": "hidden", "option": True, "choices": ["unifi-os", "standalone"]},
        ],
        "multi_instance": True,
        "max_extra_instances": MAX_EXTRA_INSTANCES,
        "always_encrypted": True,
        "pins_certificate": True,
        "manual_restore": {
            "docs": "https://rsaturns.github.io/backuparr/apps/unifi/",
            "steps": [
                {"title": "UniFi OS", "steps": [
                    "Download the backup: a .unifi file holds everything, a .unf file UniFi Network only.",
                    "On a new install, finish the setup first (Proceed Without a UI Account works) and update it to the backup's versions or newer.",
                    "Open Settings > Control Plane > Backups and click Upload & Restore.",
                    "Choose the downloaded file. For a .unifi file, leave Restore All Applications and Settings ticked.",
                    "Click Restore. UniFi restarts with the backup's settings.",
                ]},
                {"title": "Standalone app", "steps": [
                    "Download the backup.",
                    "On a new install, click Restore Server from a Backup on the setup's first page, choose the downloaded file and click Confirm.",
                    "Otherwise, open Settings > System > Backups, click Restore, choose the file and click Restore.",
                ]},
            ],
        },
    },
    {
        "id": "bitwarden",
        "label": "Bitwarden",
        "icon": "bitwarden.svg",
        # Bitwarden has no backup API (#194): the standard install's bwdata
        # or lite's backups folder is mounted into this container, and the
        # driver tells which it is - see apps/bitwarden.py. The URL only gives
        # its version. The backup holds the database and certificate
        # passwords and the data-protection keys. Bitwarden's installers
        # offer a self-signed certificate, so it can be trusted (#153).
        "key_required": False,
        "no_api_key": True,
        "url_placeholder": "https://bitwarden:8443",
        "extra_fields": [
            {"name": "data_dir", "label": "Folder", "type": "text", "required": True,
             "placeholder": "/mnt/bitwarden",
             "help": "Bitwarden's bwdata folder, or lite's backups folder, as mounted here"},
        ],
        "always_encrypted": True,
        "pins_certificate": True,
        "manual_restore": {
            "docs": "https://rsaturns.github.io/backuparr/apps/bitwarden/",
            "steps": [
                {"title": "Standard install", "steps": [
                    "Stop Bitwarden with ./bitwarden.sh stop. On a new server, download bitwarden.sh but don't run its install.",
                    "Download the backup and unzip it into bwdata, replacing what's there (on a new server, an empty bwdata next to bitwarden.sh).",
                    "Start Bitwarden with ./bitwarden.sh start and give it a minute.",
                    "Restore the database from the .BAK file in mssql/backups with sqlcmd, as the Bitwarden page in Backuparr's docs shows.",
                    "Restart Bitwarden with ./bitwarden.sh restart, then sign in and open an item to check.",
                ]},
                {"title": "Bitwarden lite", "steps": [
                    "Download the backup and put it in lite's backups folder.",
                    "Stop lite with docker compose stop bitwarden.",
                    "Run docker compose run --rm --no-deps --entrypoint /restore-db.sh bitwarden /backups/<file> --clean --yes.",
                    "Start lite with docker compose up -d, then sign in and open an item to check.",
                ]},
            ],
        },
    },
    {
        "id": "vaultwarden",
        "label": "Vaultwarden",
        "icon": "vaultwarden.svg",
        # Vaultwarden has no backup API, so its data folder is mounted into
        # this container; the URL only gives its version and whether it's
        # running - see apps/vaultwarden.py. The backup holds its RSA key and
        # config.json (admin token, SMTP password) in plain text.
        "key_required": False,
        "no_api_key": True,
        "url_placeholder": "http://vaultwarden",
        "extra_fields": [
            {"name": "data_dir", "label": "Data folder", "type": "text", "required": True,
             "placeholder": "/mnt/vaultwarden",
             "help": "Vaultwarden's /data folder as mounted here; read-write to restore"},
        ],
        "always_encrypted": True,
        "restore_warning": {"lead": "Stop Vaultwarden first", "text": "- the restore is refused while it's running. It replaces Vaultwarden's database with the backup's - users, organisations, every vault item, devices and 2FA - and writes back its RSA key, admin page settings, attachments and Send files. Anything added since is lost. Start Vaultwarden again afterwards."},
    },
    {
        "id": "immich",
        "label": "Immich",
        "icon": "immich.svg",
        "key_required": True,
        # Immich's database backup API (2.5.0 and newer) - see apps/immich.py.
        # The database only: photos and videos are files, not in the backup.
        "key_help": "an admin's key with job.create, queue.read, maintenance, backup.download, backup.upload and backup.delete",
        "url_placeholder": "http://immich:2283",
        "extra_fields": [],
        "multi_instance": True,
        "max_extra_instances": MAX_EXTRA_INSTANCES,
        "restore_warning": {"lead": "Replaces Immich's database", "text": "with the backup's - users, albums, people and faces, shared links, settings and API keys. Photos and videos aren't in the backup and stay where they are; ones uploaded since the backup won't be in the restored library. Immich goes into maintenance mode while it restores, then restarts."},
    },
    {
        "id": "homeassistant",
        "label": "Home Assistant",
        "icon": "home-assistant.svg",
        # Home Assistant's own backup API (2025.5 and newer) over its
        # WebSocket - see apps/homeassistant.py. The options mirror its own
        # backup page; the ones only HA OS and Supervised have show once a
        # Test connection found such an install.
        "key_required": True,
        "key_label": "Access token",
        "key_help": "a long-lived access token of an administrator, from your profile's Security tab",
        "url_placeholder": "http://homeassistant:8123",
        "extra_fields": [
            {"name": "settings", "label": "Settings", "type": "switch", "locked": True, "option": True},
            {"name": "history", "label": "History", "type": "switch", "default": True, "option": True,
             "help": "history, statistics and energy"},
            {"name": "media", "label": "Media", "type": "switch", "default": False, "option": True,
             "only_install": "supervisor"},
            {"name": "share", "label": "Share folder", "type": "switch", "default": False, "option": True,
             "only_install": "supervisor"},
            {"name": "local_apps", "label": "Local apps folder", "type": "switch", "default": False, "option": True,
             "only_install": "supervisor", "help": "apps you built"},
            {"name": "apps", "label": "Apps", "type": "select", "default": "none", "option": True,
             "only_install": "supervisor",
             "choices": [{"value": "none", "label": "None"}, {"value": "all", "label": "All"},
                         {"value": "chosen", "label": "Chosen apps"}]},
            {"name": "chosen_apps", "label": "Chosen apps", "type": "app_list", "option": True,
             "only_install": "supervisor"},
            {"name": "install_type", "type": "hidden", "option": True, "choices": ["core", "supervisor"]},
        ],
        "multi_instance": True,
        "max_extra_instances": MAX_EXTRA_INSTANCES,
        "always_encrypted": True,
        "pins_certificate": True,
        "restore_warning": {"lead": "Home Assistant restarts", "text": "and is offline for a few minutes. The backup replaces its settings, and its history if the backup has it - on HA OS its apps and folders too. A token made after the backup stops working."},
    },
    {
        "id": "synology",
        "label": "Synology DSM",
        "icon": "synology-dsm.svg",
        # DSM's own Configuration Backup (.dss) - its settings, not the data in
        # its shared folders - over the Control Panel's Web API calls, signed
        # in as an administrator: see apps/synology.py. A 2FA code is asked
        # for once; DSM's device token (set by Test connection) signs in after.
        "key_required": False,
        "no_api_key": True,
        "url_placeholder": "https://192.168.1.20:5001",
        "extra_fields": [
            {"name": "username", "label": "Username", "type": "text", "required": True,
             "help": "an account in the administrators group"},
            {"name": "password", "label": "Password", "type": "password", "required": True},
            {"name": "otp_code", "label": "2FA code", "type": "text", "login_only": True, "not_saved": True,
             "help": "from the account's authenticator app, asked for once"},
            {"name": "device_token", "type": "hidden"},
        ],
        "multi_instance": True,
        "max_extra_instances": MAX_EXTRA_INSTANCES,
        "always_encrypted": True,
        "pins_certificate": True,
        "manual_restore": {
            "docs": "https://rsaturns.github.io/backuparr/apps/synology/",
            "steps": [
                {"title": "DSM 7", "steps": [
                    "Download the backup: DSM's .dss configuration file. It holds DSM's settings, not the files in its shared folders.",
                    "In DSM, open Control Panel > Update & Restore > Configuration Backup. Under Restore DSM Configurations, click Restore.",
                    "Choose Restore from a configuration file on your computer, browse to the .dss and click Next.",
                    "Tick the settings to restore, and Overwrite conflicting settings to replace ones that exist now. Click Done, then Yes: DSM stops its services while it restores.",
                ]},
                {"title": "DSM 6.2", "steps": [
                    "Download the backup: DSM's .dss configuration file.",
                    "In DSM, open Control Panel > Update & Restore > Configuration Backup and click Restore configuration.",
                    "Browse to the .dss, tick the settings to restore, and Overwrite conflicting settings to replace ones that exist now. Click OK, then Yes: DSM stops its services while it restores.",
                ]},
            ],
        },
    },
    {
        "id": "unraid",
        "label": "Unraid",
        "icon": "unraid.svg",
        # The web UI's own Boot Device Backup, signed in as root: Unraid's web
        # UI has only that user, its API has no boot backup, and /boot is
        # root-only - see apps/unraid.py. The zip is kept as Unraid makes it,
        # so the USB Flash Creator restores it in one step.
        "key_required": False,
        "no_api_key": True,
        "url_placeholder": "http://192.168.1.10",
        "extra_fields": [
            {"name": "password", "label": "Root password", "type": "password", "required": True,
             "help": "Unraid's web UI has only the root user"},
        ],
        "multi_instance": True,
        "max_extra_instances": MAX_EXTRA_INSTANCES,
        "always_encrypted": True,
        "pins_certificate": True,
        "manual_restore": {
            "docs": "https://rsaturns.github.io/backuparr/apps/unraid/",
            "steps": [
                {"title": "New or replacement drive", "steps": [
                    "Download the backup: a .zip of the whole boot device.",
                    "In the Unraid USB Flash Creator, choose Use custom, pick the downloaded .zip and write it to the USB flash drive.",
                    "Boot the server from it. On a new drive, move the license to it with Tools > Registration > Replace Key.",
                    "Check the disk assignments on Main before starting the array.",
                ]},
                {"title": "Internal boot", "steps": [
                    "Restore the backup to a USB flash drive as above and boot from it.",
                    "Switch to internal boot again (Tools > Internal Boot): Unraid copies the restored setup to the internal drive.",
                ]},
                {"title": "Single files", "steps": [
                    "Download the backup and open the .zip.",
                    "Copy the files you need from its config folder back to the same place on the boot device (the flash share, or /boot/config), then reboot.",
                ]},
            ],
        },
    },
]
# Backuparr's own backup, shown as an app card in Server (#165). Not an
# APP_META app: it has no URL or key, its switch is cfg["self_backup"], and
# its backups are always encrypted (self_backup.py).
SELF_APP = {"id": "backuparr", "label": "Backuparr", "icon": "logo.png", "category": "server", "always_encrypted": True}

for _meta in APP_META:
    _meta["discoverable"] = _meta["id"] in DISCOVERABLE_APPS
    _meta["category"] = _APP_CATEGORY[_meta["id"]]

DEST_NAMES = ["local", "gdrive", "onedrive", "dropbox"]

# Default local-destination backup path - same volume as config.json.
DEFAULT_LOCAL_DIR = "/config/backuparr/backups"

# retention_days on a destination overrides the global one; None uses it (#133).
DEFAULT_DEST = {
    "local": {"enabled": True, "path": "", "retention_days": None},
    "gdrive": {
        "enabled": False,
        "client_id": "",
        "client_secret": "",
        "developer_key": "",
        "refresh_token": "",
        "folder_id": "",
        "folder_name": "",
        "retention_days": None,
    },
    "dropbox": {"enabled": False, "token": "", "retention_days": None},
    "onedrive": {
        "enabled": False,
        "token": "",
        "drive_id": "",
        "drive_type": "",
        "item_id": "",
        "retention_days": None,
    },
}

# Fields POST /api/config can write per destination - excludes OAuth
# state (tokens, folder IDs), which only the dedicated connect routes set.
DEST_EDITABLE_FIELDS = {
    "local": {"enabled", "path", "retention_days"},
    "gdrive": {"enabled", "client_id", "client_secret", "developer_key", "retention_days"},
    "dropbox": {"enabled", "retention_days"},
    "onedrive": {"enabled", "retention_days"},
}

# Drives the Settings > Destinations card generically.
DESTINATION_META = [
    {
        "id": "local",
        "label": "Local Storage",
        "icon": "local.svg",
        "description": "Written straight to this container's own volume - nothing to connect. Download any backup from the History tab.",
        "setup_help": {
            "title": "Local Storage",
            "intro": "No setup needed - this works out of the box.",
            "steps": [
                {
                    "text": "Backups are written here by default, on the volume already mounted for this container (see docker-compose.yml):",
                    "code": "/config/backuparr/backups",
                    "link": None,
                },
                "Optionally set a custom path below if you'd rather use a different mounted volume - e.g. a NAS share mounted into this container.",
            ],
            "links": [],
        },
    },
    {
        "id": "gdrive",
        "label": "Google Drive",
        "icon": "google-drive.svg",
        "description": "Backed up to a folder in your Google Drive. Click-through OAuth - no rclone config, no terminal.",
        "setup_help": {
            "title": "Connect Google Drive",
            "intro": "Google requires every app to have its own registered OAuth client - a one-time, ~5 minute setup in Google Cloud Console.",
            "steps": [
                {
                    "text": "Create a Google Cloud project (or pick an existing one) - billing is not required for this.",
                    "link": {"label": "Create a project", "url": "https://console.cloud.google.com/projectcreate"},
                },
                {
                    "text": "Enable the Google Drive API for that project.",
                    "link": {"label": "Enable the Google Drive API", "url": "https://console.cloud.google.com/apis/library/drive.googleapis.com"},
                },
                {
                    "text": "Also enable the Google Picker API - it's what the \"Choose folder\" button uses, separately from the Drive API above, and it must be enabled before you can restrict an API key to it in a later step.",
                    "link": {"label": "Enable the Google Picker API", "url": "https://console.cloud.google.com/apis/library/picker.googleapis.com"},
                },
                {
                    "text": "Set up the Google Auth Platform: click \"Get started\", fill in an app name and your support email under App Information, then choose External under Audience (this just means anyone with a Google account can be added as a tester - it does not make the app public).",
                    "link": {"label": "Google Auth Platform - Branding", "url": "https://console.cloud.google.com/auth/branding"},
                },
                {
                    "text": "On the Audience tab, add your own Google account (and anyone else who should have access) under Test users - this keeps the app private with no Google review needed. Leave Publishing status as Testing for now; the next step changes that.",
                    "link": {"label": "Google Auth Platform - Audience", "url": "https://console.cloud.google.com/auth/audience"},
                },
                {
                    "text": "Still on the Audience tab, click \"Publish App\" under Publishing status and confirm. This is the important step: while a project stays in Testing, Google auto-expires every refresh token 7 days after it's issued, so Backuparr's Google Drive connection (and every other Testing-status app) would silently die and need reconnecting weekly. Publishing removes that 7-day limit. It does not require Google's review/verification process here, since drive.file is a non-sensitive scope and this app stays well under Google's 100-user threshold for requiring it - clicking Publish App just flips the status immediately.",
                    "link": {"label": "Google Auth Platform - Audience", "url": "https://console.cloud.google.com/auth/audience"},
                },
                {
                    "text": "On the Data access tab, click \"Add or remove scopes\", filter by API = \"Google Drive API\", and check the row for this scope (not .../auth/drive, which is full access) - its description reads \"See, edit, create, and delete only the specific Google Drive files you use with this app\". It's classified as a non-sensitive scope, so it'll show up under \"Your non-sensitive scopes\" in the panel, not Sensitive or Restricted. Then click \"Update\" at the bottom of the panel:",
                    "code": "https://www.googleapis.com/auth/drive.file",
                    "link": {"label": "Google Auth Platform - Data access", "url": "https://console.cloud.google.com/auth/scopes"},
                },
                {
                    "text": "On the Clients tab, click Create Client, choose Web application, and add the redirect URI shown below to Authorized redirect URIs, exactly as shown.",
                    "redirect_uri": True,
                    "link": {"label": "Google Auth Platform - Clients", "url": "https://console.cloud.google.com/auth/clients"},
                },
                {
                    "text": "Click Create. Google only shows the Client Secret once, at this moment - copy both the Client ID and Client Secret now.",
                    "link": None,
                },
                {
                    "text": "Also create an API key - the \"Choose folder\" button needs one even though you're already signed in, since Google's folder-picker widget calls the Drive API separately from the OAuth token. Click Create Credentials > API key.",
                    "link": {"label": "Credentials", "url": "https://console.cloud.google.com/apis/credentials"},
                },
                {
                    "text": "Google Cloud now requires picking at least one API restriction for a new key - under API restrictions, choose \"Restrict key\" and select Google Picker API (only - the picker's calls are attributed to this API, not Drive API, so Drive API doesn't need to be added here).",
                    "link": None,
                },
                {
                    "text": "Leave Application restrictions set to None. Google's own docs say restricting by Websites works if you also allow https://docs.google.com/* (the picker itself runs in an iframe hosted there) - but in practice this can still fail with a cryptic \"The API developer key is invalid\" error, since the picker's internal request doesn't always carry a referrer your browser will send. The API restriction above already limits this key to the Picker API, so an unrestricted key here is a reasonable tradeoff.",
                    "link": None,
                },
                {
                    "text": "Copy the API key, then paste the Client ID, Client Secret, and API key into the fields below, save, then click \"Connect Google Drive\". Since the app is published but unverified, Google shows a \"Google hasn't verified this app\" warning on the consent screen - that's expected for your own app; click Advanced, then \"Go to (your app name) (unsafe)\" to continue.",
                    "link": None,
                },
            ],
            "links": [
                {"label": "Google Auth Platform - Clients", "url": "https://console.cloud.google.com/auth/clients"},
                {"label": "Google's guide to creating OAuth credentials", "url": "https://developers.google.com/identity/protocols/oauth2/web-server#creatingcred"},
                {"label": "Credentials - API keys", "url": "https://console.cloud.google.com/apis/credentials"},
            ],
        },
    },
    {
        "id": "onedrive",
        "label": "Microsoft OneDrive",
        "icon": "microsoft-onedrive.svg",
        "description": "Backed up to a dedicated app folder in your personal OneDrive. Uses rclone's own built-in Microsoft app - no Azure account, no app registration. Personal Microsoft accounts only, not work/school (Microsoft 365) accounts.",
        "setup_help": {
            "title": "Connect OneDrive",
            "intro": "Uses rclone's own built-in Microsoft app, so there's no Azure account or app registration to set up - just run one command on any computer with a browser (it doesn't need to be this server).",
            "steps": [
                {
                    "text": "Download rclone (a single binary, no install needed) on any computer with a web browser - your own laptop is fine, it doesn't have to be wherever Backuparr runs.",
                    "link": {"label": "Download rclone", "url": "https://rclone.org/downloads/"},
                },
                {
                    "text": "Open a terminal there and run:",
                    "code": "rclone authorize onedrive",
                    "link": None,
                },
                {
                    "text": "It prints a link - open it in your browser, sign in with your personal Microsoft account, and approve access.",
                    "link": None,
                },
                {
                    "text": "Back in the terminal, rclone prints a block starting with \"Paste the following into your remote machine --->\". Copy that whole block (or just the line in the middle - either works) and paste it below, then click \"Connect OneDrive\".",
                    "link": None,
                },
            ],
            "links": [
                {"label": "rclone downloads", "url": "https://rclone.org/downloads/"},
                {"label": "rclone's remote setup docs", "url": "https://rclone.org/remote_setup/"},
            ],
        },
    },
    {
        "id": "dropbox",
        "label": "Dropbox",
        "icon": "dropbox.svg",
        "description": "Backed up to a Backuparr folder in your Dropbox. Uses rclone's own built-in Dropbox app - no Dropbox developer account, no app registration.",
        "setup_help": {
            "title": "Connect Dropbox",
            "intro": "Uses rclone's own built-in Dropbox app, so there's no app to register - just run one command on any computer with a browser (it doesn't need to be this server).",
            "steps": [
                {
                    "text": "Download rclone (a single binary, no install needed) on any computer with a web browser - your own laptop is fine, it doesn't have to be wherever Backuparr runs.",
                    "link": {"label": "Download rclone", "url": "https://rclone.org/downloads/"},
                },
                {
                    "text": "Open a terminal there and run:",
                    "code": "rclone authorize dropbox",
                    "link": None,
                },
                {
                    "text": "It prints a link - open it in your browser, sign in to Dropbox, and approve access.",
                    "link": None,
                },
                {
                    "text": "Back in the terminal, rclone prints a block starting with \"Paste the following into your remote machine --->\". Copy that whole block (or just the line in the middle - either works) and paste it below, then click \"Connect Dropbox\".",
                    "link": None,
                },
                {
                    "text": "Backups are written to a folder named Backuparr in your Dropbox, created on the first backup.",
                    "link": None,
                },
            ],
            "links": [
                {"label": "rclone downloads", "url": "https://rclone.org/downloads/"},
                {"label": "rclone's Dropbox docs", "url": "https://rclone.org/dropbox/"},
                {"label": "rclone's remote setup docs", "url": "https://rclone.org/remote_setup/"},
            ],
        },
    },
]

def valid_schedule(schedule):
    """Whether cron_schedule is the 5-field cron schedule Settings accepts. One
    saved before an update can stop being one: croniter 7.0 rejects some forms
    6.x took, such as non-ASCII digits (#166)."""
    return isinstance(schedule, str) and len(schedule.split()) == 5 and croniter.is_valid(schedule)


DEFAULTS = {
    "request_headers": [],
    "retention_days": 7,
    "cron_schedule": "0 3 * * *",
    "notify_url": "",
    "bazarr_backup_dir": "",
    # Ids of removed extra instances. Their backups are kept, so an id is
    # never given to another instance.
    "removed_instances": [],
    # Back up Backuparr's own configuration at the end of each full run (#110).
    "self_backup": True,
    # Home Assistant's read-only status routes (#170): on, and the SHA-256 of
    # their bearer token. Set only by /api/ha/token and /api/ha/disable.
    "ha_enabled": False,
    "ha_token_hash": "",
    # Plain HTTP closed, HTTPS only (#177). Set only by /api/https/only;
    # BACKUPARR_HTTPS_ONLY overrides it.
    "https_only": False,
    # Whether the user has confirmed saving the encryption key; until then
    # the web UI shows it and allows nothing else. Set by Backuparr only.
    "secrets_key_acknowledged": False,
    # Whether the "Backups are now encrypted" notice has been seen (#147). A
    # config from before 3.0 has no value, so it shows once; a new install
    # starts with it seen.
    "encryption_notice_acknowledged": False,
    # The first start of 3.0 makes Backuparr's own backup once, as those from
    # before 3.0 no longer restore (#162): whether that's been tried (a config
    # from before 3.0 has no value; a new install starts with it tried), and
    # whether it worked (the notice above then says so). Set by Backuparr only.
    "upgrade_self_backup_tried": False,
    "upgrade_self_backup_made": False,
    "apps": {name: dict(DEFAULT_APP) for name in APP_NAMES},
    "destinations": {name: dict(DEFAULT_DEST[name]) for name in DEST_NAMES},
}

_key_acknowledged = False


def secrets_key_acknowledged():
    """Checked on every API request, so it reads only this one field and
    remembers a yes."""
    global _key_acknowledged
    if not _key_acknowledged:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                _key_acknowledged = json.load(f).get("secrets_key_acknowledged") is True
        except (OSError, ValueError, AttributeError):
            return False
    return _key_acknowledged


def holds_settings():
    """True once Backuparr holds stored secrets, or its key was confirmed
    saved. A lost login is then only replaced with the encryption key (#123):
    without it, deleting auth.json would hand those secrets to whoever sets
    up next. Read from the file itself, not the cached flag; an unreadable
    config.json counts as holding settings."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if secrets_crypto.PREFIX in raw:
        return True
    try:
        return json.loads(raw).get("secrets_key_acknowledged") is True
    except (ValueError, AttributeError):
        return True


def acknowledge_secrets_key():
    global _key_acknowledged
    cfg = load_config()
    cfg["secrets_key_acknowledged"] = True
    save_config(cfg)
    _key_acknowledged = True


def forget_secrets_key_acknowledged():
    """Reset: the next install's key has to be saved again."""
    global _key_acknowledged
    _key_acknowledged = False


def instance_type(instance_id):
    """The app an instance id runs ("radarr" for radarr-4k), or None if
    it isn't a well-formed instance id."""
    if instance_id in APP_NAMES:
        return instance_id
    if not isinstance(instance_id, str) or len(instance_id) > _MAX_INSTANCE_ID_LENGTH:
        return None
    match = _EXTRA_INSTANCE_ID.fullmatch(instance_id)
    return match.group(1) if match else None


def instance_ids(cfg):
    """Every configured instance id, each app's extra instances right after
    its first one."""
    extras = {}
    for instance_id in cfg["apps"]:
        if instance_id not in APP_NAMES and instance_type(instance_id):
            extras.setdefault(instance_type(instance_id), []).append(instance_id)
    return [i for name in APP_NAMES for i in [name, *sorted(extras.get(name, []))]]


def _secret_fields(cfg):
    """(ref, container_dict, key) for every value encrypted at rest - an
    explicit allowlist, not "encrypt everything". `ref` names the field the
    same way across saves, so adding or removing an instance never lines up
    one field's stored value with another."""
    fields = []
    for instance_id in instance_ids(cfg):
        app = cfg["apps"][instance_id]
        fields.append((("apps", instance_id, "api_key"), app, "api_key"))
        if instance_type(instance_id) in PASSWORD_APPS:
            fields.append((("apps", instance_id, "password"), app, "password"))
        for key in SECRET_EXTRA_FIELDS.get(instance_type(instance_id), ()):
            if key in app:  # extra fields aren't in DEFAULT_APP until a card saves one
                fields.append((("apps", instance_id, key), app, key))
    for dest, key in (("gdrive", "client_secret"), ("gdrive", "developer_key"), ("gdrive", "refresh_token"),
                      ("onedrive", "token"), ("dropbox", "token")):
        fields.append((("destinations", dest, key), cfg["destinations"][dest], key))
    fields.append((("notify_url",), cfg, "notify_url"))
    rows = cfg.get("request_headers", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("value"), str) for row in rows):
        raise ConfigError("request_headers must be a list of name/value pairs")
    # A header row is the same field only at the same position with the same name.
    fields.extend((("request_headers", index, row.get("name")), row, "value") for index, row in enumerate(rows))
    return fields


def _merge_defaults(data):
    merged = copy.deepcopy(DEFAULTS)
    for key, value in data.items():
        if key not in ("apps", "destinations"):
            merged[key] = value
    apps = data.get("apps", {})
    for name in APP_NAMES:
        merged["apps"][name].update(apps.get(name, {}))
    for instance_id, app_data in apps.items():
        if instance_id not in APP_NAMES and instance_type(instance_id):
            merged["apps"][instance_id] = {**DEFAULT_INSTANCE, **app_data}
    for name in DEST_NAMES:
        merged["destinations"][name].update(data.get("destinations", {}).get(name, {}))
    return merged


def _stored_secret_fields():
    """The secret fields as config.json holds them now (still encrypted),
    by _secret_fields() ref, or {} if it can't be read."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {ref: (container, key) for ref, container, key in _secret_fields(_merge_defaults(data))}
    except (OSError, ValueError, ConfigError, AttributeError, TypeError):
        return {}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["encryption_notice_acknowledged"] = True  # new: there's no change to tell about
        cfg["upgrade_self_backup_tried"] = True  # nor old backups of itself
        save_config(cfg)
        return cfg

    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{CONFIG_PATH} isn't valid JSON ({exc.msg}, line {exc.lineno}) - fix it, or delete the file to start over with defaults"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH} should hold a JSON object - fix it, or delete the file to start over with defaults")

    merged = _merge_defaults(data)

    # Decrypt in place; a legacy plaintext value gets re-saved encrypted.
    needs_migration = False
    for _ref, container, key in _secret_fields(merged):
        raw = container.get(key, "")
        if raw and not raw.startswith(secrets_crypto.PREFIX):
            needs_migration = True
        container[key] = secrets_crypto.decrypt(raw)

    try:
        validate_request_headers(merged["request_headers"], instance_ids(merged))
    except RequestHeadersError as exc:
        raise ConfigError(str(exc)) from None
    if needs_migration:
        save_config(merged)

    return merged


def save_config(cfg):
    try:
        validate_request_headers(cfg.get("request_headers", []), instance_ids(cfg))
    except RequestHeadersError as exc:
        raise ConfigError(str(exc)) from None
    to_write = copy.deepcopy(cfg)
    stored = _stored_secret_fields()
    kept = 0
    for ref, container, key in _secret_fields(to_write):
        value = container.get(key, "")
        old = stored.get(ref)
        # A secret the current key can't decrypt loads as "" - saving that
        # would erase it for good. Keep it encrypted as it is, unless a new
        # value was entered (or a header row was replaced).
        if not value and old is not None and secrets_crypto.undecryptable(old[0].get(old[1], "")):
            container[key] = old[0][old[1]]
            kept += 1
            continue
        container[key] = secrets_crypto.encrypt(value)
    if kept:
        log.warning("kept %d stored secret(s) that the current encryption key can't decrypt - "
                    "restore the original key (secrets.key or BACKUPARR_SECRETS_KEY) to use them again", kept)

    atomic_file.write_private(CONFIG_PATH, json.dumps(to_write, indent=2))


def enabled_apps(cfg):
    """Enabled instance ids, e.g. ["radarr", "radarr-4k", "sonarr"]."""
    return [name for name in instance_ids(cfg) if cfg["apps"][name].get("enabled")]


# These take an app or an instance id: an instance shares its app's metadata.
def app_meta(name):
    return next((m for m in APP_META if m["id"] == instance_type(name)), None)


def app_label(app_id, app_cfg=None):
    """An app or instance id's name as its card shows it: "Pi-hole",
    "Radarr (4K)" - an instance's own name from its settings, or the end of
    its id."""
    if app_id == SELF_APP["id"]:
        return SELF_APP["label"]
    meta = app_meta(app_id)
    if not meta:
        return app_id
    if instance_type(app_id) == app_id:
        return meta["label"]
    return f"{meta['label']} ({(app_cfg or {}).get('name') or app_id[len(meta['id']) + 1:]})"


def key_required(name):
    return (app_meta(name) or {}).get("key_required", True)


def restore_supported(name):
    """Whether Backuparr restores the app itself. An app with manual_restore
    gets a download and steps on the Restore tab instead (#151)."""
    meta = app_meta(name) or {}
    return not meta.get("manual_restore") and meta.get("restore_supported", True)


def always_encrypted(name):
    return bool((app_meta(name) or {}).get("always_encrypted"))


def encrypts(cfg, name):
    """Whether an app or instance's backups are encrypted (#147): always for an
    always_encrypted app, otherwise unless its card turned it off."""
    return always_encrypted(name) or cfg["apps"].get(name, {}).get("encrypt", True) is not False


def acknowledge_encryption_notice():
    cfg = load_config()
    cfg["encryption_notice_acknowledged"] = True
    save_config(cfg)


def mark_upgrade_self_backup(made=False):
    """Records that the first start of 3.0 tried its self-backup (#162), and
    that it worked when made."""
    cfg = load_config()
    cfg["upgrade_self_backup_tried"] = True
    cfg["upgrade_self_backup_made"] = cfg.get("upgrade_self_backup_made") is True or made
    save_config(cfg)


def enabled_destinations(cfg):
    return [name for name in DEST_NAMES if cfg["destinations"][name].get("enabled")]


def retention_days(cfg, dest_id):
    """How many days a destination keeps backups: its own retention_days,
    or the global one when it has none (#133) - or a hand-edited one that
    isn't a positive whole number."""
    own = cfg["destinations"].get(dest_id, {}).get("retention_days")
    if isinstance(own, int) and not isinstance(own, bool) and own >= 1:
        return own
    return int(cfg.get("retention_days", DEFAULTS["retention_days"]))

