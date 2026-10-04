"""Retrieve the pull-request diff for Terraform files, with several fallbacks."""

from __future__ import annotations

import fnmatch
import logging
import subprocess
from pathlib import Path

import requests

from ci_context import PullRequestContext

log = logging.getLogger(__name__)


class DiffUnavailable(RuntimeError):
    pass


def _git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _has_commit(ref: str, cwd: Path) -> bool:
    return _git(["cat-file", "-e", f"{ref}^{{commit}}"], cwd).returncode == 0


def _ensure_base_available(ctx: PullRequestContext, cwd: Path) -> str | None:
    """Return a ref usable as the diff base, fetching it if the clone is shallow."""
    candidates = [c for c in (ctx.base_sha, f"origin/{ctx.base_ref}" if ctx.base_ref else "") if c]
    for ref in candidates:
        if _has_commit(ref, cwd):
            return ref
    fetch_targets = [t for t in (ctx.base_sha, ctx.base_ref) if t]
    for target in fetch_targets:
        log.info("Fetching diff base %s from origin", target)
        # Deepen enough for a merge-base to exist in most PRs, without a full clone.
        result = _git(["fetch", "--no-tags", "--depth=200", "origin", target], cwd, timeout=300)
        if result.returncode != 0:
            log.debug("git fetch %s failed: %s", target, result.stderr.strip())
            continue
        for ref in (*candidates, "FETCH_HEAD"):
            if _has_commit(ref, cwd):
                return ref
    return None


def _git_diff(ctx: PullRequestContext, cwd: Path, globs: list[str]) -> str:
    if _git(["rev-parse", "--is-inside-work-tree"], cwd).returncode != 0:
        raise DiffUnavailable(f"{cwd} is not a git work tree")
    base = _ensure_base_available(ctx, cwd)
    if base is None:
        raise DiffUnavailable("diff base commit is not available locally")
    head = ctx.head_sha if ctx.head_sha and _has_commit(ctx.head_sha, cwd) else "HEAD"
    pathspec = ["--", *globs]
    # --relative keeps paths relative to the workspace even when it is a subdirectory of the repo.
    common = [
        "diff", "--no-color", "--no-ext-diff", "--relative", "--unified=3", "--find-renames", "--diff-filter=ACMR",
    ]
    # Three-dot diff = changes introduced by the PR branch since it forked (what GitHub shows).
    result = _git([*common, f"{base}...{head}", *pathspec], cwd)
    if result.returncode != 0:
        log.info("Three-dot diff failed (%s); falling back to two-dot diff", result.stderr.strip())
        result = _git([*common, base, head, *pathspec], cwd)
    if result.returncode != 0:
        raise DiffUnavailable(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def _api_diff(ctx: PullRequestContext, token: str, api_url: str, globs: list[str], prefix: str) -> str:
    if not (ctx.is_pull_request and token):
        raise DiffUnavailable("GitHub API diff needs GITHUB_REPOSITORY, PR_NUMBER and GITHUB_TOKEN")
    url = f"{api_url}/repos/{ctx.repository}/pulls/{ctx.pr_number}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3.diff"},
        timeout=60,
    )
    if resp.status_code != 200:
        raise DiffUnavailable(f"GitHub API diff request failed: HTTP {resp.status_code} {resp.text[:200]}")
    return filter_diff_by_globs(resp.text, globs, prefix)


def filter_diff_by_globs(diff_text: str, globs: list[str], prefix: str = "") -> str:
    """Keep file sections whose new path matches a glob (full path or basename).

    With ``prefix`` (workspace subdirectory, e.g. ``infra/``), only files below it are kept and
    the prefix is stripped so paths match ``git diff --relative`` output.
    """
    out: list[str] = []
    keep = False
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            path = line.rstrip("\n").split(" b/", 1)[-1]
            keep = path.startswith(prefix) and any(
                fnmatch.fnmatch(path, g) or fnmatch.fnmatch(Path(path).name, g) for g in globs
            )
        if keep and prefix:
            for marker in ("diff --git a/", " b/", "--- a/", "+++ b/", "rename from ", "rename to "):
                line = line.replace(f"{marker}{prefix}", marker, 1)
        if keep:
            out.append(line)
    return "".join(out)


def repo_prefix(workspace: Path) -> str:
    """Path of the workspace inside the git repository ("" at the root, "infra/" for a subdirectory)."""
    try:
        result = _git(["rev-parse", "--show-prefix"], workspace)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def get_pr_diff(ctx: PullRequestContext, workspace: Path, globs: list[str], token: str, api_url: str) -> str:
    """Diff of the PR restricted to ``globs``, with paths relative to ``workspace``."""
    errors: list[str] = []
    try:
        return _git_diff(ctx, workspace, globs)
    except (DiffUnavailable, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        errors.append(f"git: {exc}")
    try:
        return _api_diff(ctx, token, api_url, globs, repo_prefix(workspace))
    except (DiffUnavailable, requests.RequestException) as exc:
        errors.append(f"api: {exc}")
    raise DiffUnavailable("; ".join(errors))
