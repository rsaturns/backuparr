---
title: Tdarr
parent: Apps
nav_order: 16
---

# Tdarr

Tdarr 2 and 3 are both supported. Use the server's port (8266), not the web UI's.

- **API key:** needed when Tdarr's authentication is on - the default from
  Tdarr 3. Create one under **Tools > API Keys**. Tdarr 3 asks for an admin
  account first.
- **Restore** wipes each of Tdarr's database collections before refilling it,
  so you're asked to confirm first. The backup must come from the same Tdarr
  version, so a Tdarr 3 backup doesn't go onto Tdarr 2.
- **Tdarr's docs** have no page on backups;
  [Volume mapping](https://docs.tdarr.io/docs/installation/docker/volume-mapping)
  says where its server data lives, its own backups included.
