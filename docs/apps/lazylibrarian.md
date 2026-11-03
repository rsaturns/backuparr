---
title: LazyLibrarian
parent: Apps
nav_order: 4
---

# LazyLibrarian

Use the **full-access** API key (Config > Interface > API). Mount the folder
LazyLibrarian has at `/config` into Backuparr and set it as the **Data
folder**.

- **What's in the backup:** LazyLibrarian's own backup - its database,
  `config.ini`, and any `dicts.json`, `genres.json` and file/login templates.
  Books, magazines and covers aren't included.
- **Mount it read-write:** LazyLibrarian writes each backup into that folder
  and Backuparr removes it once collected. With a read-only mount the files
  stay there and restores don't work.
- **Restore** only while LazyLibrarian is stopped. It replaces the database
  and settings, the API key included.
