---
title: LazyLibrarian
parent: Apps
nav_order: 7
---

# LazyLibrarian

## Setting it up

Mount the folder LazyLibrarian has at `/config` into Backuparr. It's
LazyLibrarian's [datadir](https://lazylibrarian.gitlab.io/config_commandline/#-datadir),
which holds its database and config; LazyLibrarian's docs have no page on
backups.

On LazyLibrarian's card, enter its URL - for example
`http://lazylibrarian:5299` - the **full-access** API key (Config > Interface
> API), and that folder as the **Data folder**. Click **Test connection**.

## Backups and restores

- **What's in the backup:** LazyLibrarian's own backup - its database,
  `config.ini`, and any `dicts.json`, `genres.json` and file/login templates.
  Books, magazines and covers aren't included.
- **Mount it read-write:** LazyLibrarian writes each backup into that folder
  and Backuparr removes it once collected. With a read-only mount the files
  stay there and restores don't work.
- **Restore** only while LazyLibrarian is stopped. It replaces the database
  and settings, the API key included.
