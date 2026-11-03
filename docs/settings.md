---
title: Settings
nav_order: 5
---

# Settings

## Prowlarr discovery

Optional. Enter Prowlarr's URL and API key on its card and click **Discover
services**: Backuparr fills in the URLs and API keys of the Radarr, Sonarr and
SABnzbd instances Prowlarr knows, letting you choose when there are several.
Extra Radarr and Sonarr instances it finds can be added with **Add as
instance**.

- Only empty fields are filled, and nothing is saved until you save.
- Prowlarr masks those API keys, so Backuparr reads them from a temporary
  Prowlarr backup, which it then deletes. With Prowlarr on PostgreSQL, or a
  login proxy blocking `/backup/`, only the URLs are filled in.
- The URLs are the ones Prowlarr uses; **Test connection** confirms Backuparr
  can reach them.
- Other apps, including NZBGet, are configured by hand.

## More than one Radarr or Sonarr

Click **Add another Radarr** (or Sonarr) under its card and give the instance
a name, such as `4K` or `Anime`. Each instance has its own settings, backups,
History and Restore entry, and can get its own
[authentication headers](#service-authentication-headers).

- The name also sets the instance's backup folder and archive names: `4K`
  gives `radarr-4k/radarr-4k_<date>_<time>.zip`. Renaming it later keeps the
  folder.
- Removing an instance keeps its backups; they stay in History, and retention
  keeps the newest.
- Up to 10 extra instances per app.

## Service authentication headers

For apps behind an authenticating reverse proxy, add headers under **Settings
→ Service authentication**. They're sent with every request to the apps -
tests, discovery, backups and restores - but never to cloud destinations,
OAuth providers or notification webhooks. Values are encrypted like API keys.

- A header goes to **all apps** unless you open **Apps** under it and pick
  some. One header name can be used on several rows, as long as no app gets it
  twice.
- An app's own authentication wins over a header of the same name - Tdarr's
  and NZBGet's `Authorization`, Jellyfin's `Authorization: MediaBrowser`. Use
  another header name (such as `Proxy-Authorization`) for the proxy.
- A configured `Cookie` is added alongside the app's cookies; a cookie the app
  already set wins.
- Requests to an app with headers must stay on its configured scheme, host and
  port, redirects included, so use its final URL.
  `BACKUPARR_ALLOW_CROSS_HOST_REDIRECTS` doesn't override this.
- **Generate Basic authentication** turns a username and password into a
  `Basic …` value for the header you choose, in your browser; only the header
  is saved.

If the API and backup downloads have different access rules, run a backup as
well as **Test connection**.

