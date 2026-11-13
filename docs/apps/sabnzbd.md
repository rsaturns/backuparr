---
title: SABnzbd
parent: Apps
nav_order: 15
---

# SABnzbd

## Setting it up

On SABnzbd's card, enter its URL - for example `http://sabnzbd:8080` - and
its **API Key**, from SABnzbd's **Config > General > Security** (not the NZB
Key, which can only add downloads). Click **Test connection**. SABnzbd's guide
to these settings: [General](https://sabnzbd.org/wiki/configuration/5.1/general).

Backuparr reads and writes SABnzbd's settings over its API. If Backuparr
reaches SABnzbd from outside SABnzbd's local network, **External internet
access** on the same page must allow the full API.

## Backups and restores

- **What's in the backup:** every setting SABnzbd's API gives (`get_config`):
  folders, Usenet servers, categories, RSS feeds, sorters, schedules and
  notifications. SABnzbd hides every password in it as `**********`, so the
  Usenet servers' passwords aren't in the backup. The queue and history
  aren't either.
- **Restore** writes the settings back over the API and asks for each Usenet
  server's password; leave one blank to set it in SABnzbd later. Categories,
  RSS feeds and sorters aren't restored - set them up again in SABnzbd - and
  SABnzbd's API key and NZB key stay as they are.
