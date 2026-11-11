---
title: Pi-hole
parent: Apps
nav_order: 10
---

# Pi-hole

Pi-hole v6 or newer is backed up over its API with Teleporter, the same
archive as Pi-hole's **Settings > Teleporter > Export**. No folder is mounted.
Pi-hole's backups are always encrypted, because the archive holds its
password hashes and 2FA secret.

## Setting it up

Pi-hole's card needs an app password, and Pi-hole must let app passwords use
Teleporter:

1. In Pi-hole, switch Settings to **Expert** (the toggle at the top of any
   Settings page).
2. **Settings > Web interface / API > Configure app password**: click
   **Enable new app password** and copy it. Pi-hole shows it only once.
3. **Settings > All settings > Webserver and API**: turn on
   `webserver.api.app_sudo`, then **Save & Apply**. With Docker you can set
   `FTLCONF_webserver_api_app_sudo: "true"` in its environment instead.
4. On Backuparr's Pi-hole card, enter the URL (for example
   `http://192.168.1.2`, without `/admin`) and the app password, then
   **Test connection**.

Use `http://`. Pi-hole's own `https://` certificate is self-signed, which
Backuparr refuses.

An app password never asks for a 2FA code. Each backup signs in, exports,
and logs out again, so it doesn't use up one of Pi-hole's API sessions.

For a second Pi-hole, use **Add instance** on the card.

## What's in a backup

Pi-hole's settings (`pihole.toml`, local DNS records included), adlists,
allowed and denied domains, groups, clients and DHCP leases. The domains
downloaded from adlists aren't included: Pi-hole fetches them again on its
next gravity update (**Tools > Update Gravity** runs one now).

## Restoring

A restore replaces Pi-hole's settings with the backup's, including its web
password, app password and 2FA. Settings forced by an environment variable
(`FTLCONF_...`) stay as they are. Pi-hole restarts to apply the backup, which
takes a second or two. Backuparr then signs in again and tells you if:

- the app password on the card no longer works, because the backup brought
  back the app password it was made with. Put that one on the card, or make
  a new one in Pi-hole.
- the backup turned `webserver.api.app_sudo` off, so backups would fail. Turn
  it on again.

You can restore one Pi-hole's backup onto another, for example to rebuild a
secondary from the primary. It brings everything across, including the
primary's passwords and its DHCP settings.

The [version check](../restoring.md#the-version-check) compares Pi-hole's FTL
version, which is shown at the bottom of Pi-hole's web page (for example
`FTL v6.8`).
