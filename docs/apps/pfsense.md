---
title: pfSense
parent: Apps
nav_order: 7
---

# pfSense

pfSense (CE and Plus) has no official API, so Backuparr signs in to its web
UI and downloads the configuration from **Diagnostics > Backup & Restore**,
the way Netgate documents for [scripted
backups](https://docs.netgate.com/pfsense/en/latest/backup/remote-backup.html).
`config.xml` holds the firewall's whole setup, private keys and password
hashes included, so pfSense's backups are always encrypted.

## Setting it up

Give Backuparr a user of its own:

1. In pfSense, open **System > User Manager > Users** and click **Add**.
   Pick a username (for example `backuparr`) and a long random password.
   Save.
2. Edit the user, and under **Effective Privileges** click **Add**. Pick
   **WebCfg - Diagnostics: Backup & Restore** (the download) and **WebCfg -
   System: Login / Logout / Dashboard** (the dashboard, for the version
   recorded with each backup). Save.
3. On Backuparr's pfSense card, enter the URL (for example
   `https://192.168.1.1`), the username and the password, then **Test
   connection**.

pfSense's guide: [User Management and
Privileges](https://docs.netgate.com/pfsense/en/latest/usermanager/index.html).

pfSense has no read-only backup privilege: the Backup & Restore page also
restores a configuration, so pfSense warns that this privilege is as
powerful as an administrator's. That's why the user is Backuparr's alone and
its password is encrypted at rest. Keep the dashboard's **System
Information** widget on that user's dashboard: the version comes from it.
pfSense blocks an address after repeated failed logins, so a wrong password
is reported once and not retried.

## The certificate

pfSense's web UI uses a self-signed certificate. The first **Test
connection** shows it (name, expiry, SHA-256 fingerprint); compare it with
**System > Certificates** in pfSense, then click **Trust** and **Save**. It
works as for [OPNsense](opnsense.md#the-certificate): a changed certificate
stops backups until you trust the new one, and the failure links to the card.

## Restoring

Pick pfSense and a backup on Backuparr's **Restore** tab: it shows a
**Download** button (the backup comes back decrypted, as an `.xml` file) and
these steps.

**Firewall still running** ([Netgate's guide](https://docs.netgate.com/pfsense/en/latest/backup/restore.html)):

1. Download the backup.
2. In pfSense, open **Diagnostics > Backup & Restore**.
3. Under **Restore Backup**, leave **Restore area** at **All**, choose the
   downloaded file and click **Restore Configuration**. Leave **Configuration
   file is encrypted** unticked: the download is already decrypted. The
   firewall reboots.

**New install** ([Netgate's guide](https://docs.netgate.com/pfsense/en/latest/backup/restore-during-install.html)):

1. Download the backup and rename it `config.xml`.
2. On a USB stick formatted FAT or FAT32 (with a partition, not exFAT), make
   a folder named `config` and put `config.xml` in it.
3. Install pfSense, then plug in the stick and boot the firewall from its own
   disk. The External Configuration Locator copies the configuration from
   the stick at boot. (The installer's **Configuration Restore** option can
   read it from the stick too.)
4. Remove the stick once the firewall is up: otherwise pfSense copies it
   again at every boot, and changes made since are lost.

The download is decrypted: delete it once the firewall is back.
