---
title: Synology DSM
parent: Apps
nav_order: 18
---

# Synology DSM

**This backs up DSM's settings, not your data.** Backuparr makes the same
file as DSM's own **Configuration Backup** (Control Panel > Update & Restore
> Configuration Backup): a `.dss` holding DSM's settings, such as users,
groups, shared folder settings and permissions, file services, network,
security, notifications and Task Scheduler. The files in your shared folders,
the `homes` folder, packages and their data, and Hyper Backup tasks are **not**
in it. Back those up with Hyper Backup or another backup of the NAS itself.

It works on DSM 7 and DSM 6.2, with the same Web API calls DSM's Control
Panel uses, and was tested on DSM 7.2.2 and 6.2.4. Synology's guide:
[Configuration Backup (DSM 7)](https://kb.synology.com/en-global/DSM/help/DSM/AdminCenter/system_configbackup?version=7)
([DSM 6](https://kb.synology.com/en-global/DSM/help/DSM/AdminCenter/system_configbackup?version=6)).

DSM can back the same file up on its own only to your Synology Account: one
copy with no versions, deleted after 180 days without an upload. Backuparr
keeps dated copies at every destination you use. The file holds your users
and their settings, so it's always encrypted.

## Setting it up

DSM lets only administrators export its configuration, so the card takes the
**username and password of an account in the administrators group**. Make one
just for Backuparr (Control Panel > User & Group > Create, then add it to
`administrators`). The password is encrypted at rest; Backuparr uses it only
to sign in, make the backup and sign out.

On Backuparr's Synology DSM card, enter DSM's address with its port - for
example `https://192.168.1.20:5001`, or `http://192.168.1.20:5000` if DSM
doesn't redirect HTTP to HTTPS - and the account, then **Test connection**.
It signs in, reads DSM's version and checks the account can export the
configuration, without making a backup.

DSM blocks an address after several failed sign-ins (**Auto Block**, in
Control Panel > Security). Backuparr signs in once per backup and never
retries. On DSM 6, an account that isn't an administrator is refused like a
wrong password, so check both when Backuparr says DSM refused the sign-in.

## 2-factor sign-in

An account with 2-factor authentication works if its second step is a
**verification code (OTP)** - see Synology's
[2-Factor Authentication](https://kb.synology.com/en-global/DSM/help/DSM/SecureSignIn/2factor_authentication?version=7).

1. **Test connection**: Backuparr says DSM asks for a 2FA code and shows a
   **2FA code** field.
2. Enter the current code from the account's authenticator app and **Test
   connection** again.
3. **Save**. DSM now remembers Backuparr as a trusted device, so scheduled
   backups sign in without a code. The code isn't saved; the device token DSM
   gives is, encrypted like the password.

If you remove Backuparr from the account's trusted devices in DSM (Personal >
Security > Account Activity on DSM 7), the next backup fails with "Synology
asked for a new 2FA code. Run Test connection on its card." Do steps 1-3
again.

**Approve sign-in** and **hardware security keys** work only for web
sign-ins, so Backuparr can't use them: set up OTP for the account too.

**Adaptive MFA** (DSM 7.2): DSM may ask an administrator without 2FA for a
second step when it signs in from a device DSM doesn't know - and Backuparr
always looks like one. It only happens to an account with a verified email
address or that has signed in to a Synology mobile app (Synology's
[Account security](https://kb.synology.com/en-global/DSM/help/DSM/AdminCenter/connection_security_account?version=7)
help). An account made just for Backuparr has neither; if DSM still asks,
set up OTP for the account as above.

## The certificate

DSM serves HTTPS with a self-signed certificate until you add your own. The
first **Test connection** shows it (name and expiry, and the SHA-256
fingerprint on the **?** next to SHA-256): click **Trust** and **Save**. It
works as for [OPNsense](opnsense.md#the-certificate): a changed certificate
stops backups until you trust the new one. A certificate from a trusted CA
(such as DSM's Let's Encrypt one) needs nothing.

## Restoring

Pick Synology DSM and a backup on Backuparr's **Restore** tab: it shows a
**Download** button (the backup comes back decrypted, as a `.dss`) and these
steps. Restore onto the same DSM major version the backup came from.

**DSM 7:**

1. Download the backup.
2. In DSM, open **Control Panel > Update & Restore > Configuration Backup**.
   Under **Restore DSM Configurations**, click **Restore**.
3. Choose **Restore from a configuration file on your computer**, browse to
   the `.dss` and click **Next**.
4. Tick the settings to restore, and **Overwrite conflicting settings** to
   replace users, groups, shared folders and services that exist now. Click
   **Done**, then **Yes**. DSM stops its services while it restores, about a
   minute.

**DSM 6.2:** in **Control Panel > Update & Restore > Configuration Backup**,
click **Restore configuration**, browse to the `.dss`, tick the settings to
restore (and **Overwrite conflicting settings** if you want), then **OK** and
**Yes**.

Afterwards, as Synology notes:

- Verify the email recipients in **Control Panel > Notification** again.
- The password of the account you restore with isn't changed, so you stay
  signed in.
- Users or groups whose names DSM now reserves are restored under other names.

The download holds DSM's users and settings: delete it once DSM is back.
