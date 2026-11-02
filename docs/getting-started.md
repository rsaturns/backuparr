---
title: Getting started
nav_order: 2
---

# Getting started

Add a `backuparr` service to the compose file that already defines your apps,
so it shares their network and can reach them by name.

## Pull the published image (recommended)

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
      # Changing this also means updating the ports: mapping below.
      #- WEBUI_PORT=8990
    ports:
      - "8990:8990"
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
```

```sh
docker compose up -d
```

Or without Compose:

```sh
docker run -d --name backuparr --restart unless-stopped \
  -e TZ=America/Los_Angeles -e PUID=1000 -e PGID=1000 \
  -p 8990:8990 \
  -v /share/Container/backuparr:/config/backuparr \
  rsaturns/backuparr:latest
```

Published for `linux/amd64` and `linux/arm64`. `latest` is the newest tagged
release; pin a version (e.g. `rsaturns/backuparr:1.10.1`) to control
upgrades. Tags are on [Docker Hub](https://hub.docker.com/r/rsaturns/backuparr).

The optional settings are explained under
[Environment variables](environment-variables.md), and the extra volumes in the
notes for [Bazarr](apps/bazarr.md), [Jellyfin](apps/jellyfin.md),
[Emby](apps/emby.md), [Plex](apps/plex.md) and [Seerr](apps/seerr.md).

## Or build from source

Clone the [repository](https://github.com/rsaturns/backuparr) and use its
`docker-compose.yml` (or replace the `image:` line above with `build: .`):

```sh
docker compose up -d --build backuparr
```

## First run

Open `http://<host>:8990` and create the admin account on the setup screen.
Then, on the **Settings** tab:

1. Switch on each app to back up, enter its URL (e.g. `http://radarr:7878` on
   the same Compose network, or `IP:port` for apps on host networking) and its
   API key, and click **Test connection**. Using Prowlarr? Set it up first and
   click **Discover services** - see [Prowlarr discovery](settings.md#prowlarr-discovery).
2. Enable at least one [destination](destinations.md).
3. Set retention and a schedule. Retention deletes each app's backups older
   than the number of days you set, but always keeps its newest one.
4. Click **Save settings**. Schedule changes apply without a restart.

**Run & Status** runs a backup now - of every app, or just one - and shows it
live, **History** lists, downloads and deletes backups, and **Restore** restores them.
