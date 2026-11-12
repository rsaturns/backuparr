---
title: Radarr, Sonarr and Lidarr
parent: Apps
nav_order: 13
---

# Radarr, Sonarr, Lidarr, Whisparr and Prowlarr

Backuparr asks the app to make a backup over its API, then downloads the file
from the app's web UI, which doesn't accept the API key. If the app asks every
address to log in, **Test connection** says so and shows **Username** and
**Password** on the card. Restores go through the API and work either way.

Each app's own guide to its backups and restores:
[Radarr](https://wiki.servarr.com/radarr/faq#how-do-i-backuprestore-radarr),
[Sonarr](https://wiki.servarr.com/sonarr/faq#how-do-i-backuprestore-my-sonarr),
[Lidarr](https://wiki.servarr.com/lidarr/system#backup),
[Whisparr](https://wiki.servarr.com/whisparr/faq#how-do-i-backuprestore-whisparr) and
[Prowlarr](https://wiki.servarr.com/prowlarr/faq#how-do-i-backuprestore-prowlarr).

Pick one of the two options below.

## Option 1: enter the app's login

Type the username and password you use to sign in to the app, then click
**Test connection** again. It should say `signed in`. Backuparr finds out by
itself whether the app uses Forms (a login page) or Basic, and only sends the
login to the app's own address, to download the backup.

- A wrong username or password is tried once per backup, not retried. The app
  logs each failed attempt, so check the card if you use fail2ban.
- If you change the app's password, update it on the card too.

## Option 2: let Backuparr through without a login

In each app, open **Settings > General > Security** and set **Authentication
Required** to **Disabled for Local Addresses**. It's listed once
**Authentication** is Forms or Basic.

Backuparr reaches the app from a private address (its Docker network or your
LAN), so it counts as local. Anyone else on your local network can then open
the app without logging in too; from outside it still asks.

- **Allowed Hosts:** if you fill it in, include the name in the URL on the
  app's card in Backuparr, such as `sonarr` for `http://sonarr:8989`.
  Otherwise the app turns Backuparr away.
- **Through a reverse proxy:** if Backuparr reaches the app through a proxy,
  add the proxy's address to **Trusted Networks**. Since the 2026 security fix
  (CVE-2026-30975), Radarr, Sonarr, Lidarr and Prowlarr only count a proxied
  request as local when the proxy is trusted.
- **External** authentication (set in `config.xml` when a proxy handles the
  login) works as it is.
