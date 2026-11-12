---
title: Docker
parent: Getting started
nav_order: 1
---

# Docker

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
release; pin a version (e.g. `rsaturns/backuparr:3.0.0-beta`) to control
upgrades. Tags are on [Docker Hub](https://hub.docker.com/r/rsaturns/backuparr),
and the same images are on GitHub Container Registry as
[`ghcr.io/rsaturns/backuparr`](https://github.com/rsaturns/backuparr/pkgs/container/backuparr).

The optional settings are explained under
[Environment variables](../environment-variables.md), and the extra volumes in the
notes for [Bazarr](../apps/bazarr.md), [Jellyfin](../apps/jellyfin.md),
[Emby](../apps/emby.md), [Plex](../apps/plex.md), [Seerr](../apps/seerr.md),
[LazyLibrarian](../apps/lazylibrarian.md) and [Vaultwarden](../apps/vaultwarden.md).

Then go through the [first run](first-run.md).
