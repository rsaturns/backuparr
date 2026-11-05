---
title: Radarr, Sonarr and Lidarr
parent: Apps
nav_order: 8
---

# Radarr, Sonarr, Lidarr, Whisparr and Prowlarr

Backuparr asks the app to make a backup over its API, then downloads the file
from the app's web UI, which doesn't accept the API key. If the app asks every
address to log in, the download fails and **Test connection** says so.
Restores go through the API and work either way.

## Let Backuparr through

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
