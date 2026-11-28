---
title: Unraid
parent: Getting started
nav_order: 2
---

# Unraid

On the **Apps** tab, search for **Backuparr** and click **Install**. The
[template](https://github.com/rsaturns/backuparr/blob/main/templates/backuparr.xml)
installs the same image, with appdata in `/mnt/user/appdata/backuparr` and
`PUID`/`PGID` set to Unraid's `99`/`100`. Fill in the folders of the apps that
need one (leave the others empty) and click **Apply**. Unraid passes its own
timezone as `TZ`. Apps are reached at your server's IP and each app's port,
e.g. `http://192.168.1.10:7878`.

Then go through the [first run](first-run.md).

## The template's fields

What each field on Unraid's install page does, in the order Unraid shows
them. A required field can't be left empty. A default is what the template
fills in; a field without one starts empty.

### Container

| Field | Required | What it does |
|---|---|---|
| **Name** | Yes | The container's name.<br>**Default:** `backuparr` |
| **Repository** | Yes | The image. `latest` follows full releases; a version tag such as `rsaturns/backuparr:3.0.2` stays put (see [Updating](updating.md)).<br>**Default:** `rsaturns/backuparr:latest` |
| **Network Type** | Yes | With Bridge, the web UI is reached at your server's IP on the two ports below.<br>**Default:** Bridge |
| **Use Tailscale** | No | Unraid's own option to put the container on your Tailscale network. Backuparr doesn't need it.<br>**Default:** Off |
| **Console shell command** | No | The shell the container's **Console** opens.<br>**Default:** Bash |
| **Privileged** | No | Leave it off: Backuparr needs no extra access to the server.<br>**Default:** Off |

Unraid's **Advanced View** switch, top right, shows more of its own fields,
such as **WebUI** (where the container's **WebUI** link goes: the HTTPS
port), **Extra Parameters**, **Post Arguments** and **CPU Pinning**.
Backuparr needs none of them changed.

### Ports and folders

| Field | Required | What it does |
|---|---|---|
| **WebUI** | Yes | The server's port for the web UI over plain HTTP.<br>**Default:** `8990` |
| **WebUI HTTPS** | Yes | The server's port for the web UI over [HTTPS](../security.md#https), with a self-signed certificate: your browser warns the first time.<br>**Default:** `8991` |
| **Appdata** | Yes | Backuparr's settings, encrypted credentials and keys. With Local Storage, the backups too, in its `backups` folder. Back it up, or keep a cloud destination on.<br>**Default:** `/mnt/user/appdata/backuparr` |
| **Jellyfin backups folder** | Optional | Needed to back up [Jellyfin](../apps/jellyfin.md): the folder Jellyfin writes its backups to. Read/write. |
| **Emby config folder** | Optional | Needed to back up [Emby](../apps/emby.md): Emby's `/config` folder. Read only; to restore from the web UI, **Edit** it and set **Access Mode** to **Read/Write**. |
| **Plex data folder** | Optional | Needed to back up [Plex](../apps/plex.md): its `Plex Media Server` folder. Read only; set **Read/Write** to restore from the web UI. |
| **Seerr config folder** | Optional | Needed to back up [Seerr](../apps/seerr.md): Seerr's `/app/config` folder. Read only; set **Read/Write** to restore from the web UI. |
| **LazyLibrarian config folder** | Optional | Needed to back up [LazyLibrarian](../apps/lazylibrarian.md): its `/config` folder. Read/write, so each backup's file is removed from it and restores work. |
| **Bitwarden folder** | Optional | Needed to back up [Bitwarden](../apps/bitwarden.md): a standard install's `bwdata` folder, or Bitwarden lite's `/backups` folder. Read only is enough. |
| **Vaultwarden data folder** | Optional | Needed to back up [Vaultwarden](../apps/vaultwarden.md): its `/data` folder. Read only; set **Read/Write** to restore from the web UI. |
| **Bazarr backup folder** | Optional | Needed only to restore [Bazarr](../apps/bazarr.md): its backup folder. Read/write. Its backups don't need it. |

Most folders' descriptions on the install page give an example path.

### Show more settings

These are under **Show more settings**. See
[Environment variables](../environment-variables.md) for more on each.

| Field | Required | What it does |
|---|---|---|
| **PUID** | Yes | The user the container runs as.<br>**Default:** `99`, Unraid's `nobody` |
| **PGID** | Yes | The group it runs as.<br>**Default:** `100`, Unraid's `users` |
| **UMASK** | Optional | Permissions of new files. `077` lets only that user read backups; `022` lets other users read them too.<br>**Default:** `077` |
| **LOG_LEVEL** | Optional | How much backup and restore runs log. `DEBUG` logs more.<br>**Default:** `INFO` |
| **BACKUPARR_<wbr>FORCE_<wbr>HTTPS** | Optional | `true` only behind your own HTTPS reverse proxy. Leave it empty on plain HTTP, or login fails. |
| **BACKUPARR_<wbr>HTTPS_<wbr>ONLY** | Optional | `true` closes plain HTTP, so only the HTTPS port answers; `false` keeps it open. Empty leaves it to the **HTTPS only** switch on the Backuparr card in Settings. |
| **BACKUPARR_<wbr>TLS_<wbr>CERT** | Optional | Your own certificate in place of the self-signed one: a PEM file's path inside the container, e.g. `/config/backuparr/cert.pem`. Needs `BACKUPARR_TLS_KEY`. |
| **BACKUPARR_<wbr>TLS_<wbr>KEY** | Optional | The certificate's unencrypted PEM key, e.g. `/config/backuparr/key.pem`. |
| **BACKUPARR_<wbr>TRUSTED_<wbr>PROXY** | Optional | With `BACKUPARR_FORCE_HTTPS`: your reverse proxy's IP address, so the login lockout sees each visitor's own address. |
| **BACKUPARR_<wbr>DISABLE_<wbr>AUTH** | Optional | `true` turns off Backuparr's [login](../security.md#login): only when an authenticating reverse proxy protects the whole web UI and its ports can't be reached any other way. |
| **BACKUPARR_<wbr>SECRETS_<wbr>KEY** | Optional | Your [encryption key](../security.md#your-encryption-key) in place of the generated `secrets.key`. It encrypts your backups and the credentials in Backuparr's settings. Leave it empty to use the generated one. |
| **RCLONE_<wbr>CONFIG_<wbr>PASS** | Optional | Replaces the generated password that encrypts `rclone.conf`, rclone's settings for the cloud destinations. Leave it empty to use the generated one. |

Other [environment variables](../environment-variables.md), such as
`RCLONE_BWLIMIT`, can be added with **Add another Path, Port, Variable, Label
or Device** at the bottom of the page.
