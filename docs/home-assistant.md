---
title: Home Assistant
nav_order: 11
---

# Home Assistant

Home Assistant can read whether your backups worked, for a dashboard, a
notification or an automation. Backuparr only answers questions: Home
Assistant can't change settings, run a backup or restore through it.

This page is about Home Assistant reading Backuparr. To back up Home Assistant
itself, see the [Home Assistant app note](apps/homeassistant.md).

## The Backuparr integration

The [Backuparr integration](https://github.com/rsaturns/ha-backuparr) sets
everything up from Home Assistant's UI. It needs Home Assistant 2026.10 or
later.

[![Open your Home Assistant instance and open the Backuparr repository.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rsaturns&repository=ha-backuparr&category=integration)

1. Select the badge to open the integration in Home Assistant's
   **Marketplace** (or HACS before 2026.11), and select **Download**.
2. [Turn on](#turning-it-on) the integration in Backuparr and copy the token.
3. In Home Assistant, go to **Settings** > **Devices & services** > **Add
   integration**, pick **Backuparr**, and enter Backuparr's address (like
   `https://192.168.1.10:8991`) and the token.
4. Home Assistant shows the fingerprint of Backuparr's
   [certificate](security.md#https). Compare it with the one on the Backuparr
   card (the **?** next to SHA-256) and submit if they match.

It adds a Backuparr device with an **All backups** problem sensor, one for
each app and for Backuparr's own backup, a **Failed backups** count and,
switched off at first, the time of each app's last backup. Apps you add or
remove in Backuparr follow. See the
[integration's README](https://github.com/rsaturns/ha-backuparr#readme).

## Turning it on

1. On the **Settings** tab, open **Server** and switch on **Enable Home
   Assistant integration** on the Backuparr card. The **?** beside it opens
   the [Backuparr integration](#the-backuparr-integration)'s page.
2. Copy the **token** it shows. It's shown only this once; Backuparr keeps
   just a hash of it.
3. Give the token to Home Assistant: in the [Backuparr integration](#the-backuparr-integration),
   or a [REST sensor](#example-a-rest-sensor) of your own.

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

Then in `configuration.yaml`, with Backuparr's address. Over HTTPS (port
8991), Home Assistant can't check Backuparr's self-signed certificate, so
`verify_ssl: false` is needed, which also accepts anyone pretending to be
Backuparr. The Backuparr integration for Home Assistant pins the certificate
instead.

{% raw %}
```yaml
rest:
  - resource: https://192.168.1.10:8991/api/ha/status
    verify_ssl: false
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
