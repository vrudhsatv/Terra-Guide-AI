"""Detect pull-request metadata on GitHub Actions, AWS CodeBuild, CircleCI or a plain shell.

Explicit environment variables always win, so any CI system can be supported by exporting:
    GITHUB_REPOSITORY=owner/repo  PR_NUMBER=123  BASE_SHA=<sha>  HEAD_SHA=<sha>
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

_REPO_URL_RE = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/.]+?)(?:\.git)?/?$")
_PR_URL_RE = re.compile(r"github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)")


@dataclass
class PullRequestContext:
    platform: str
    repository: str = ""  # owner/repo
    pr_number: int | None = None
    base_sha: str = ""
    head_sha: str = ""
    base_ref: str = ""  # branch name, used when base_sha is unknown

    @property
    def is_pull_request(self) -> bool:
        return bool(self.repository and self.pr_number)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _from_github_actions() -> PullRequestContext:
    ctx = PullRequestContext(platform="github-actions", repository=_env("GITHUB_REPOSITORY"))
    event_path = _env("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, encoding="utf-8") as fh:
                event = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read GitHub event payload %s: %s", event_path, exc)
            event = {}
        pr = event.get("pull_request") or {}
        if pr:
            ctx.pr_number = pr.get("number")
            ctx.base_sha = (pr.get("base") or {}).get("sha", "")
            ctx.head_sha = (pr.get("head") or {}).get("sha", "")
            ctx.base_ref = (pr.get("base") or {}).get("ref", "")
    if not ctx.base_ref:
        ctx.base_ref = _env("GITHUB_BASE_REF")
    if not ctx.head_sha:
        ctx.head_sha = _env("GITHUB_SHA")
    return ctx


def _from_codebuild() -> PullRequestContext:
    ctx = PullRequestContext(platform="aws-codebuild")
    match = _REPO_URL_RE.search(_env("CODEBUILD_SOURCE_REPO_URL"))
    if match:
        ctx.repository = match.group("repo")
    trigger = _env("CODEBUILD_WEBHOOK_TRIGGER")  # e.g. "pr/42"
    if trigger.startswith("pr/") and trigger[3:].isdigit():
        ctx.pr_number = int(trigger[3:])
    ctx.base_ref = _env("CODEBUILD_WEBHOOK_BASE_REF").removeprefix("refs/heads/")
    ctx.head_sha = _env("CODEBUILD_RESOLVED_SOURCE_VERSION")
    return ctx


def _from_circleci() -> PullRequestContext:
    ctx = PullRequestContext(platform="circleci", head_sha=_env("CIRCLE_SHA1"))
    user, name = _env("CIRCLE_PROJECT_USERNAME"), _env("CIRCLE_PROJECT_REPONAME")
    if user and name:
        ctx.repository = f"{user}/{name}"
    pr_url = _env("CIRCLE_PULL_REQUEST") or _env("CIRCLE_PULL_REQUESTS").split(",")[0]
    match = _PR_URL_RE.search(pr_url)
    if match:
        ctx.repository = match.group("repo")
        ctx.pr_number = int(match.group("number"))
    return ctx


def _github_get(url: str, token: str) -> object | None:
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        log.warning("GitHub API request %s failed: %s", url, exc)
        return None


def _fill_from_github_api(ctx: PullRequestContext, token: str, api_url: str) -> None:
    """Ask GitHub for whatever PR metadata the CI system does not expose."""
    if not (token and ctx.repository):
        return
    if not ctx.pr_number and ctx.head_sha:
        # e.g. AWS CodePipeline only knows the commit: find the open PR containing it.
        pulls = _github_get(f"{api_url}/repos/{ctx.repository}/commits/{ctx.head_sha}/pulls", token)
        open_pulls = [p for p in pulls or [] if isinstance(p, dict) and p.get("state") == "open"]
        if open_pulls:
            ctx.pr_number = open_pulls[0]["number"]
            log.info("Resolved commit %s to PR #%s", ctx.head_sha[:12], ctx.pr_number)
    if not ctx.is_pull_request or (ctx.base_sha and ctx.head_sha):
        return
    pr = _github_get(f"{api_url}/repos/{ctx.repository}/pulls/{ctx.pr_number}", token)
    if not isinstance(pr, dict):
        return
    ctx.base_sha = ctx.base_sha or pr["base"]["sha"]
    ctx.base_ref = ctx.base_ref or pr["base"]["ref"]
    ctx.head_sha = ctx.head_sha or pr["head"]["sha"]


def detect_context(token: str = "", api_url: str = "https://api.github.com") -> PullRequestContext:
    if _env("GITHUB_ACTIONS") == "true":
        ctx = _from_github_actions()
    elif _env("CODEBUILD_BUILD_ID"):
        ctx = _from_codebuild()
    elif _env("CIRCLECI") == "true":
        ctx = _from_circleci()
    else:
        ctx = PullRequestContext(platform="local")

    # Explicit overrides (also how AWS CodePipeline passes PR data into CodeBuild).
    ctx.repository = _env("GITHUB_REPOSITORY") or ctx.repository
    if _env("PR_NUMBER").isdigit():
        ctx.pr_number = int(_env("PR_NUMBER"))
    ctx.base_sha = _env("BASE_SHA") or ctx.base_sha
    ctx.head_sha = _env("HEAD_SHA") or ctx.head_sha
    ctx.base_ref = _env("BASE_REF") or ctx.base_ref

    _fill_from_github_api(ctx, token, api_url)
    log.info(
        "CI context: platform=%s repo=%s pr=%s base=%s head=%s",
        ctx.platform,
        ctx.repository or "-",
        ctx.pr_number or "-",
        (ctx.base_sha or ctx.base_ref or "-")[:12],
        (ctx.head_sha or "-")[:12],
    )
    return ctx
