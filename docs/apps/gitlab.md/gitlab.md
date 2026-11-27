---
title: GitLab
parent: Apps
nav_order: 4
---

# GitLab

## Setting it up

GitLab makes its own backups on its own schedule: Backuparr copies the newest one from GitLab's backups folder, mounted read-only. Set that up first.

**Back GitLab up on a schedule.** Put this in a cron job on the GitLab server:

```sh
gitlab-backup create CRON=1
gitlab-ctl backup-etc --backup-path /var/opt/gitlab/backups/config
chmod 644 /var/opt/gitlab/backups/config/gitlab_config_*.tar
```

The first line makes the `*_gitlab_backup.tar` archive; the second backs up `/etc/gitlab` (including `gitlab-secrets.json`) into `config/`; the third makes the config archive readable, so Backuparr can copy it - it is created readable only by root. Set `gitlab_rails['backup_archive_permissions'] = 0644` in `/etc/gitlab/gitlab.rb` so Backuparr can also read the backup archive, which is otherwise readable only by the `git` user. GitLab's own guide, [Back up GitLab](https://docs.gitlab.com/administration/backup_restore/backup_gitlab/), covers the schedule and keeping old backups.

**Mount the backups folder.** Mount GitLab's backups folder into Backuparr read-only - its `/var/opt/gitlab/backups`, or `$GITLAB_HOME/data/backups` in Docker - and put that path in the card's **Data folder**. Backuparr only reads from it.

**Add the URL and a token.** On GitLab's card, enter its URL - for example `http://gitlab:8080` - and a personal access token. Make one in GitLab's **User settings > Access tokens**, with only the **read_user** scope: Backuparr uses it only to read GitLab's version, so it can check it against the newest backup. GitLab's own guide, [Personal access tokens](https://docs.gitlab.com/user/profile/personal_access_tokens/), covers making one. A token shows once: copy it onto the card, then click **Test connection**.

## What's in a backup

The newest `*_gitlab_backup.tar` - the database, the repositories and their LFS files, uploads, CI/CD job artifacts and logs, packages and containers - and the newest `config/gitlab_config_*.tar`, which holds `/etc/gitlab`, including `gitlab-secrets.json` and `gitlab.rb`. The config archive holds the database encryption key: without it, two-factor authentication and CI/CD variables can't be restored.

A backup is skipped while GitLab is still writing it, and Backuparr warns if the newest backup is more than two days old, if it skipped any parts, or if there is no config archive. Backups are always encrypted. GitLab's own backup doesn't cover object storage data, a Helm chart or Operator install, or a host key in `/etc/ssh`: see GitLab's own guide, [Back up GitLab](https://docs.gitlab.com/administration/backup_restore/backup_gitlab/).

## Restoring

GitLab restores only to the exact same version and type (CE or EE) on a working install, so Backuparr can't restore it itself. Pick GitLab and a backup on Backuparr's **Restore** tab: instead of **Restore** it shows a **Download** button and these steps.

1. Download the backup: the `*_gitlab_backup.tar` archive and the `config/gitlab_config_*.tar` archive. An encrypted backup downloads decrypted; anyone with access to that computer can read it, so delete it when you're done.
2. Restore only to the exact same GitLab version and type (CE or EE) on a working install.
3. Stop Puma and Sidekiq on the GitLab server.
4. Run `gitlab-backup restore BACKUP=<backup-id>` (in Docker: `docker exec -t <container> gitlab-backup restore BACKUP=<backup-id>`).
5. Restore `gitlab-secrets.json` from the config archive to `/etc/gitlab`, then restart GitLab.

GitLab's own guide, [Restore GitLab](https://docs.gitlab.com/administration/backup_restore/restore_gitlab/), covers the rest.
