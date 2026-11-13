---
title: UniFi
parent: Apps
nav_order: 20
---

# UniFi

Backuparr backs up UniFi wherever it runs: on **UniFi OS** (UniFi OS
Server, and consoles such as a Dream Machine, Cloud Gateway or Cloud Key)
and the older standalone **UniFi Network Server**. The backups hold Wi-Fi
passwords, VPN keys and admin logins, so they're always encrypted.

**Test connection** finds which kind of install it is, and the card then
shows the options it has.

- **UniFi OS** - the card's **Backup** option:
  - **Everything** (the default): UniFi OS's own backup, the `.unifi` file
    from **Settings > Control Plane > Backups > Download**. It holds UniFi
    OS, its admins and every app's settings (Network, Protect, Access and
    the rest), but not UniFi Network's statistics history.
  - **Network only**: UniFi Network's own backup, a `.unf` file, with as
    much statistics history as **History** says. For the statistics too,
    add a second UniFi instance for the same install with Network only.
- **UniFi Network Server**: UniFi Network's `.unf` backup, with **History**.

**History** picks how much of UniFi Network's statistics goes into a `.unf`:
**Settings only** (the default: small and quick), the last **7**, **30**,
**90** or **365 days**, or **Everything**. Settings only restores the whole
setup; the statistics only bring back past graphs and reports, and they're
most of the file (on one small network: 76 KB for settings only, 5.5 MB
with 7 days, 7.2 MB with a year).

Ubiquiti's guide: [Backups and Migration in
UniFi](https://help.ui.com/hc/en-us/articles/360008976393).

## Setting it up

Backuparr signs in as a **local admin** - not a UI.com account, which signs
in with two-factor authentication. Give it an admin of its own, with the
role the backup needs:

- **UniFi OS, Everything:** a **Super Admin**. UniFi OS lets no lesser role
  download its backup - not even one with Network and UniFi OS user
  management - and Ubiquiti counts a Super Admin as equal to the Owner, so
  keep its password long and Backuparr's alone.
- **UniFi OS, Network only:** an admin with only **Network: Full
  Management**. It has no rights over UniFi OS or its other apps.
- **UniFi Network Server:** the admin its setup made. Only a Super Admin may
  make a backup, and its **Admins** page adds only site admins (Site
  Administrator, Hotspot Manager, View Only), which can't.

To add the admin on UniFi OS, open **People** in UniFi Network (the people
icon in the sidebar), click **Create New > Create New User**, tick **Admin**
and enter a username (for example `backuparr`) and a long random password.
Either pick the **Super Admin** role, or untick **Use a Predefined Role**
and set the first role (Network) to **Full Management** and the second one
to **None**. Then **Create**. The admin is local only: it signs in without
a UI.com account. Ubiquiti's guides: [UniFi Roles
Explained](https://help.ui.com/hc/en-us/articles/1500011491541) and [Adding
Admins in UniFi](https://help.ui.com/hc/en-us/articles/28692158912279).

Then, on Backuparr's UniFi card, enter the URL - UniFi's own address with
its port: `https://<console>` for a console, port `11443` for UniFi OS
Server, `8443` for UniFi Network Server - the username and the password,
and **Test connection**. It checks the admin may make the backup chosen.
The password is encrypted at rest.

UniFi OS counts sign-ins: after many in a few minutes it refuses even the
right password for about a minute. Backuparr signs in once per backup and
never retries, and says so when it's refused.

## The certificate

UniFi uses a self-signed certificate unless you've given it your own. The
first **Test connection** shows it (name and expiry, and the SHA-256
fingerprint on the **?** next to SHA-256): click **Trust** and **Save**. It
works as for [OPNsense](opnsense.md#the-certificate): a changed certificate
stops backups until you trust the new one. A certificate from a trusted CA
needs nothing.

## Restoring

Pick UniFi and a backup on Backuparr's **Restore** tab: it shows a
**Download** button (the backup comes back decrypted, as a `.unifi` or
`.unf` file) and these steps. A backup restores into the same versions or
newer ones, so update a new install first.

**UniFi OS** (both files):

1. Download the backup.
2. On a new install, finish the setup first (**Proceed Without a UI
   Account** works) and update it.
3. Open **Settings > Control Plane > Backups** and click **Upload &
   Restore**.
4. Choose the downloaded file. For a `.unifi` file, leave **Restore All
   Applications and Settings** ticked.
5. Click **Restore**. UniFi restarts with the backup's settings; a `.unifi`
   brings back its admins too.

UniFi OS's setup also offers **Restore From Backup**, but that one needs a
UI.com account.

**UniFi Network Server:**

1. Download the backup.
2. On a new install, click **Restore Server from a Backup** on the setup's
   first page, choose the downloaded file and click **Confirm**.
3. Otherwise, open **Settings > System > Backups**, click **Restore**,
   choose the file and click **Restore**.

UniFi Network Server keeps its settings in MongoDB, and a restore goes
through a `<database>_restore` database. If a restore fails and UniFi's
`server.log` says `not authorized on unifi_restore`, its MongoDB user lacks
that database: the linuxserver.io image added it to its [MongoDB
setup](https://github.com/linuxserver/docker-unifi-network-application#setting-up-your-external-database)
in 2025, for new installs.

If devices don't come back online on a new install, see Ubiquiti's
[migration guide](https://help.ui.com/hc/en-us/articles/360008976393). The
download is decrypted: delete it once UniFi is back.
