"""scripts/security_scan.py: the weekly security scan's plan and freshness
check (.github/workflows/security-scan.yml)."""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "security_scan", os.path.join(ROOT, "scripts", "security_scan.py"))
security_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(security_scan)

DEPENDABOT = """version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    target-branch: "release-3.0.0"
  - package-ecosystem: docker
    target-branch: release-3.0.0  # same line
  - package-ecosystem: "github-actions"
    target-branch: 'release-3.1.0'
"""

TRIVY_STEP = """
      - uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0
        with:
          version: v0.76.2
"""


def test_release_branches_are_the_release_lines_only():
    assert security_scan.release_branches(
        ["main", "release-3.0.0", "gh-pages", "release-2.5.8", "ci-security"]
    ) == ["release-2.5.8", "release-3.0.0"]


def test_target_branches_reads_quoted_and_bare_values():
    assert security_scan.target_branches(DEPENDABOT) == {"release-3.0.0", "release-3.1.0"}
    assert security_scan.target_branches("version: 2\nupdates: []\n") == set()


def test_dockerfile_arg():
    dockerfile = "FROM python:3.15-alpine@sha256:abc\nARG RCLONE_VERSION=1.76.0\nARG TARGETARCH\n"
    assert security_scan.dockerfile_arg(dockerfile, "RCLONE_VERSION") == "1.76.0"
    assert security_scan.dockerfile_arg(dockerfile, "TARGETARCH") is None


def test_trivy_versions_only_counts_trivy_action_steps():
    assert security_scan.trivy_versions(TRIVY_STEP) == {"0.76.2"}
    assert security_scan.trivy_versions("with:\n  version: v1.2.3\n") == set()


@pytest.mark.parametrize("older, newer", [
    ("1.76.0", "1.76.1"), ("v0.76.2", "0.77.0"), ("1.9.9", "1.10.0"),
])
def test_version_key_orders_numerically(older, newer):
    assert security_scan.version_key(older) < security_scan.version_key(newer)


def test_image_plan_gates_the_newest_release_only():
    plan = security_scan.image_plan("3.0.0-beta.3")
    assert plan == [
        {"name": "newest", "tag": "3.0.0-beta.3", "gate": True},
        {"name": "latest", "tag": "latest", "gate": False},
    ]


def _files(rclone="1.76.0", trivy="v0.76.2"):
    step = TRIVY_STEP.replace("v0.76.2", trivy)
    return {"release-3.0.0": {
        "Dockerfile": f"FROM x\nARG RCLONE_VERSION={rclone}\n",
        ".github/workflows/ci.yml": step,
    }}


def test_freshness_passes_when_everything_is_current():
    errors, warnings = security_scan.freshness(
        ["release-3.0.0"], ["main", "release-3.0.0"],
        'target-branch: "release-3.0.0"\n', _files(), "1.76.0", "0.76.2")
    assert errors == [] and warnings == []


def test_freshness_flags_an_older_rclone_and_trivy():
    errors, _ = security_scan.freshness(
        ["release-3.0.0"], ["main", "release-3.0.0"],
        'target-branch: "release-3.0.0"\n', _files("1.76.0", "v0.76.2"), "1.77.0", "0.77.1")
    assert any("rclone 1.76.0; 1.77.0 is out" in e for e in errors)
    assert any(".github/workflows/ci.yml pins Trivy 0.76.2; 0.77.1 is out" in e for e in errors)


def test_freshness_fails_on_a_target_branch_that_no_longer_exists():
    # 3.0.0 final: release-3.0.0 is merged and deleted, dependabot.yml
    # still points at it.
    errors, _ = security_scan.freshness(
        [], ["main"], 'target-branch: "release-3.0.0"\n', {}, "1.76.0", "0.76.2")
    assert len(errors) == 1 and "release-3.0.0, which no longer exists" in errors[0]


def test_freshness_warns_about_a_release_branch_nothing_watches():
    errors, warnings = security_scan.freshness(
        ["release-3.0.1"], ["main", "release-3.0.1"], "version: 2\n", {}, "1.76.0", "0.76.2")
    assert errors == []
    assert len(warnings) == 1 and "release-3.0.1" in warnings[0]


def test_main_rejects_an_unknown_command(capsys):
    assert security_scan.main(["security_scan.py", "nope"]) == 2
    assert "usage" in capsys.readouterr().err
