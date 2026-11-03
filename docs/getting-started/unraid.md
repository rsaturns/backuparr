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
