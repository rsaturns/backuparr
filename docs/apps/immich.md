---
title: Immich
parent: Apps
nav_order: 3
---

# Immich

Immich 2.5.0 or newer is backed up over its API: Backuparr has Immich make a
database dump, downloads it, then deletes that dump from Immich. No folder is
mounted.

The backup is **the database only** - users, albums, people and faces, shared
links, settings and API keys. Photos and videos are files in Immich's upload
location and aren't in it; back those up as well, as Immich's [backup
guide](https://docs.immich.app/administration/backup-and-restore/) explains.

## Setting it up

1. Sign in to Immich as an admin and open **Account Settings > API Keys**
   ([Immich's guide](https://docs.immich.app/features/command-line-interface/#obtain-the-api-key)).
2. Click **New API Key** and give it only these permissions: `job.create`,
   `queue.read`, `maintenance`, `backup.download`, `backup.upload` and
   `backup.delete`. Copy the key.
3. On Backuparr's Immich card, enter the URL (for example
   `http://immich:2283`) and the key, then **Test connection**. It names any
   permission the key is missing, and changes nothing in Immich.

For a second Immich, use **Add instance** on the card.

Immich marks its database backup API as alpha, so it can change in a new
Immich release.

## What a backup does

Backuparr starts Immich's **Create Database Dump** job, waits for it, and
downloads the `.sql.gz` it writes. It then deletes that dump, so Backuparr's
backups don't push Immich's own nightly dumps out of its **keep last** count
(**Administration > Settings > Database Dump Settings**). Immich's own dumps
are left alone.

The backup is encrypted when **Encrypt backup** is on (the default). It holds
your users' details and face data, so leave it on.

## Restoring

Immich stays running. Backuparr uploads the backup, and Immich then:

1. goes into maintenance mode and makes a restore point,
2. replaces its database with the backup's and runs its migrations,
3. restarts on the restored database.

Anything added since the backup is gone from the database. That includes
photos uploaded since: their files stay where they are, but the restored
library doesn't show them. Backuparr deletes its uploaded copy afterwards;
the restore point stays in Immich's backups folder.

The restored database has the API keys it was backed up with. If the key on
the card isn't one of them, Backuparr says so: put the old key back on the
card, or create a new one.

If the restore fails:

- **While the backup is replayed:** nothing was changed, because that step
  runs as one transaction. Backuparr takes Immich out of maintenance mode and
  shows Immich's error.
- **Later, during the migrations or Immich's checks:** Immich puts back its
  restore point and stays in maintenance mode. Its log says why, and has the
  link to its [maintenance page](https://docs.immich.app/administration/maintenance-mode/),
  where you can end maintenance mode or restore the uploaded backup again.

### Onto a new install

Restoring onto a new Immich that has no admin yet needs no API key: Backuparr
uses Immich's own **restore from backup** start, the one its setup page offers.

1. Install Immich, and move the old install's `library`, `upload`, `thumbs`,
   `encoded-video`, `profile` and `backups` folders into the new upload
   location first ([Immich's
   steps](https://docs.immich.app/administration/backup-and-restore/#restore-from-onboarding)).
2. Start Immich, but don't create an account.
3. Restore the backup from Backuparr's Restore tab. The card can keep its old
   key: it works again once the database is back.

The [version check](../restoring.md#the-version-check) needs the same Immich
version the backup came from, so install that version first.
