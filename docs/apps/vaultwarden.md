---
title: Vaultwarden
parent: Apps
nav_order: 13
---

# Vaultwarden

Vaultwarden has no backup API, so Backuparr reads **Vaultwarden's data
folder** (its `/data`):

```yaml
    volumes:
      - /share/Container/vaultwarden/data:/mnt/vaultwarden:ro   # drop :ro to restore from the web UI
```

Then fill in the card: the URL (for example `http://vaultwarden`, the address
Backuparr reaches it on - no login is needed) and **Data folder** =
`/mnt/vaultwarden`. The URL is only used for Vaultwarden's version and, before
a restore, to check that it's stopped.

- **What's in the backup:** what [Vaultwarden's backup
  guide](https://github.com/dani-garcia/vaultwarden/wiki/Backing-up-your-vault)
  lists - the database (users, organisations, every vault item, devices,
  2FA), the `rsa_key` files that sign logins, `config.json` (the admin page's
  settings, including the admin token and SMTP password) and the
  `attachments` and `sends` folders. The icon cache and `tmp` are left out;
  Vaultwarden rebuilds them.
- **Always encrypted:** `config.json` and the RSA key are plain text in
  Vaultwarden's folder, so its backups are always encrypted with your
  [encryption key](../security.md#your-encryption-key).
- **Only SQLite is supported** (Vaultwarden's default). With PostgreSQL or
  MySQL, the database isn't in the folder; dump it as Vaultwarden's guide
  describes.
- **Restore needs Vaultwarden stopped** (`docker stop vaultwarden`); Backuparr
  refuses while it's running. It replaces the database and writes back the
  backup's other files, then you start Vaultwarden. Files the backup doesn't
  have (an attachment added since, say) are left alone. The backup must come
  from the same Vaultwarden version.
- **Restoring from the web UI needs write access** to Vaultwarden's files.
  Vaultwarden runs as root by default, so its files are root's; run it as
  Backuparr's `PUID`/`PGID` instead (see Vaultwarden's [non-root
  guide](https://github.com/dani-garcia/vaultwarden/wiki/Running-docker-container-with-non-root-user)),
  or restore by hand. Test connection says when Backuparr can't write them.
- **Rebuilding:** start the new Vaultwarden once, so it creates its database,
  stop it, then restore.
- **To restore by hand:** download the backup from History (it comes back
  decrypted) and unzip it. Stop Vaultwarden, put `db.sqlite3` in its data
  folder (deleting any `db.sqlite3-wal` and `db.sqlite3-shm` there) along with
  the `rsa_key` files, `config.json`, `attachments` and `sends`, then start
  Vaultwarden.
