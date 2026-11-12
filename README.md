<img src="webui/static/logo.png" alt="Backuparr logo" width="160">

# Backuparr

**Everything you host, backed up.**

> **AI disclosure:** This project was built with substantial assistance
> from Claude (Anthropic) - code, documentation, and commit history
> included. Reviewed and maintained by a human.

Scheduled config/database backups for Radarr, Sonarr, Lidarr, Whisparr,
Prowlarr, Profilarr, Bazarr, Tdarr, SABnzbd, NZBGet, Tautulli, Jellyfin, Emby,
Plex, Seerr, Immich, LazyLibrarian, Pi-hole, Vaultwarden, OPNsense, pfSense,
UniFi, Home Assistant and Unraid, sent to every destination you enable: Local storage, Google Drive, OneDrive, and
Dropbox. Everything - apps, destinations,
schedule, retention and restores - is set up in the web UI, not env vars.

Most apps are backed up through their own API. Jellyfin, Emby, Plex, Seerr,
LazyLibrarian and Vaultwarden also need a folder mounted into Backuparr - see
the [app notes](https://rsaturns.github.io/backuparr/apps/).

**[Documentation](https://rsaturns.github.io/backuparr/)** · [Changelog](CHANGELOG.md)

If you like Backuparr, give it a ⭐ star - it helps the project grow.

## Screenshots

<table>
  <tr>
    <td><img src="docs/screenshots/overview.png" alt="Overview tab: enabled services, destinations, schedule and retention, a line saying all 23 backups succeeded, and each app's last backup"><br><b>Overview</b></td>
    <td><img src="docs/screenshots/settings.png" alt="Settings tab: apps grouped by category on the left, each category folding away under a header with its enabled count, Server open on Backuparr's own card, and destinations on the right"><br><b>Settings</b></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/run.png" alt="Run and Status tab: a backup in progress with its live log"><br><b>Run &amp; Status</b></td>
    <td><img src="docs/screenshots/history.png" alt="History tab: each category's apps, Backuparr's own backups under Server, with each backup's version, size, date and whether it's encrypted"><br><b>History</b></td>
  </tr>
</table>

## Quick start

### Docker

Add a `backuparr` service to the compose file that already defines your apps,
so it shares their network and can reach them by name.

```yaml
services:
  backuparr:
    image: rsaturns/backuparr:latest
    container_name: backuparr
    restart: unless-stopped
    environment:
      - TZ=America/Los_Angeles
      - PUID=1000
      - PGID=1000
      # Optional - see Environment variables.
      #- LOG_LEVEL=INFO
      #- BACKUPARR_FORCE_HTTPS=true
      #- BACKUPARR_DISABLE_AUTH=true
      #- BACKUPARR_SECRETS_KEY=
      #- RCLONE_CONFIG_PASS=
      #- BACKUPARR_HTTPS_ONLY=true
      # Changing these also means updating the ports: mapping below.
      #- WEBUI_PORT=8990
      #- WEBUI_HTTPS_PORT=8991
    ports:
      - "8990:8990"   # HTTP
      - "8991:8991"   # HTTPS, self-signed certificate
    volumes:
      # config.json, rclone.conf, and local-destination backups.
      - /share/Container/backuparr:/config/backuparr
      # Optional: needed only to restore Bazarr - path to its config/backup folder.
      #- /share/Container/bazarr/backup:/mnt/bazarr-backup
      # Needed to back up Jellyfin - its backups folder (see the Jellyfin note).
      #- /share/Container/jellyfin/config/data/backups:/mnt/jellyfin-backups
      # Needed to back up Emby - its /config folder (see the Emby note).
      #- /share/Container/emby/config:/mnt/emby:ro
      # Needed to back up Plex - its "Plex Media Server" folder (see the Plex note).
      #- "/share/Container/plex/Library/Application Support/Plex Media Server:/mnt/plex:ro"
      # Needed to back up Seerr - its /app/config folder (see the Seerr note).
      #- /share/Container/seerr/config:/mnt/seerr:ro
      # Needed to back up LazyLibrarian - its /config folder (see the LazyLibrarian note).
      #- /share/Container/lazylibrarian/config:/mnt/lazylibrarian
      # Needed to back up Vaultwarden - its /data folder (see the Vaultwarden note).
      #- /share/Container/vaultwarden/data:/mnt/vaultwarden:ro
```

```sh
docker compose up -d
```

Or without Compose:

```sh
docker run -d --name backuparr --restart unless-stopped \
  -e TZ=America/Los_Angeles -e PUID=1000 -e PGID=1000 \
  -p 8990:8990 -p 8991:8991 \
  -v /share/Container/backuparr:/config/backuparr \
  rsaturns/backuparr:latest
```

Published for `linux/amd64` and `linux/arm64`. `latest` is the newest tagged
release; pin a version (e.g. `rsaturns/backuparr:3.0.0`) to control
upgrades. Tags are on [Docker Hub](https://hub.docker.com/r/rsaturns/backuparr),
and the same images are on GitHub Container Registry as
[`ghcr.io/rsaturns/backuparr`](https://github.com/rsaturns/backuparr/pkgs/container/backuparr).

The optional settings are covered in
[Docker](https://rsaturns.github.io/backuparr/getting-started/docker/), and
building the image yourself in
[Build from source](https://rsaturns.github.io/backuparr/getting-started/build-from-source/).

### Unraid

On the **Apps** tab, search for **Backuparr** and click **Install**. The
template uses the same image, with appdata in `/mnt/user/appdata/backuparr`;
fill in the folders of the apps that need one - see
[Unraid](https://rsaturns.github.io/backuparr/getting-started/unraid/).

### First run

Open `https://<host>:8991` (or `http://<host>:8990`) and create the admin account on the setup screen
(or [restore Backuparr](https://rsaturns.github.io/backuparr/restoring/#restoring-backuparr-itself) from its own
backup). Save the encryption key Backuparr then shows - you need it to restore
Backuparr. Then, on the **Settings** tab:

1. Switch on each app to back up, enter its URL (e.g. `http://radarr:7878` on
   the same Compose network, or `IP:port` for apps on host networking) and its
   API key, and click **Test connection**. Using Prowlarr? Set it up first and
   click **Discover services** - see [Prowlarr discovery](https://rsaturns.github.io/backuparr/settings/#prowlarr-discovery).
2. Enable at least one [destination](https://rsaturns.github.io/backuparr/destinations/).
3. Set retention and a schedule. Retention deletes each app's backups older
   than the number of days you set, but always keeps its newest one.
4. Click **Save settings**.

**Run & Status** runs a backup now - of every app, or just one - and shows it
live, **History** lists, downloads and deletes backups, and **Restore** restores them.

## Documentation

- [Features](https://rsaturns.github.io/backuparr/features/) - everything Backuparr does, in one list
- [Getting started](https://rsaturns.github.io/backuparr/getting-started/)
- [Apps](https://rsaturns.github.io/backuparr/apps/) - how each app is backed up and restored
- [Destinations](https://rsaturns.github.io/backuparr/destinations/) - Local Storage, Google Drive, OneDrive, Dropbox
- [Settings](https://rsaturns.github.io/backuparr/settings/) - Prowlarr discovery, multiple instances of an app, reverse-proxy headers
- [Backups and history](https://rsaturns.github.io/backuparr/backups/) - schedule, retention, run logs, History
- [Restoring](https://rsaturns.github.io/backuparr/restoring/) - apps, the version check, and Backuparr itself
- [Login and security](https://rsaturns.github.io/backuparr/security/)
- [Notifications](https://rsaturns.github.io/backuparr/notifications/)
- [Home Assistant](https://rsaturns.github.io/backuparr/home-assistant/) - the [Backuparr integration](https://github.com/rsaturns/ha-backuparr) and a read-only status
- [Environment variables](https://rsaturns.github.io/backuparr/environment-variables/) and the [configuration reference](https://rsaturns.github.io/backuparr/configuration/)

Want to help? See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## Credits

App icons (`webui/static/icons/`) are from the [selfh.st icon
collection](https://selfh.st/icons/) ([selfhst/icons on
GitHub](https://github.com/selfhst/icons)), licensed
[CC BY 4.0](https://github.com/selfhst/icons/blob/main/LICENSE).

## License

[GNU AGPLv3](LICENSE) - if you run a modified version as a network service,
its source must be available to its users too.
