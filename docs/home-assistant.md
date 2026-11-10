---
title: Home Assistant
nav_order: 11
---

# Home Assistant

Home Assistant can read whether your backups worked, for a dashboard, a
notification or an automation. Backuparr only answers questions: Home
Assistant can't change settings, run a backup or restore through it.

## Turning it on

1. On the **Settings** tab, open **Server** and switch on **Enable Home
   Assistant integration** on the Backuparr card.
2. Copy the **token** it shows. It's shown only this once; Backuparr keeps
   just a hash of it.
3. Give the token to Home Assistant (see the example below).

**New token** makes another one, and the old one stops working. Switching the
integration off removes the token. Both take effect straight away, without
**Save settings**.

The token is sent as `Authorization: Bearer <token>`. It opens only the two
addresses below, and it's needed even when Backuparr's own login is turned
off with `BACKUPARR_DISABLE_AUTH`. A wrong token counts towards the same
lockout as a wrong password. While the integration is off, both addresses
answer 404.

## What Backuparr answers

`GET /api/ha/status` gives everything at once:

```json
{
  "ok": false, "total": 17, "succeeded": 15, "failed": 1, "running": false, "version": "3.0.0",
  "apps": {
    "radarr": {"name": "Radarr", "ok": true, "last_run": "2026-11-10T03:01:12+00:00",
               "last_success": "2026-11-10T03:01:12+00:00", "error": null},
    "sonarr-4k": {"name": "Sonarr (4K)", "ok": false, "last_run": "2026-11-10T03:02:40+00:00",
                  "last_success": "2026-11-09T03:02:31+00:00", "error": "couldn't reach the app"}
  }
}
```

- `apps` holds every enabled app and instance, and `backuparr` (Backuparr's
  own backup) while it's on. Each key is the app's id, as on its card.
- An app's `ok` is its latest run, as on the Overview: `true`, `false`, or
  `null` before its first backup. A backup that failed on one destination
  counts as failed.
- The top-level `ok` is `true` only when every one of them succeeded.
- `error` is the short reason the Overview shows. It never holds a URL or a
  key.
- `last_run` and `last_success` are UTC times, or `null`.
- `running` is `true` while a backup runs.

`GET /api/ha/status/<app>` gives one app's part, for example
`/api/ha/status/sonarr-4k`.

## Example: a REST sensor

Put the token in `secrets.yaml`:

```yaml
backuparr_auth: "Bearer <your token>"
```

Then in `configuration.yaml`, with Backuparr's address:

{% raw %}
```yaml
rest:
  - resource: http://192.168.1.10:8990/api/ha/status
    headers:
      Authorization: !secret backuparr_auth
    scan_interval: 300
    binary_sensor:
      - name: Backuparr backups
        device_class: problem
        value_template: "{{ not value_json.ok }}"
    sensor:
      - name: Backuparr failed backups
        value_template: "{{ value_json.failed }}"
```
{% endraw %}

`binary_sensor.backuparr_backups` turns on when any backup failed, ready for a
notification automation. It also turns on when Home Assistant can't read the
status, for example after **New token** until the new token is in
`secrets.yaml`.
