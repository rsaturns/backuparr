---
title: Home Assistant
parent: Apps
nav_order: 4
---

# Home Assistant

Home Assistant 2025.5 or newer is backed up over its own backup API: Backuparr
has Home Assistant make a backup, downloads it, then deletes that copy from
Home Assistant. No folder is mounted, and Home Assistant's own backups and
schedule are left alone.

## Setting it up

1. Sign in to Home Assistant as an administrator, open your profile's
   **Security** tab and create a **long-lived access token**
   ([Home Assistant's guide](https://www.home-assistant.io/docs/authentication/#managing-account-access)).
   Every backup command is for administrators only, so the token must be an
   administrator's.
2. On Backuparr's Home Assistant card, enter the URL (for example
   `http://homeassistant:8123`) and the token, then **Test connection**.
3. Pick what to back up. Test connection finds the install type and shows
   the options it has.

For a second Home Assistant, use **Add instance** on the card. If Home
Assistant serves HTTPS with a self-signed certificate, Test connection shows
it and you trust it on the card, as for [OPNsense](opnsense.md).

## What to back up

The options are the ones on Home Assistant's own backup page
([what each one holds](https://www.home-assistant.io/common-tasks/general/backups/#what-a-backup-contains-and-where-its-stored)):

| Option | Install types |
|---|---|
| Settings (always) | All |
| History (on by default) | All |
| Media | HA OS and Supervised |
| Share folder | HA OS and Supervised |
| Local apps folder | HA OS and Supervised |
| Apps: none, all or chosen ones | HA OS and Supervised |

History is only the default database. One in MariaDB or PostgreSQL isn't in
the backup; on HA OS, back up its app instead.

The backup has no Home Assistant password: Backuparr always encrypts it
itself. It holds `secrets.yaml` and every integration's tokens.

## Restoring

Backuparr uploads the backup and Home Assistant restores what it holds, then
restarts. It's offline for a few minutes. Backuparr waits until it's back,
reads Home Assistant's own result, and deletes the uploaded copy.

- **Container and Core** restore the settings and history.
- **HA OS and Supervised** also restore the backup's apps and folders.

A backup from HA OS or Supervised restores only onto HA OS or Supervised:
on Container or Core, Home Assistant would start with HA OS's settings,
including its port, so Backuparr refuses it. A backup from Container restores
onto HA OS, the move Home Assistant supports.

The restore also brings back the backup's network settings, its port
included. If Home Assistant answers on another port afterwards (HA OS uses
80, Container 8123), update its URL in Settings.

The restore brings back Home Assistant's users and tokens as they were in the
backup. A token made after the backup no longer works afterwards, and the
Restore tab says **Restored. Make a new token.** Put the new one on the card.
The uploaded copy is then left in Home Assistant, under **Settings > System >
Backups**, for you to delete.

### Onto a new install

A new Home Assistant that hasn't been set up yet needs no token: Backuparr
restores through its first screen's **Restore from backup**, as Home
Assistant's own
[restore steps](https://www.home-assistant.io/common-tasks/general/backups/#restoring-a-backup)
do. Install Home Assistant, don't create an account, and restore from
Backuparr's Restore tab. The card can keep its old token: it works again once
the backup is back.

The [version check](../restoring.md#the-version-check) needs the same Home
Assistant version the backup came from, so install that version first.
