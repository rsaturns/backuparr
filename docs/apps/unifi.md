---
title: UniFi Network
parent: Apps
nav_order: 17
---

# UniFi Network

Backuparr backs up the UniFi Network app wherever it runs: on **UniFi OS**
(UniFi OS Server, and consoles such as a Dream Machine, Cloud Gateway or
Cloud Key) and the older standalone **UniFi Network Server**. It makes the
same Network backup (a `.unf` file) as **Settings > Control Plane > Backups
> Download** does - Wi-Fi networks and passwords, VLANs, firewall rules,
port profiles and adopted devices, plus as much statistics history as you
choose. The file holds Wi-Fi passwords, VPN keys and admin logins, so
UniFi Network's backups are always encrypted.

It isn't the console's own full "UniFi OS" backup: that one also holds
UniFi OS and its other apps (Protect, Access and so on), and has no API.
Ubiquiti's guide: [Backups and Migration in
UniFi](https://help.ui.com/hc/en-us/articles/360008976393).

## Setting it up

Backuparr signs in as a **local admin** - not a UI.com account, which signs
in with two-factor authentication. Give it an admin of its own:

- **UniFi OS:** in UniFi Network, open **People** (the people icon in the
  sidebar), click **Create New > Create New User**, tick **Admin** and
  enter a username (for example `backuparr`) and a long random password.
  Untick **Use a Predefined Role**, set the first role (Network) to **Full
  Management** and the second one to **None**, then **Create**. The admin
  is local only: it signs in without a UI.com account.
- **UniFi Network Server:** use the admin its setup made. Only a Super Admin
  may make a backup, and its **Admins** page adds only site admins (Site
  Administrator, Hotspot Manager, View Only), which can't.

Then, on Backuparr's UniFi Network card, enter the URL - UniFi's own
address with its port: `https://<console>` for a console, port `11443` for
UniFi OS Server, `8443` for UniFi Network Server - the username and the
password, and **Test connection**. It finds out which kind of install it
is, and checks the admin may make backups. Ubiquiti's guide: [Adding Admins
in UniFi](https://help.ui.com/hc/en-us/articles/28692158912279).

On UniFi OS, Network: Full Management is the least UniFi allows for a
backup - **Site Admin**, **View Only** and **Hotspot Operator** can't make
one - and gives no rights over UniFi OS or its other apps. The password is
encrypted at rest.

**History** picks how much statistics history goes into the backup:
**Settings only** (the default: small and quick), the last **7**, **30**,
**90** or **365 days**, or **Everything**. Settings only restores the whole
setup; the statistics only bring back past graphs and reports.

UniFi OS counts sign-ins: after many in a few minutes it refuses even the
right password for about a minute. Backuparr signs in once per backup and
never retries, and says so when it's refused.

## The certificate

UniFi uses a self-signed certificate. The first **Test connection** shows
it (name, expiry, SHA-256 fingerprint): click **Trust** and **Save**. It
works as for [OPNsense](opnsense.md#the-certificate): a changed certificate
stops backups until you trust the new one. A certificate from a trusted CA
needs nothing.

## Restoring

Pick UniFi Network and a backup on Backuparr's **Restore** tab: it shows a
**Download** button (the backup comes back decrypted, as a `.unf` file) and
these steps. A backup restores into the same UniFi Network version or a
newer one, so update a new install first.

**UniFi OS:**

1. Download the backup.
2. On a new install, finish the setup first (**Proceed Without a UI
   Account** works) and update UniFi Network.
3. In UniFi Network, open **Settings > Control Plane > Backups** and click
   **Upload & Restore**.
4. Choose the downloaded file and click **Restore**. UniFi Network restarts
   with the backup's settings.

UniFi OS's setup also offers **Restore From Backup**, but that one needs a
UI.com account and takes the console's own backups.

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
