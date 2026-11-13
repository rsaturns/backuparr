---
title: Tdarr
parent: Apps
nav_order: 20
---

# Tdarr

## Setting it up

On Tdarr's card, enter the URL of Tdarr's **server** - for example
`http://192.168.1.10:8266` - not its web UI's port. Tdarr 2 and 3 are both
supported.

When Tdarr's authentication is on - the default from Tdarr 3 - the card also
needs an API key: create one under **Tools > API Keys** (Tdarr 3 asks for an
admin account first). Click **Test connection**.

## Good to know

- **Restore** wipes each of Tdarr's database collections before refilling it,
  so you're asked to confirm first. The backup must come from the same Tdarr
  version, so a Tdarr 3 backup doesn't go onto Tdarr 2.
- **Tdarr's docs** have no page on backups;
  [Volume mapping](https://docs.tdarr.io/docs/installation/docker/volume-mapping)
  says where its server data lives, its own backups included.
