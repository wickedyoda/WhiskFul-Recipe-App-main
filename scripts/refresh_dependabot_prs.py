#!/usr/bin/env python3
"""Rebase open Dependabot PRs onto the current default branch.

Dependabot rebases a PR when it detects a new upstream release, but not when the
base branch moves ahead on its own. On a repository where security fixes are
merged to master regularly, an unrebased Dependabot PR accumulates a diff that
also reverts that unrelated work. PR #214 is the worked example: it proposed
reverting the CodeQL config fix, the Trivy secret scans, the pypdf migration,
and every review fix from PR #216, because it had been open against a base from
six days earlier.

For each open Dependabot PR this script:
  * measures how far behind the base branch the PR is,
  * rebases it onto origin/<base> via the GitHub API,
  * leaves a one-time comment recording what it did.

It never merges. Branch protection and required checks remain authoritative.

Requires GH_TOKEN in the environment. Set DRY_RUN=true to report without
changing anything.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "")
BASE = os.environ.get("BASE_BRANCH", "master")
TOKEN = os.environ.get("GH_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in {"true", "1", "yes"}
API = "https://api.github.com"
SUMMARY = Path("dependabot-refresh-summary.md")

COMMENT_MARKER = "<!-- dependabot-refresh -->"


def api(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, object]:
    """Call the GitHub API and return (status, decoded body)."""
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dependabot-refresh",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]
    except urllib.error.URLError as exc:
        return 0, str(exc)


def is_dependabot(head_ref: str) -> bool:
    return head_ref.startswith("dependabot/")


def commits_behind(base_sha: str, head_sha: str) -> int:
    """Count commits on base that are absent from head."""
    status, data = api(f"/repos/{REPO}/compare/{head_sha}...{base_sha}")
    if status != 200 or not isinstance(data, dict):
        return 0
    return int(data.get("behind_by") or 0)


def has_prior_comment(pr_number: int) -> bool:
    status, data = api(f"/repos/{REPO}/issues/{pr_number}/comments?per_page=100")
    if status != 200 or not isinstance(data, list):
        return False
    return any(COMMENT_MARKER in (c.get("body") or "") for c in data)


def post_comment(pr_number: int, behind: int, ok: bool) -> None:
    if ok:
        body = (
            f"{COMMENT_MARKER}\n"
            "**Automated rebase** — this PR had drifted "
            f"{behind} commit(s) behind `{BASE}`, so its diff also carried "
            "reverts of unrelated work merged since it was opened.\n\n"
            f"Rebased onto `{BASE}` by the *Dependabot Refresh* workflow. "
            "Please re-check the diff and wait for CI before merging."
        )
    else:
        body = (
            f"{COMMENT_MARKER}\n"
            f"**Needs manual rebase** — this PR is {behind} commit(s) behind "
            f"`{BASE}`, and the automated rebase could not be applied cleanly.\n\n"
            "Close and reopen it, or rebase locally, so the diff reflects only "
            "the intended dependency change."
        )
    api(f"/repos/{REPO}/issues/{pr_number}/comments", "POST", {"body": body})


def rebase(pr_number: int, head_sha: str) -> tuple[bool, str]:
    """Rebase the PR's head branch onto its base branch.

    `expected_head_sha` is the optimistic-concurrency guard: the rebase is
    rejected if the branch moved since we read it, so we never rebase onto a
    base computed from a stale read.
    """
    status, data = api(
        f"/repos/{REPO}/pulls/{pr_number}/update-branch",
        "PUT",
        {"expected_head_sha": head_sha},
    )
    if status in (200, 202):
        return True, "rebased"
    if status == 422:
        return False, "merge conflict, or branch not rebasable"
    return False, f"HTTP {status}: {data}"


def main() -> int:
    if not REPO or not TOKEN:
        print("GITHUB_REPOSITORY and GH_TOKEN are required", file=sys.stderr)
        return 1

    status, prs = api(f"/repos/{REPO}/pulls?state=open&per_page=100")
    if status != 200 or not isinstance(prs, list):
        print(f"failed to list PRs: HTTP {status} {prs}", file=sys.stderr)
        return 1

    dependabot = [p for p in prs if is_dependabot(p["head"]["ref"])]
    others = [p for p in prs if not is_dependabot(p["head"]["ref"])]

    lines: list[str] = ["## Dependabot Refresh", ""]

    if not dependabot:
        lines.append("No open Dependabot PRs. Nothing to do.")
        SUMMARY.write_text("\n".join(lines) + "\n")
        print("no Dependabot PRs open")
        return 0

    # Resolve the base tip so comparisons use a fresh sha.
    status, ref = api(f"/repos/{REPO}/git/ref/heads/{BASE}")
    if status != 200 or not isinstance(ref, dict):
        print(f"failed to resolve {BASE}: HTTP {status} {ref}", file=sys.stderr)
        return 1
    base_sha = ref["object"]["sha"]

    lines.append(f"Base branch `{BASE}` at `{base_sha[:8]}`.")
    lines.append(f"Non-Dependabot open PRs (left alone): {len(others)}")
    lines.append("")
    lines.append("| PR | Branch | Behind | Action |")
    lines.append("|---|---|---:|---|")

    for pr in dependabot:
        number = pr["number"]
        head_sha = pr["head"]["sha"]
        behind = commits_behind(base_sha, head_sha)

        if behind == 0:
            lines.append(f"| #{number} | `{pr['head']['ref']}` | 0 | already current |")
            print(f"#{number}: current with {BASE}")
            continue

        if DRY_RUN:
            action = "would rebase (dry run)"
            lines.append(f"| #{number} | `{pr['head']['ref']}` | {behind} | {action} |")
            print(f"#{number}: {behind} behind, dry run, no action")
            continue

        ok, note = rebase(number, head_sha)
        if ok and not has_prior_comment(number):
            post_comment(number, behind, ok)
            note = "rebased + commented"
        elif ok:
            note = "rebased (comment already present)"
        lines.append(f"| #{number} | `{pr['head']['ref']}` | {behind} | {note} |")
        print(f"#{number}: {behind} behind -> {note}")

    SUMMARY.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
