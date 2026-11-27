---
title: Reactive Resume
parent: Apps
nav_order: 15
---

# Reactive Resume

## Setting it up

On Reactive Resume's card, enter its URL - for example
`http://reactive-resume:3000` - and an API key. Make one in Reactive
Resume's **Settings > Developer > API keys**, following
[Using the API](https://docs.rxresu.me/guides/using-the-api):

- **Name:** something like `Backuparr`.
- **Expires:** **Never** - an expired key disappears and stops working, and
  backups with it.
- **Access:** **Read only** - that's all Backuparr needs.

Reactive Resume shows the key once: copy it onto the card, then click **Test
connection**. It reports the version and the account's name. To back up
another account, add it as an extra instance with its own key.

## What's in a backup

One account's export, with its uploaded files, in the same layout as Reactive
Resume's **Export everything** zip - see
[Exporting your data](https://docs.rxresu.me/guides/exporting-your-data):
the profile, resumes, cover letters, version history, job applications,
Career data and feedback, plus the files the documents reference. It never
holds passwords, tokens or API keys.

The export caps at 500 files, 8 MiB per file and 16 MiB of uploaded files in
total, and 64 MiB or 50,000 entries for the whole archive. Files over those
limits are skipped and listed in `upload-manifest.json`, and the run log warns
about them. Backups of Reactive Resume are always encrypted.

## Restoring

Reactive Resume has no account import, so Backuparr can't restore it itself.
Pick Reactive Resume and a backup on Backuparr's **Restore** tab: instead of
**Restore** it shows a **Download** button and these steps.

![Restore tab with Reactive Resume picked: a Download button, the steps to restore it in Reactive Resume, and a link to this page]({{ '/screenshots/restore-manual.png' | relative_url }})

1. **Download** the backup. An encrypted backup downloads decrypted, as a
   `.zip`; anyone with access to that computer can read it, so delete it
   when you're done.
2. Unzip it: it holds the account export in the same layout as Reactive
   Resume's **Export everything** zip.
3. In Reactive Resume, open **Documents > Import** and pick a resume's JSON
   file from `resumes/` - each one becomes a new document; do the same for
   a cover letter from `letters/`. See
   [Importing a resume](https://docs.rxresu.me/guides/importing-resumes).
4. Add photos again by hand, and re-enter account settings, sharing settings
   and application links: the export doesn't hold them.

The same backup can also be downloaded from **History**.
