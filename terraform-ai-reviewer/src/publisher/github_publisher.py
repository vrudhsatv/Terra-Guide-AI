"""Stage 3b: publish the review to GitHub.

* One **sticky PR comment** per pull request. It is identified by a hidden HTML marker and
  edited in place on every push, so the PR never accumulates duplicate review comments.
* One **Check Run** per head commit with a summary and line annotations. Tokens that may not
  create check runs (e.g. a PAT on CodeBuild/CircleCI) fall back to a commit status.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

from models import SEVERITY_ICONS, Finding, ReviewReport, Severity, ToolStatus

log = logging.getLogger(__name__)

MAX_COMMENT_CHARS = 65_000  # GitHub hard limit is 65,536
MAX_CHECK_TEXT = 65_000
ANNOTATIONS_PER_REQUEST = 50

_VERDICT_BADGE = {
    "APPROVE": "✅ `APPROVE`",
    "APPROVE_WITH_CHANGES": "⚠️ `APPROVE_WITH_CHANGES`",
    "REJECT": "❌ `REJECT`",
}
_TOOL_BADGE = {
    ToolStatus.PASSED: "✅ passed",
    ToolStatus.FAILED: "⚠️ findings",
    ToolStatus.ERROR: "💥 error",
    ToolStatus.SKIPPED: "⏭️ skipped",
}
_ANNOTATION_LEVEL = {
    Severity.CRITICAL: "failure",
    Severity.HIGH: "failure",
    Severity.MEDIUM: "warning",
    Severity.LOW: "notice",
    Severity.INFO: "notice",
}


class PublishError(RuntimeError):
    pass


def _cell(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class MarkdownRenderer:
    def __init__(self, report: ReviewReport, *, marker: str, blob_base: str = "", path_prefix: str = ""):
        self.report = report
        self.marker = marker
        self.blob_base = blob_base  # e.g. https://github.com/o/r/blob/<sha>
        self.path_prefix = path_prefix

    def _location(self, f: Finding) -> str:
        if not f.file:
            return "_repository_"
        label = f"`{f.location}`"
        if not self.blob_base:
            return label
        anchor = f"#L{f.line}" if f.line > 0 else ""
        if f.line > 0 and f.end_line and f.end_line > f.line:
            anchor += f"-L{f.end_line}"
        return f"[{label}]({self.blob_base}/{self.path_prefix}{f.file}{anchor})"

    def _row(self, f: Finding) -> str:
        rule = f"[`{_cell(f.rule_id, 60)}`]({f.guideline})" if f.guideline else f"`{_cell(f.rule_id, 60)}`"
        return (
            f"| {SEVERITY_ICONS[f.severity]} {f.severity.value} | {self._location(f)} | {f.source.value} "
            f"| {rule} | {_cell(f.title)} |"
        )

    def _table(self, findings: list[Finding], budget: int) -> tuple[str, int]:
        header = "| Severity | Location | Source | Rule | Issue |\n|---|---|---|---|---|\n"
        rows: list[str] = []
        used = len(header)
        for f in findings:
            row = self._row(f) + "\n"
            if used + len(row) > budget:
                break
            rows.append(row)
            used += len(row)
        return header + "".join(rows), len(findings) - len(rows)

    def _details(self, findings: list[Finding], budget: int) -> str:
        blocks: list[str] = []
        used = 0
        for f in findings:
            if not (f.description or f.recommendation or f.evidence):
                continue
            parts = [f"**{SEVERITY_ICONS[f.severity]} {_cell(f.title, 200)}** — {self._location(f)} (`{f.rule_id}`)"]
            if f.description:
                parts.append(f.description.strip())
            if f.evidence and f.source.value == "llm-policy":
                parts.append(f"```hcl\n{f.evidence.strip()}\n```")
            if f.recommendation:
                parts.append(f"**Fix:** {f.recommendation.strip()}")
            if f.duplicates:
                parts.append(f"_Also reported by: {', '.join(f.duplicates)}_")
            block = "\n\n".join(parts) + "\n\n---\n"
            if used + len(block) > budget:
                blocks.append("_More details omitted to stay within GitHub's comment size limit._\n")
                break
            blocks.append(block)
            used += len(block)
        return "".join(blocks)

    def render(self) -> str:
        r = self.report
        counts = r.counts()
        out: list[str] = [self.marker, "## 🤖 Terraform AI Review", ""]
        out.append(
            f"**Recommendation:** {_VERDICT_BADGE.get(r.verdict, r.verdict)} — advisory only; "
            "a human reviewer makes the final call."
        )
        out += [
            "",
            "| " + " | ".join(f"{SEVERITY_ICONS[s]} {s.value.title()}" for s in Severity) + " |",
            "|" + "---|" * len(Severity),
            "| " + " | ".join(str(counts[s]) for s in Severity) + " |",
            "",
        ]

        if not r.changed_files:
            out += ["No Terraform files changed in this pull request.", ""]

        out += ["### Checks", "", "| Check | Result | Findings | Notes |", "|---|---|---|---|"]
        for t in r.tool_results:
            out.append(f"| `{t.tool}` | {_TOOL_BADGE[t.status]} | {len(t.findings)} | {_cell(t.message, 200) or ''} |")
        if r.llm is not None:
            if r.llm.skipped_reason:
                status, note = _TOOL_BADGE[ToolStatus.SKIPPED], r.llm.skipped_reason
            elif r.llm.error and not r.llm.accepted and not r.llm.summary:
                status, note = _TOOL_BADGE[ToolStatus.ERROR], r.llm.error
            else:
                status = _TOOL_BADGE[ToolStatus.FAILED if r.llm.accepted else ToolStatus.PASSED]
                note = f"{r.llm.provider}:{r.llm.model}"
                if r.llm.rejected:
                    note += f"; {len(r.llm.rejected)} unverifiable finding(s) discarded"
                if r.llm.error:
                    note += f"; partial failure: {r.llm.error}"
            out.append(f"| `org-policy (LLM)` | {status} | {len(r.llm.accepted)} | {_cell(note, 200)} |")
        out.append("")

        if r.llm is not None and r.llm.summary:
            out += ["### Policy review summary", "", r.llm.summary.strip(), ""]

        in_diff = [f for f in r.findings if f.in_diff]
        elsewhere = [f for f in r.findings if not f.in_diff]
        body_so_far = len("\n".join(out))
        budget = MAX_COMMENT_CHARS - body_so_far - 1500
        if in_diff:
            table, omitted = self._table(in_diff, budget // 2)
            out += [f"### Findings on lines changed in this PR ({len(in_diff)})", "", table]
            if omitted:
                out.append(f"_…and {omitted} more (see the check run for the full list)._")
            out.append("")
            budget -= len(table)
        if elsewhere:
            table, omitted = self._table(elsewhere, budget // 2)
            out += [
                "<details>",
                f"<summary><b>Other findings in the reviewed files ({len(elsewhere)})</b></summary>",
                "",
                table,
            ]
            if omitted:
                out.append(f"_…and {omitted} more._")
            out += ["</details>", ""]
            budget -= len(table)
        if not r.findings and r.changed_files:
            out += ["🎉 No issues found in the changed Terraform code.", ""]

        details = self._details(r.findings, max(budget, 0))
        if details:
            out += ["<details>", "<summary><b>Details and suggested fixes</b></summary>", "", details, "</details>", ""]

        footer = [
            f"Commit `{r.head_sha[:7] or 'unknown'}`",
            f"updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        ]
        if r.hidden_count:
            footer.append(f"{r.hidden_count} finding(s) outside the configured scope/severity hidden")
        out.append("<sub>" + " · ".join(footer) + " · This comment is updated in place on every push.</sub>")
        text = "\n".join(out)
        return text[:MAX_COMMENT_CHARS]


class GitHubPublisher:
    def __init__(self, token: str, repository: str, api_url: str = "https://api.github.com"):
        if not token:
            raise PublishError("GITHUB_TOKEN is required to publish")
        if "/" not in repository:
            raise PublishError(f"invalid repository '{repository}', expected owner/repo")
        self.repo = repository
        self.api = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "terraform-ai-reviewer",
            }
        )

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{self.api}{path}"
        for attempt in range(4):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise PublishError(f"{method} {url} failed: {exc}") from exc
                time.sleep(2**attempt)
                continue
            retryable = resp.status_code in (502, 503, 504) or (
                resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0"
            )
            if retryable and attempt < 3:
                time.sleep(min(int(resp.headers.get("retry-after", 2**attempt)), 30))
                continue
            return resp
        raise PublishError(f"{method} {url} failed after retries")  # pragma: no cover

    @staticmethod
    def _check(resp: requests.Response, action: str) -> Any:
        if resp.status_code >= 400:
            raise PublishError(f"{action} failed: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else None

    # ------------------------------------------------------------------ sticky comment

    def find_sticky_comment(self, pr_number: int, marker: str) -> dict[str, Any] | None:
        url: str | None = f"/repos/{self.repo}/issues/{pr_number}/comments?per_page=100"
        found: dict[str, Any] | None = None
        while url:
            resp = self._request("GET", url)
            for comment in self._check(resp, "listing PR comments") or []:
                if marker in (comment.get("body") or ""):
                    found = comment  # keep the newest match if there are several
            url = resp.links.get("next", {}).get("url")
        return found

    def upsert_comment(self, pr_number: int, body: str, marker: str) -> str:
        existing = self.find_sticky_comment(pr_number, marker)
        if existing:
            resp = self._request("PATCH", f"/repos/{self.repo}/issues/comments/{existing['id']}", json={"body": body})
            data = self._check(resp, "updating the review comment")
            log.info("Updated review comment %s", data.get("html_url"))
        else:
            resp = self._request("POST", f"/repos/{self.repo}/issues/{pr_number}/comments", json={"body": body})
            data = self._check(resp, "creating the review comment")
            log.info("Created review comment %s", data.get("html_url"))
        return data.get("html_url", "")

    # ------------------------------------------------------------------ check run

    @staticmethod
    def _annotations(findings: list[Finding], path_prefix: str) -> list[dict[str, Any]]:
        annotations = []
        for f in findings:
            if not f.file or f.line <= 0:
                continue
            message = f.description or f.title
            if f.recommendation:
                message += f"\n\nFix: {f.recommendation}"
            annotations.append(
                {
                    "path": f"{path_prefix}{f.file}",
                    "start_line": f.line,
                    "end_line": f.line,  # single-line keeps annotations on the exact line in the diff view
                    "annotation_level": _ANNOTATION_LEVEL[f.severity],
                    "title": f"[{f.severity.value}] {f.source.value}: {f.rule_id}"[:255],
                    "message": message[:64_000],
                }
            )
        return annotations

    def publish_check_run(
        self,
        name: str,
        head_sha: str,
        report: ReviewReport,
        summary_markdown: str,
        conclusion: str,
        path_prefix: str = "",
        details_url: str = "",
    ) -> None:
        counts = report.counts()
        if report.findings:
            title = f"{report.verdict}: " + ", ".join(f"{counts[s]} {s.value.lower()}" for s in Severity if counts[s])
        else:
            title = f"{report.verdict}: no issues"
        annotations = self._annotations(report.findings, path_prefix)
        summary = summary_markdown[:MAX_CHECK_TEXT]
        payload: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "output": {"title": title[:255], "summary": summary, "annotations": annotations[:ANNOTATIONS_PER_REQUEST]},
        }
        if details_url:
            payload["details_url"] = details_url
        resp = self._request("POST", f"/repos/{self.repo}/check-runs", json=payload)
        if resp.status_code in (403, 404):
            log.warning(
                "Token cannot create check runs (HTTP %s); falling back to a commit status. "
                "Check runs need a GitHub Actions token or a GitHub App.",
                resp.status_code,
            )
            self.publish_commit_status(name, head_sha, conclusion, title, details_url)
            return
        check = self._check(resp, "creating the check run")
        for start in range(ANNOTATIONS_PER_REQUEST, len(annotations), ANNOTATIONS_PER_REQUEST):
            batch = annotations[start : start + ANNOTATIONS_PER_REQUEST]
            patch = {"output": {"title": title[:255], "summary": summary, "annotations": batch}}
            self._check(self._request("PATCH", f"/repos/{self.repo}/check-runs/{check['id']}", json=patch),
                        "adding check run annotations")
        log.info("Published check run %s with %d annotation(s)", check.get("html_url"), len(annotations))

    def publish_commit_status(self, context: str, sha: str, conclusion: str, description: str, target_url: str) -> None:
        state = {"success": "success", "neutral": "success", "failure": "failure"}.get(conclusion, "error")
        payload = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            payload["target_url"] = target_url
        self._check(self._request("POST", f"/repos/{self.repo}/statuses/{sha}", json=payload), "setting commit status")
        log.info("Published commit status '%s' = %s", context, state)
