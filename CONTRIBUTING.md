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

To run the suite exactly as CI does - in the image's own Alpine Python, a
worker per CPU - you only need Docker:

```sh
scripts/test_in_image.sh -n auto
```

To try your change in the real app, build the image and run it as in the
[Quick start](README.md#quick-start):

```sh
docker build -t backuparr:dev .
scripts/smoke_test.sh backuparr:dev    # the same container check CI runs
```

## Making a change

- Branch off the open release branch if there is one: a
  [`release-X.Y.Z` branch](https://github.com/rsaturns/backuparr/branches/all?query=release-),
  with a draft "Release X.Y.Z" pull request. Open your pull request against
  it. Otherwise branch off `main`. Keep each pull request to one change:
  small PRs get reviewed and merged much faster.
- **Add or update tests** in `tests/`: new functionality and bug fixes come
  with tests that cover them. The suite must pass without the `rclone`
  binary (CI doesn't have it), so stub `rclone_util` calls in tests, and
  without a network: tests talk only to servers they start on localhost.
- Follow the [coding standard](#coding-standard) below; the suite checks the
  Python part, so a finding fails it like any other test.
- **Test against the real app** when you change how one is backed up or
  restored, and say which version you used in the PR.
- The web UI is plain JavaScript and CSS with no framework or build step -
  please keep it that way. API calls that change state must send JSON.
- Update the docs in `docs/` (published as the
  [documentation site](https://docs.backuparr.com/)) when behaviour
  or setup changes, and add a line under `## [Unreleased]` in
  [CHANGELOG.md](CHANGELOG.md) for anything users will notice. Contributors
  are credited there.

### Coding standard

- **Python** follows [PEP 8](https://peps.python.org/pep-0008/), without a
  fixed line length: wrap a line where that makes it easier to read. The
  suite checks it with [ruff](https://docs.astral.sh/ruff/)
  (`tests/test_lint.py`, rules in `ruff.toml`): pycodestyle's E and W rules,
  and pyflakes' F rules for unused imports, undefined names and the like.
  Run it on its own with `.venv/bin/ruff check .`.
- **JavaScript and CSS** are plain, with no framework or build step. No
  inline scripts or event handlers - the Content Security Policy blocks
  them - and API calls that change state send JSON. Not checked by a tool
  yet.
- **Shell scripts** start with `set -euo pipefail` and quote their variables.
  Not checked by a tool yet.

### Adding an app

Each app is a driver in `apps/<name>.py`, researched against the app's real
API or source code (the existing drivers' docstrings show the depth expected).
A new app also needs an entry in `config_store.py`'s `APP_META`, its driver in
`backup.py`'s `DRIVERS` (built from the `APP_META` extra fields), its restore
in `restore_actions.py`'s `RESTORES` (with a `restore_warning` in `APP_META`
when the user must know something first), an icon in `webui/static/icons/`,
its row in `docs/apps/index.md` (plus a page in `docs/apps/` if it needs
notes), the issue-form dropdown, and the generator in
`scripts/gen_architecture_diagram.py`. Ask in the issue if any of that is
unclear.

## AI-assisted contributions

Backuparr itself is built with a lot of help from AI (see the README), so
AI-assisted PRs are fine. You're still responsible for every line: understand
it, test it, and say in the PR that AI helped.

## Pull requests

Opening a PR fills in a short template - answer each section. CI runs the
tests and a container smoke test, and PRs are merged once they pass and have
been reviewed. A PR merged into a release branch ships with that release, and
we close its issue then: `Closes #N` only works for pull requests into `main`.
