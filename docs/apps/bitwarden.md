---
title: Bitwarden
parent: Apps
nav_order: 2
---

# Bitwarden

This is the official self-hosted Bitwarden server. For Vaultwarden, see the
[Vaultwarden note](vaultwarden.md).

## Setting it up

Bitwarden has no backup API. Backuparr reads a folder you mount into it
instead, and works out from that folder which kind of install you have:

```yaml
    volumes:
      # A standard install: its bwdata folder.
      - /opt/bitwarden/bwdata:/mnt/bitwarden:ro
      # Or Bitwarden lite: the folder lite has at /backups.
      #- /opt/bitwarden-lite/backups:/mnt/bitwarden:ro
```

Then fill in the card: the URL you open Bitwarden at (for example
`https://vault.example.com`) and the folder, `/mnt/bitwarden`. The URL is
only used to read Bitwarden's version (`/api/config`, no login needed).
**Test connection** says which install it found and names the newest
database backup. If Bitwarden uses a self-signed certificate (an option in
both installers), Test connection shows it and you can trust it on the card:
Backuparr then accepts only that certificate.

Backups are always encrypted with your encryption key. They hold the
database and certificate passwords, and the data-protection keys that
Bitwarden encrypts parts of its database with.

## Standard install

Bitwarden's [backup
guide](https://bitwarden.com/help/backup-on-premise/) says to keep the whole
`./bwdata` folder. Backuparr takes all of it except `logs/` and the live
database in `mssql/data`. That covers `env/`, `core/` (attachments,
`aspnet-dataprotection` and licenses), `identity/`, `ssl/` and
`config.yml`.

In place of the live database it takes the newest of the nightly database
backups in `mssql/backups` (`vault_FULL_YYYYMMDD_HHMMSS.BAK`). Bitwarden's
`mssql` container makes one at midnight UTC and keeps them for 30 days.

- **Make one now** if there isn't one yet:
  `docker exec -i bitwarden-mssql /backup-db.sh`.
- **Only a finished backup is taken.** SQL Server writes the `.BAK` in
  place, so Backuparr checks that it ends the way a finished one does. If
  the newest is still being written, or was cut short, it takes the one
  before it and says so in the run log.
- **Permissions:** Backuparr must be able to read every file in `bwdata`.
  Run it as the user that owns `bwdata` (`PUID`/`PGID`), the user you
  installed Bitwarden as.

## Bitwarden lite

Bitwarden lite 2026.10.1 and newer [back themselves
up](https://github.com/bitwarden/self-host/blob/main/bitwarden-lite/backup/README.md)
once a day, into the folder lite has at `/backups`
(`bitwarden-lite_YYYYMMDD_HHMMSS.tar.gz`). Each one holds the database
dump, whichever database lite uses, and lite's `/etc/bitwarden`. Backuparr
copies the newest of them as it is. Mount that backups folder, not
`/etc/bitwarden`.

- **Older lite:** update to 2026.10.1 or newer, and add the `/backups`
  mount to lite's `docker-compose.yml` as its backup guide shows.
- **Include `settings.env`:** lite leaves it out unless you set
  `BACKUP_INCLUDE_SETTINGS=true` (and mount it at `/settings.env`), and it
  holds your installation ID and key and the database password. Backuparr
  warns in the run log while it's missing. Otherwise keep a copy of it
  somewhere else.
- **Permissions:** lite writes its backups readable only by its own user
  (`PUID`, 911 by default). Set `BACKUP_FILE_MODE=640` in lite's
  `settings.env` so its group can read them too, and give lite and Backuparr
  the same `PGID`: set lite's `PGID` to Backuparr's (for example `PGID=100`
  on Unraid), or Backuparr's to lite's (911). Docker's `group_add` isn't
  enough, because Backuparr runs as just its `PUID` and `PGID`. Test
  connection tells you if it can't read them.

## Old backups

If the newest database backup is more than 2 days old, the run log has a
warning: Bitwarden's own nightly backups may have stopped.

## Restoring

Restores are by hand. The Restore tab gives you the download (decrypted)
and these steps.

**Standard install**, following Bitwarden's [backup
guide](https://bitwarden.com/help/backup-on-premise/). This works on the
same server and on a new one; on a new server, the backup brings the
installation ID, certificates and passwords with it, so you don't run
Bitwarden's install.

1. Stop Bitwarden: `./bitwarden.sh stop`. On a new server, download
   `bitwarden.sh` as Bitwarden's [Linux install
   guide](https://bitwarden.com/help/install-on-premise-linux/#install-bitwarden)
   shows, but don't run `./bitwarden.sh install`.
2. As the user Bitwarden runs as, unzip the backup into `bwdata`, replacing
   what's there. On a new server, unzip it into an empty `bwdata` folder next
   to `bitwarden.sh`.
3. Start Bitwarden: `./bitwarden.sh start`. Give it a minute: on a new
   server it makes an empty database first.
4. Restore the database from the backup's `.BAK` file (it's in
   `bwdata/mssql/backups`). The database password is the `SA_PASSWORD`
   in `bwdata/env/mssql.override.env`:

   ```sh
   docker exec -it bitwarden-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P '<SA_PASSWORD>' -C
   1> use master
   2> alter database vault set offline with rollback immediate
   3> restore database vault from disk='/etc/bitwarden/mssql/backups/vault_FULL_<date>.BAK' with replace
   4> alter database vault set online
   5> GO
   1> exit
   ```

5. Restart Bitwarden: `./bitwarden.sh restart`. Then sign in as an
   existing user and open an item to check that the vault decrypts.

**Bitwarden lite**, following lite's [backup
guide](https://github.com/bitwarden/self-host/blob/main/bitwarden-lite/backup/README.md#restoring):

These commands use the service name in lite's own `docker-compose.yml`,
`bitwarden`.

1. Put the downloaded backup in lite's backups folder.
2. Stop lite: `docker compose stop bitwarden`. On a new machine with
   MariaDB, PostgreSQL or SQL Server, start the database instead:
   `docker compose up -d db`.
3. Restore it:
   `docker compose run --rm --no-deps --entrypoint /restore-db.sh bitwarden /backups/<file> --clean --yes`.
4. Start lite with `docker compose up -d`, then sign in as an existing user
   and open an item to check that the vault decrypts.
