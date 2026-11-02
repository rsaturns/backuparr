# Contributing to Backuparr

Thanks for helping out. Bug fixes, new apps, docs and test improvements are
all welcome. Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Bugs:** open an issue with the [bug report form](https://github.com/rsaturns/backuparr/issues/new?template=bug_report.yml),
  or go straight to a pull request for a small, obvious fix.
- **Features and new apps:** open an issue (or comment on an existing one)
  first, so we can agree on the approach before you write the code.
- **Security problems:** don't open a public issue - see [SECURITY.md](SECURITY.md).

## Getting set up

You need Git, Python 3.15 (what CI and the image use) and Docker.

```sh
git clone https://github.com/<you>/backuparr.git
cd backuparr
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

To try your change in the real app, build the image and run it as in the
[Quick start](README.md#quick-start):

```sh
docker build -t backuparr:dev .
scripts/smoke_test.sh backuparr:dev    # the same container check CI runs
```

## Making a change

- Branch off `main` and keep each pull request to one change. Small PRs get
  reviewed and merged much faster.
- **Add or update tests** in `tests/`. The suite must pass without the
  `rclone` binary (CI doesn't have it), so stub `rclone_util` calls in tests.
- **Test against the real app** when you change how one is backed up or
  restored, and say which version you used in the PR.
- The web UI is plain JavaScript and CSS with no framework or build step -
  please keep it that way. API calls that change state must send JSON.
- Update the docs in `docs/` (published as the
  [documentation site](https://rsaturns.github.io/backuparr/)) when behaviour
  or setup changes, and add a line under `## [Unreleased]` in
  [CHANGELOG.md](CHANGELOG.md) for anything users will notice. Contributors
  are credited there.

### Adding an app

Each app is a driver in `apps/<name>.py`, researched against the app's real
API or source code (the existing drivers' docstrings show the depth
expected). A new app also needs an entry in `config_store.py`'s `APP_META`,
wiring in `backup.py`'s `_build_app()`, an icon in `webui/static/icons/`, its
row in `docs/apps/index.md` (plus a page in `docs/apps/` if it needs notes),
the issue-form dropdown, and the generator in
`scripts/gen_architecture_diagram.py`. Ask in the issue if any of that is
unclear.

## AI-assisted contributions

Backuparr itself is built with a lot of help from AI (see the README), so
AI-assisted PRs are fine. You're still responsible for every line: understand
it, test it, and say in the PR that AI helped.

## Pull requests

Opening a PR fills in a short template - answer each section. CI runs the
tests and a container smoke test, and PRs are squash-merged once they pass and
have been reviewed.
