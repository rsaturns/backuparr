---
title: Bazarr
parent: Apps
nav_order: 1
---

# Bazarr

## Backup downloads and Bazarr's web auth

Bazarr's backup download follows its **Settings > General > Security**, not
the API key:

- `None` - works as is.
- `Basic` - fill in the Basic auth username and password on Bazarr's card.
- `Forms` - not supported; switch to `None` or `Basic`, or authenticate at
  your reverse proxy.

## Restore

A restore needs Bazarr's config/backup folder mounted into Backuparr:

```yaml
    volumes:
      - /share/Container/bazarr/backup:/mnt/bazarr-backup
```

Enter it under **Bazarr backup folder** on the Restore tab, unless it's set in
Settings.
