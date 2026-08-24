#!/usr/bin/env python3
"""Restore one app from its latest (or a named) Google Drive backup.

Usage:
    python restore.py radarr
    python restore.py sonarr --file sonarr_20260101_030000.zip
    python restore.py bazarr --bazarr-backup-dir /mnt/bazarr-config/backup
    python restore.py tdarr --yes
    python restore.py sabnzbd

Radarr/Sonarr/Prowlarr restore purely over the API (multipart upload) - the
app restarts itself once restored. Bazarr needs a local path to its own
backup folder (see README); defaults to bazarr_backup_dir from config.json
if set there. Tdarr wipes and repopulates its database collections over the
API - this is destructive, so it asks for confirmation unless --yes is
given. SABnzbd restores everything except categories/RSS/sorters over the
API too, prompting interactively for each Usenet server's password
(SABnzbd's API never returns the real value - see apps/sabnzbd.py); run
this from a real terminal so the prompts work, or use --yes to skip both
confirmation and password prompts (servers are then left without a
password, to be set manually in Settings > Servers).

Settings (URLs, API keys, rclone remote) come from config.json - the same
config the web UI edits - not environment variables.
"""
import argparse
import getpass
import os
import shutil
import sys

import restore_actions as ra
from config_store import key_required, load_config


def confirm(message, assume_yes):
    if assume_yes:
        return True
    reply = input(f"{message} [y/N] ")
    return reply.strip().lower() == "y"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("app", choices=["radarr", "sonarr", "prowlarr", "bazarr", "tdarr", "sabnzbd"])
    parser.add_argument("--file", help="Specific backup filename to restore (default: newest)")
    parser.add_argument("--bazarr-backup-dir", help="Local path to Bazarr's config/backup folder (required for bazarr)")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    args = parser.parse_args()

    cfg = load_config()
    rclone_remote = cfg.get("rclone_remote")
    if not rclone_remote:
        raise SystemExit("rclone_remote is not configured (set it via the web UI or config.json)")
    app_cfg = cfg["apps"].get(args.app, {})
    if not app_cfg.get("url") or (key_required(args.app) and not app_cfg.get("api_key")):
        raise SystemExit(f"{args.app} is not configured (set its URL/API key via the web UI or config.json)")

    print(f"Downloading {args.app} backup ...")
    tmp_dir, local_zip, filename = ra.fetch_backup(rclone_remote, args.app, args.file)
    print(f"  -> {filename}")

    try:
        if args.app in ra.UPLOAD_RESTORE_APPS:
            if not confirm(f"Restore {args.app} from {filename}? This restarts {args.app}.", args.yes):
                print("Aborted.")
                return 1
            ra.restore_upload_app(args.app, app_cfg, local_zip)
            print(f"{args.app}: restore uploaded successfully, app is restarting.")

        elif args.app == "bazarr":
            backup_dir = args.bazarr_backup_dir or cfg.get("bazarr_backup_dir")
            if not backup_dir:
                raise SystemExit(
                    "bazarr restore requires --bazarr-backup-dir (or bazarr_backup_dir in config.json) - "
                    "a local path to its config/backup folder"
                )
            if not confirm(f"Restore bazarr from {filename}? This restarts bazarr.", args.yes):
                print("Aborted.")
                return 1
            ra.restore_bazarr(app_cfg, local_zip, backup_dir)
            print("bazarr: restore triggered successfully, app is restarting.")

        elif args.app == "tdarr":
            if not confirm(
                "Restore tdarr? This WIPES each database collection before repopulating it from the backup.",
                args.yes,
            ):
                print("Aborted.")
                return 1
            ra.restore_tdarr(app_cfg, tmp_dir, local_zip)
            print("tdarr: restore complete.")

        elif args.app == "sabnzbd":
            config = ra.load_sabnzbd_config(tmp_dir, local_zip)
            servers = config.get("config", {}).get("servers", [])
            if not confirm(
                f"Restore sabnzbd config ({len(servers)} server(s) plus misc settings)? "
                "You'll be prompted for each Usenet server's password. Categories/RSS "
                "feeds/sorters are not auto-restored.",
                args.yes,
            ):
                print("Aborted.")
                return 1

            def prompt_password(name, server):
                if args.yes or not sys.stdin.isatty():
                    print(f"sabnzbd: skipping password prompt for server '{name}' (no TTY or --yes given)")
                    return None
                host = server.get("host", "?")
                try:
                    return getpass.getpass(f"Password for SABnzbd server '{name}' ({host}) (blank to skip): ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    return None

            summary = ra.restore_sabnzbd(app_cfg, config, prompt_password)

            print(
                f"sabnzbd: restored {len(summary['servers_restored'])} server(s): "
                f"{', '.join(summary['servers_restored']) or 'none'}"
            )
            print(f"sabnzbd: restored {len(summary['misc_keys_restored'])} misc setting(s)")
            if summary["servers_missing_password"]:
                print(
                    "sabnzbd: NO PASSWORD SET for: "
                    f"{', '.join(summary['servers_missing_password'])} "
                    "- set these manually in Settings > Servers"
                )
            if summary["sections_skipped"]:
                print(f"sabnzbd: not auto-restored, recreate by hand: {', '.join(summary['sections_skipped'])}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
