#!/usr/bin/env python3
"""prepares, tags and describes semantic-version releases.

  python scripts/release.py prepare X.Y.Z
      on an up-to-date main: creates release/vX.Y.Z, sets the version in
      blackvuesync_v2/__init__.py and pyproject.toml, dates the changelog
      section, commits, pushes the branch and opens a pull request.

  python scripts/release.py tag X.Y.Z
      after that pull request is merged: checks origin/main carries version
      X.Y.Z and a dated changelog section, then pushes an annotated vX.Y.Z tag.
      the tag starts release.yml (github release) and docker-build.yml (images
      X.Y.Z, X.Y and X).

  python scripts/release.py notes X.Y.Z
      prints the changelog section for X.Y.Z after checking the version files
      agree; release.yml uses it for the release text.

versions are plain MAJOR.MINOR.PATCH (no pre-release suffix), so the git tag,
the python version and the docker tags are always the same string.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INIT_FILE = REPO / "blackvuesync_v2" / "__init__.py"
PYPROJECT = REPO / "pyproject.toml"
CHANGELOG = REPO / "CHANGELOG.md"

VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
INIT_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
PYPROJECT_RE = re.compile(r'^version = "([^"]+)"$', re.MULTILINE)
HEADING_RE = re.compile(r"^## (.+)$", re.MULTILINE)
DATED_RE = r"^## {version} - \d{{4}}-\d{{2}}-\d{{2}}$"


class ReleaseError(Exception):
    """a release precondition failed; the message says which."""


def parse_version(text: str) -> tuple[int, int, int]:
    """returns (major, minor, patch) or raises ReleaseError."""
    match = VERSION_RE.match(text)
    if not match:
        raise ReleaseError(f"{text!r} is not MAJOR.MINOR.PATCH (e.g. 3.1.0)")
    return int(match[1]), int(match[2]), int(match[3])


def highest_tag(tags: list[str]) -> tuple[int, int, int] | None:
    """returns the highest vX.Y.Z tag, ignoring anything else."""
    versions = [
        parse_version(t[1:])
        for t in tags
        if t.startswith("v") and VERSION_RE.match(t[1:])
    ]
    return max(versions) if versions else None


def read_version(text: str, pattern: re.Pattern[str], name: str) -> str:
    match = pattern.search(text)
    if not match:
        raise ReleaseError(f"no version found in {name}")
    return match[1]


def set_version(text: str, pattern: re.Pattern[str], version: str, name: str) -> str:
    """replaces the single version assignment in a file's text."""
    new, count = pattern.subn(lambda m: m[0].replace(f'"{m[1]}"', f'"{version}"'), text)
    if count != 1:
        raise ReleaseError(f"expected one version line in {name}, found {count}")
    return new


def date_changelog(text: str, version: str, today: datetime.date) -> str:
    """turns "## Unreleased" or "## X.Y.Z - unreleased" into "## X.Y.Z - date"."""
    dated = f"## {version} - {today.isoformat()}"
    candidates = [f"## {version} - unreleased", "## Unreleased"]
    for heading in candidates:
        pattern = re.compile(rf"^{re.escape(heading)}$", re.MULTILINE)
        if pattern.search(text):
            if not changelog_section(text.replace(heading, dated, 1), version):
                raise ReleaseError(f"the {heading!r} section in CHANGELOG.md is empty")
            return pattern.sub(dated, text, count=1)
    raise ReleaseError(
        f"CHANGELOG.md has neither '## Unreleased' nor '## {version} - unreleased'"
    )


def changelog_section(text: str, version: str) -> str:
    """returns the body of the '## X.Y.Z - ...' section, without its heading."""
    headings = list(HEADING_RE.finditer(text))
    for i, heading in enumerate(headings):
        if heading[1].split(" ")[0] == version:
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            body = text[heading.end() : end]
            body = body.split("<!-- --8<-- [end:changelog] -->")[0]
            return body.strip()
    return ""


def git(*args: str, capture: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO, check=True, text=True, capture_output=capture
    )
    return result.stdout.strip() if capture else ""


def origin_repo() -> str:
    """returns owner/name of the origin remote (github https or ssh url)."""
    url = git("remote", "get-url", "origin")
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not match:
        raise ReleaseError(f"origin is not a github repository: {url}")
    return match[1]


def require_clean_main() -> None:
    if git("status", "--porcelain"):
        raise ReleaseError("the working tree has uncommitted changes")
    git("fetch", "--quiet", "--tags", "origin", "main")
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise ReleaseError("run this on the main branch")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        raise ReleaseError("main is not identical to origin/main; pull first")


def cmd_prepare(version: str, open_pr: bool) -> None:
    target = parse_version(version)
    require_clean_main()
    latest = highest_tag(git("tag", "--list", "v*").splitlines())
    if latest is not None and target <= latest:
        raise ReleaseError(
            f"{version} is not newer than tag v{'.'.join(map(str, latest))}"
        )
    init_text = INIT_FILE.read_text()
    current = read_version(init_text, INIT_RE, "__init__.py")
    if VERSION_RE.match(current) and target < parse_version(current):
        raise ReleaseError(f"{version} is lower than the code's version {current}")

    new_init = set_version(init_text, INIT_RE, version, "__init__.py")
    new_pyproject = set_version(
        PYPROJECT.read_text(), PYPROJECT_RE, version, "pyproject.toml"
    )
    new_changelog = date_changelog(
        CHANGELOG.read_text(), version, datetime.date.today()
    )

    branch = f"release/v{version}"
    git("switch", "--quiet", "-c", branch)
    INIT_FILE.write_text(new_init)
    PYPROJECT.write_text(new_pyproject)
    CHANGELOG.write_text(new_changelog)
    git("add", str(INIT_FILE), str(PYPROJECT), str(CHANGELOG))
    git("commit", "--quiet", "-m", f"release: v{version}", capture=False)
    print(f"committed release v{version} on {branch}")
    if not open_pr:
        print(f"push it with: git push -u origin {branch}")
        return
    git("push", "--quiet", "-u", "origin", branch, capture=False)
    body = changelog_section(new_changelog, version)
    pr = subprocess.run(
        [
            "gh", "api", f"repos/{origin_repo()}/pulls",
            "-f", f"title=release: v{version}",
            "-f", f"head={branch}",
            "-f", "base=main",
            "-f", f"body={body}",
            "--jq", ".html_url",
        ],
        cwd=REPO, check=True, text=True, capture_output=True,
    )  # fmt: skip
    print(f"opened {pr.stdout.strip()}")
    print(f"after it is merged: python scripts/release.py tag {version}")


def check_versions(version: str, init_text: str, pyproject_text: str) -> None:
    for text, pattern, name in (
        (init_text, INIT_RE, "__init__.py"),
        (pyproject_text, PYPROJECT_RE, "pyproject.toml"),
    ):
        found = read_version(text, pattern, name)
        if found != version:
            raise ReleaseError(f"{name} says {found}, expected {version}")


def cmd_tag(version: str) -> None:
    parse_version(version)
    git("fetch", "--quiet", "--tags", "origin", "main")
    tag = f"v{version}"
    if git("tag", "--list", tag):
        raise ReleaseError(f"tag {tag} already exists")
    rel_init = INIT_FILE.relative_to(REPO).as_posix()
    check_versions(
        version,
        git("show", f"origin/main:{rel_init}"),
        git("show", "origin/main:pyproject.toml"),
    )
    changelog = git("show", "origin/main:CHANGELOG.md")
    if not re.search(DATED_RE.format(version=re.escape(version)), changelog, re.M):
        raise ReleaseError(
            f"CHANGELOG.md on origin/main has no dated {version} section"
        )
    commit = git("rev-parse", "origin/main")
    git("tag", "-a", tag, commit, "-m", f"BlackVue Sync v2 {version}")
    git("push", "--quiet", "origin", tag, capture=False)
    print(
        f"pushed {tag} at {commit[:7]}; release.yml and docker-build.yml take it from here"
    )


def cmd_notes(version: str) -> None:
    parse_version(version)
    check_versions(version, INIT_FILE.read_text(), PYPROJECT.read_text())
    section = changelog_section(CHANGELOG.read_text(), version)
    if not section:
        raise ReleaseError(f"CHANGELOG.md has no section for {version}")
    print(section)


def main() -> int:
    parser = argparse.ArgumentParser(description="semantic-version releases")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="bump, date the changelog, open a pr")
    prepare.add_argument("version")
    prepare.add_argument("--no-pr", action="store_true", help="commit only")
    sub.add_parser("tag", help="tag the merged release").add_argument("version")
    sub.add_parser("notes", help="print the release notes").add_argument("version")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            cmd_prepare(args.version, open_pr=not args.no_pr)
        elif args.command == "tag":
            cmd_tag(args.version)
        else:
            cmd_notes(args.version)
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        print(f"release: {' '.join(error.cmd[:3])} failed: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
