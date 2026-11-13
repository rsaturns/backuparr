---
title: QNAP QuTS hero
parent: Apps
nav_order: 14
---

# QNAP QuTS hero

**This backs up the NAS's settings, not your data.** Backuparr makes the same
file as QuTS hero's **Download to Computer** (Control Panel > System > Backup /
Restore > Backup/Restore Settings): a `.bin` system configuration file holding
the NAS's general settings (system, network, file service and server
settings), users and groups, shared folder settings and privileges, network
settings and a record of the apps installed from App Center. The data in your
shared folders, app settings (including myQNAPcloud), the power recovery
setting, Wi-Fi and USB QuickAccess are **not** in it. Back the data up with
Hybrid Backup Sync or another backup of the NAS itself.

It works on **QuTS hero h5.2 and later**, with the same calls QuTS hero's
Control Panel makes, and was tested on QuTS hero h5.2.11. QNAP's guide:
[Backing up system settings](https://docs.qnap.com/operating-system/quts-hero/5.2.x/en-us/backing-up-system-settings-4CB1653B.html).
QTS has the same page, but Backuparr takes only QuTS hero for now: Test
connection says so on a NAS running QTS.

QuTS hero can save the same file on a schedule, but only to a folder on the
NAS itself, and it keeps at most 10. Backuparr makes a fresh file on every
run - the NAS's own schedule can stay off - and keeps dated copies at every
destination you use. The file holds your users and their settings, so it's
always encrypted.

## Setting it up

QuTS hero lets only administrators and accounts with the **System Management**
role export its settings - no other role, and no ordinary user, can. Make an
account just for Backuparr and give it one of them:

- **System Management role** (recommended): Control Panel > Privilege >
  Delegated Administration, select **System Management**, tick the account and
  click **Apply** - see QNAP's
  [Delegated Administration](https://docs.qnap.com/operating-system/quts-hero/5.2.x/en-us/delegated-administration-87EDB7A6.html).
  The account stays out of the `administrators` group, can't manage that group
  or its members, and has no SSH or Telnet.
- Or add the account to the `administrators` group.

On Backuparr's QNAP QuTS hero card, enter the NAS's address - for example
`https://192.168.1.30` (QNAP's HTTPS port is 443 unless you changed it in
Control Panel > System > General Settings) - and the account's username and
password, then **Test connection**. It signs in, reads the NAS's version and
checks the account can export the settings, without making a backup. The
password is encrypted at rest; Backuparr uses it only to sign in, make the
backup and sign out.

QuTS hero blocks an address after several failed sign-ins (**Network Access
Protection**, in Control Panel > System > Security). Backuparr signs in once
per backup and never retries.

## 2-step verification

An account with 2-step verification works with its **security code** - the
six-digit code from an authenticator app, which every account with 2-step
verification has (QNAP's
[2-step verification](https://docs.qnap.com/operating-system/quts-hero/5.2.x/en-us/2-step-verification-E6CF0C20.html)).

1. **Test connection**: Backuparr says the NAS asks for a 2FA code and shows a
   **2FA code** field.
2. Enter the current code from the account's authenticator app and **Test
   connection** again.
3. **Save**. QuTS hero now remembers Backuparr's sign-in, so scheduled backups
   sign in without a code. The code isn't saved; the token QuTS hero gives is,
   encrypted like the password.

QuTS hero stops accepting that token when the account's password changes.
The next backup then fails with "QNAP asked for a new 2FA code. Run Test
connection on its card." Enter the new password, then do steps 1-3 again.
Changing the password is also how to revoke Backuparr's sign-in.

Backuparr signs in with the security code. QNAP's other methods - QR code,
login approval and online verification code - are for signing in at the NAS's
login page.

## The certificate

QuTS hero serves HTTPS with a self-signed certificate until you add your own.
The first **Test connection** shows it (name and expiry, and the SHA-256
fingerprint on the **?** next to SHA-256): click **Trust** and **Save**. It
works as for [OPNsense](opnsense.md#the-certificate): a changed certificate
stops backups until you trust the new one. A certificate from a trusted CA
(such as one from myQNAPcloud or Let's Encrypt) needs nothing.

## Restoring

Pick QNAP QuTS hero and a backup on Backuparr's **Restore** tab: it shows a
**Download** button (the backup comes back decrypted, as QNAP's `.bin`) and
these steps, from QNAP's
[Restoring system settings](https://docs.qnap.com/operating-system/qts/5.2.x/en-us/restoring-system-settings-FD94EF48.html):

1. Download the backup.
2. On a NAS running QuTS hero, the same version as the backup or newer, open
   **Control Panel > System > Backup / Restore > Backup/Restore Settings**.
   Under **Restore System Settings**, click **Restore**.
3. Choose **Upload file from computer**, browse to the `.bin` and click
   **Next**.
4. On each page - **General**, **Users / Groups**, **Shared Folders**,
   **Network** and **Apps** - choose what to restore, or click **Skip** to keep
   the NAS's own.
5. On **Summary**, click **Restore**, confirm with your password and click
   **OK**. The NAS restores the settings and restarts.

QNAP's rules for a restore:

- The NAS must run QuTS hero (a backup from QuTS hero doesn't restore onto
  QTS), the same version as the backup or newer.
- To restore network settings, it needs at least as many network ports as
  the NAS the backup came from.
- Users and groups in the backup replace ones of the same name. Skipping
  users and groups skips shared folders too.
- Shared folders that aren't in the backup are removed (their data stays on
  the disks), and apps that aren't in the backup are removed. Encrypted, ISO
  and snapshot shared folders aren't restored.

The download holds the NAS's users and settings: delete it once the NAS is
back.
