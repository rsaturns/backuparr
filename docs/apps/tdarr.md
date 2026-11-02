---
title: Tdarr
parent: Apps
nav_order: 9
---

# Tdarr

## API auth token

If Tdarr has an API auth token enabled, put it in the optional API key field
on its card (it's sent as `Authorization: Bearer <key>`). Leave it blank if
Tdarr has no auth (the default).

## Restore

A restore wipes each of Tdarr's database collections before refilling it, so
you're asked to confirm first.
