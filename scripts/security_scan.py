#!/usr/bin/env python3
"""Bookkeeping for the weekly security scan (.github/workflows/security-scan.yml).

  plan       Writes what to scan to $GITHUB_OUTPUT: every release-* branch
             (fixes are made there), and the published images - the newest
             release, whose findings fail the run, and :latest, whose
             findings only go to the Security tab (during the 3.0 beta it's
             2.5.7, which gets no more fixes).
  freshness  What Dependabot can't keep current, on each release branch:
             rclone (an ARG in the Dockerfile) and the Trivy version the
             workflows pin. Also fails when .github/dependabot.yml points at
             a branch that no longer exists, and warns about a release branch
             nothing points at.

Runs on the runner's own Python with the standard library only. Needs
GITHUB_REPOSITORY and GH_TOKEN (read access is enough).
"""
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request

RELEASE_PREFIX = "release-"


def release_branches(names):
    return sorted(n for n in names if n.startswith(RELEASE_PREFIX))


def target_branches(dependabot_yml):
    """The target-branch values in a dependabot.yml (entries without one
    watch the default branch)."""
    return set(re.findall(r"^\s*target-branch:\s*[\"']?([^\"'\s#]+)", dependabot_yml, re.M))


def dockerfile_arg(dockerfile, name):
    match = re.search(rf"^ARG\s+{re.escape(name)}=(\S+)", dockerfile, re.M)
    return match.group(1) if match else None


def trivy_versions(workflow):
    """The Trivy versions a workflow pins with trivy-action's `version:`."""
    if "aquasecurity/trivy-action" not in workflow:
        return set()
    return set(re.findall(r"^\s*version:\s*[\"']?v?(\d+(?:\.\d+)+)", workflow, re.M))


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version))


def image_plan(newest_tag):
    """The published images to scan. Each keeps the same name from week to
    week, so the Security tab closes an alert once a newer image is free
    of it."""
    return [
        {"name": "newest", "tag": newest_tag, "gate": True},
        {"name": "latest", "tag": "latest", "gate": False},
    ]


def freshness(branches, all_branches, dependabot_yml, files, latest_rclone, latest_trivy):
    """(errors, warnings) for the freshness check.

    branches: the release branches; all_branches: every branch name;
    files: {branch: {path: text}} with each branch's Dockerfile and
    workflow files (the scan's own branch included)."""
    errors, warnings = [], []
    targets = target_branches(dependabot_yml)
    for name in sorted(targets - set(all_branches)):
        errors.append(
            f".github/dependabot.yml points at {name}, which no longer exists: "
            "point it at the branch the work happens on (main when no release line is open)."
        )
    for name in branches:
        if name not in targets:
            warnings.append(
                f"No Dependabot entry watches {name}. A long-lived release line needs its "
                "own target-branch entries; a short patch branch doesn't."
            )
    for branch, texts in sorted(files.items()):
        rclone = dockerfile_arg(texts.get("Dockerfile", ""), "RCLONE_VERSION")
        if rclone and version_key(rclone) < version_key(latest_rclone):
            errors.append(
                f"{branch}: the Dockerfile has rclone {rclone}; {latest_rclone} is out "
                "(bump RCLONE_VERSION)."
            )
        for path, text in sorted(texts.items()):
            for version in sorted(trivy_versions(text)):
                if version_key(version) < version_key(latest_trivy):
                    errors.append(
                        f"{branch}: {path} pins Trivy {version}; {latest_trivy} is out "
                        "(bump trivy-action's version:)."
                    )
    return errors, warnings


# --- GitHub API ---------------------------------------------------------

def _api(path):
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(base + path, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _repo():
    return os.environ["GITHUB_REPOSITORY"]


def _all_branches():
    names, page = [], 1
    while True:
        batch = _api(f"/repos/{_repo()}/branches?per_page=100&page={page}")
        names += [b["name"] for b in batch]
        if len(batch) < 100:
            return names
        page += 1


def _file_at(ref, path):
    quoted = urllib.parse.quote(path)
    data = _api(f"/repos/{_repo()}/contents/{quoted}?ref={urllib.parse.quote(ref)}")
    return base64.b64decode(data["content"]).decode()


def _workflows_at(ref):
    listing = _api(f"/repos/{_repo()}/contents/.github/workflows?ref={urllib.parse.quote(ref)}")
    return [item["path"] for item in listing if item["name"].endswith((".yml", ".yaml"))]


def _latest_release(repo):
    return _api(f"/repos/{repo}/releases/latest")["tag_name"].lstrip("v")


def _local_files():
    files = {}
    workflows = ".github/workflows"
    for name in sorted(os.listdir(workflows)):
        if name.endswith((".yml", ".yaml")):
            with open(os.path.join(workflows, name), encoding="utf-8") as f:
                files[f"{workflows}/{name}"] = f.read()
    return files


def _write(variable, lines):
    target = os.environ.get(variable)
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))


def plan():
    branches = release_branches(_all_branches())
    images = image_plan(_latest_release(_repo()))
    _write("GITHUB_OUTPUT", [f"branches={json.dumps(branches)}", f"images={json.dumps(images)}"])
    print("Branches:", ", ".join(branches) or "none")
    print("Images:", ", ".join(f"{i['name']} ({i['tag']})" for i in images))
    return 0


def check_freshness():
    all_names = _all_branches()
    branches = release_branches(all_names)
    with open(".github/dependabot.yml", encoding="utf-8") as f:
        dependabot_yml = f.read()
    files = {os.environ.get("GITHUB_REF_NAME", "this branch"): _local_files()}
    for branch in branches:
        texts = {"Dockerfile": _file_at(branch, "Dockerfile")}
        for path in _workflows_at(branch):
            texts[path] = _file_at(branch, path)
        files[branch] = texts
    latest_rclone = _latest_release("rclone/rclone")
    latest_trivy = _latest_release("aquasecurity/trivy")
    errors, warnings = freshness(branches, all_names, dependabot_yml, files, latest_rclone, latest_trivy)
    for message in warnings:
        print(f"::warning::{message}")
    for message in errors:
        print(f"::error::{message}")
    summary = [
        "## Freshness",
        f"Latest rclone {latest_rclone}, Trivy {latest_trivy}; Dependabot watches "
        f"{', '.join(sorted(target_branches(dependabot_yml))) or 'the default branch'}.",
        "",
    ]
    summary += [f"- :x: {m}" for m in errors] + [f"- :warning: {m}" for m in warnings]
    if not errors and not warnings:
        summary.append("- :white_check_mark: Nothing to update.")
    _write("GITHUB_STEP_SUMMARY", summary)
    return 1 if errors else 0


def main(argv):
    commands = {"plan": plan, "freshness": check_freshness}
    if len(argv) != 2 or argv[1] not in commands:
        print(f"usage: {argv[0]} plan|freshness", file=sys.stderr)
        return 2
    return commands[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
