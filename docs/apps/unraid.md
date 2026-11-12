---
title: Unraid
parent: Apps
nav_order: 18
---

# Unraid

Unraid keeps its whole setup on its boot device: array and pool layout,
shares, users, network, Docker templates, plugins, VM settings and the
license key. Backuparr backs that up with Unraid's own **Boot Device
Backup** (on **Main**, click the boot device, then **Boot Device Backup**),
the zip you'd download by hand. It works the same on a USB flash drive and
on internal boot (Unraid 7.3 and later), and was tested on Unraid 7.3.5 and
7.4.0.

The zip is kept as Unraid makes it: everything on the boot device except the
previous Unraid release Unraid keeps for downgrades, so about 1.2 GB with
Unraid's own files. That's what lets the USB Flash Creator restore it in one
step; a zip of only the `config` folder doesn't boot. It holds the license,
the root and share password hashes, SSH keys and WireGuard keys - more than
Unraid Connect's flash backup, which leaves passwords and keys out - so it's
always encrypted. Array data, appdata and VMs aren't on the boot device and
aren't in it.

Unraid's guides: [Backing up the boot
device](https://docs.unraid.net/unraid-os/system-administration/secure-your-server/secure-your-boot-drive/)
and [Changing the boot
device](https://docs.unraid.net/unraid-os/system-administration/maintain-and-update/changing-the-flash-device).

## Setting it up

Unraid's web UI has only the `root` user, and neither its API nor a mounted
folder offers the boot device to anything else (the boot device can be read
only by root), so the card takes the **root password**. It's encrypted at
rest, and Backuparr uses it only to sign in to the web UI, make the backup
and sign out.

On Backuparr's Unraid card, enter the URL of Unraid's web UI - the address
you open it at, for example `http://192.168.1.10` - and the root password,
then **Test connection**. Backuparr can run on the Unraid server itself or
anywhere that reaches its web UI. If Unraid redirects to another address
(for example a `myunraid.net` one), enter that address instead.

Unraid ignores sign-ins from an address for 15 minutes after 3 wrong
passwords. Backuparr signs in once per backup and never retries, and says
so when it's refused.

Up to Unraid 7.3, Unraid makes the zip on the server before it's downloaded,
and needs free space for it - about 1.5 times what the boot device uses - in
memory or on a share such as `system`. With the array stopped and little
memory free, that fails, as Unraid's own button does; Backuparr says so.
From 7.4 the zip is streamed and needs no space on the server.

Backuparr itself needs room for the zip and its encrypted copy while a
backup runs - about 2.5 GB in its container's temporary folder - and each
kept backup takes about 1.2 GB at every destination, so pick the retention
with that in mind.

## The certificate

With **Use SSL/TLS** set to **Yes** (**Settings > Management Access**), Unraid
serves its web UI with a self-signed certificate. The first **Test
connection** shows it (name and expiry, and the SHA-256 fingerprint on the
**?** next to SHA-256): click **Trust** and **Save**. It works as for
[OPNsense](opnsense.md#the-certificate): a changed certificate stops backups
until you trust the new one. A `myunraid.net` certificate is from a trusted CA
and needs nothing.

## Restoring

Pick Unraid and a backup on Backuparr's **Restore** tab: it shows a
**Download** button (the backup comes back decrypted, as a `.zip`) and
these steps.

**A new or replacement drive:**

1. Download the backup.
2. In the [Unraid USB Flash
   Creator](https://docs.unraid.net/unraid-os/getting-started/set-up-unraid/create-your-bootable-media/),
   choose **Use custom**, pick the downloaded `.zip` and write it to the USB
   flash drive.
3. Boot the server from it. On a new drive, move the license to it with
   **Tools > Registration > Replace Key** - see Unraid's [Changing the boot
   device](https://docs.unraid.net/unraid-os/system-administration/maintain-and-update/changing-the-flash-device)
   for the limits.
4. Check the disk assignments on **Main** before starting the array.

A backup made while the array was running restores as an array that wasn't
stopped cleanly: **Main** says "Unclean shutdown detected", and Unraid checks
parity when the array starts, as after a power cut.

**Internal boot:** restore the backup to a USB flash drive as above and boot
from it, then switch to internal boot again (**Tools > Internal Boot**).
Unraid copies the restored setup to the internal drive. Unraid's [Internal
Boot FAQ](https://docs.unraid.net/unraid-os/getting-started/set-up-unraid/internal-boot-faq/)
describes the same.

**Single files:** open the `.zip` and copy what you need from its `config`
folder back to the same place on the boot device (the `flash` share, or
`/boot/config`), then reboot.

The download is decrypted and holds the server's passwords and keys: delete
it once Unraid is back.
