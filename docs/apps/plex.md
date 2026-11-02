---
title: Plex
parent: Apps
nav_order: 5
---

# Plex

A Plex backup has two parts:

- **The library database** - Plex's own **Download Database** export:
  libraries, metadata, playlists, collections, and every user's watched state
  and ratings.
- **`Preferences.xml`** - the server's settings and its **identity**, which
  shared libraries are tied to. It's read from Plex's data folder. Restoring it
  brings a rebuilt server back as the *same* server; without it, every shared
  user has to be invited again.

Images, caches, codecs and plugins are left out; a library scan brings the
images back.

Mount Plex's data folder - `Library/Application Support/Plex Media Server`
inside its `/config`, in both the official and linuxserver images:

```yaml
    volumes:
      - "/share/Container/plex/Library/Application Support/Plex Media Server:/mnt/plex:ro"   # drop :ro to restore from the web UI
```

Then fill in the card: the URL, the **server owner's** Plex token, and **Data
folder** = `/mnt/plex`.

- **URL:** Plex usually runs with host networking, so use the host's address,
  not `http://plex:32400`. If Plex's **Secure connections** is **Preferred**
  (the default), `http://192.168.1.10:32400` works. If it's **Required**, use
  the server's `https://192-168-1-10.<id>.plex.direct:32400` address -
  Plex's certificate isn't valid for the bare IP. Backuparr connects to
  `plex.direct` names without a DNS lookup, so routers that block them don't
  matter.
- **Token:** click **Get Plex token** and sign in on Plex's website (your
  password never reaches Backuparr). It fills in the token, and the
  `plex.direct` address when the URL is empty or points at the same machine.
  If the popup is blocked, use the **Open Plex sign-in** link. You can also
  [find a token by hand](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
- **The archive is sensitive:** `Preferences.xml` holds the server's plex.tv
  sign-in token.
- **Restore needs Plex stopped** (`docker stop plex`); Backuparr refuses while
  it's running. It replaces the database and `Preferences.xml`, then you start
  Plex. A backup from a newer Plex is refused; an older one is upgraded by Plex.
- **Rebuilding a server:** start the new Plex once (no need to sign in), stop
  it, then restore. It comes back as the original server with its shared
  users; keep the original token in Settings. Never run the original and the
  restored server at the same time.
- **To restore by hand**, follow Plex's guides to [restoring the
  database](https://support.plex.tv/articles/202485658-restore-a-database-backed-up-via-scheduled-tasks/)
  and [moving a server](https://support.plex.tv/articles/201370363-move-an-install-to-another-system/):
  put `com.plexapp.plugins.library.db` in `Plug-in Support/Databases` (deleting
  its `-wal` and `-shm` files) and `Preferences.xml` in the `Plex Media Server`
  folder.
