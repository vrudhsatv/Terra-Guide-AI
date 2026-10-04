"""Terraform AI Reviewer: CLI entrypoint shared by GitHub Actions, AWS CodeBuild and CircleCI.

Exit codes: 0 = review completed (advisory), 1 = FAIL_ON_SEVERITY threshold reached,
2 = configuration error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Allow `python src/main.py` and `python -m main` from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ci_context import PullRequestContext, detect_context  # noqa: E402
from config import Config, ConfigError, load_config  # noqa: E402
from deterministic.runner import DeterministicRunner, discover_working_dirs  # noqa: E402
from git_utils import DiffUnavailable, get_pr_diff, repo_prefix  # noqa: E402
from llm.engine import LLMError, PolicyReviewEngine  # noqa: E402
from llm.rules import load_rules  # noqa: E402
from models import Finding, LLMReview, ReviewReport, Severity, ToolResult  # noqa: E402
from processor.deduplicator import compute_verdict, deduplicate, filter_min_severity, rank  # noqa: E402
from processor.mapper import DiffMap, apply_scope, map_findings  # noqa: E402
from publisher.github_publisher import GitHubPublisher, MarkdownRenderer, PublishError  # noqa: E402

log = logging.getLogger("terraform-ai-reviewer")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="terraform-ai-reviewer",
        description="Hybrid deterministic + LLM Terraform pull-request reviewer (advisory only).",
    )
    parser.add_argument("--workspace", help="Terraform repository to review (default: $WORKSPACE_DIR or cwd)")
    parser.add_argument("--dry-run", action="store_true", default=None, help="Print the review instead of publishing")
    parser.add_argument("--output", help="Write the machine-readable report JSON here")
    parser.add_argument("--diff-file", help="Use this unified diff instead of computing one from git")
    parser.add_argument("--base", help="Base commit/branch to diff against (overrides BASE_SHA)")
    parser.add_argument("--head", help="Head commit (overrides HEAD_SHA, default HEAD)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser.parse_args(argv)


def _github_annotation(level: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}", flush=True)


def load_diff(args: argparse.Namespace, config: Config, ctx: PullRequestContext) -> DiffMap | None:
    """Return the PR diff map, or None when no diff base is known (full-repository scan)."""
    if args.diff_file:
        return DiffMap.from_text(Path(args.diff_file).read_text(encoding="utf-8"))
    if not (ctx.base_sha or ctx.base_ref or ctx.is_pull_request):
        log.warning("No diff base (BASE_SHA/BASE_REF/PR_NUMBER); reviewing all Terraform files without a diff")
        return None
    try:
        text = get_pr_diff(ctx, config.workspace, config.diff_globs, config.github_token, config.github_api_url)
    except DiffUnavailable as exc:
        log.error("Could not compute the PR diff: %s", exc)
        _github_annotation("warning", f"Terraform AI Reviewer could not compute the PR diff: {exc}")
        return None
    return DiffMap.from_text(text)


def run_llm(config: Config, diff_map: DiffMap | None, deterministic: list[Finding]) -> LLMReview | None:
    if "llm" in config.skip_tools:
        return None
    review = LLMReview(provider=config.llm_provider, model=config.llm_model)
    if config.llm_provider == "none":
        review.skipped_reason = "no LLM API key configured (set ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY)"
        return review
    if not config.llm_api_key:
        review.skipped_reason = f"LLM_PROVIDER={config.llm_provider} but its API key is not set"
        return review
    if diff_map is None:
        review.skipped_reason = "no PR diff available; policy review needs a diff"
        return review
    try:
        rules = load_rules(config.rules_file)
    except FileNotFoundError as exc:
        review.skipped_reason = str(exc)
        return review
    if not rules.text.strip():
        review.skipped_reason = f"rules file {config.rules_file} is empty"
        return review
    try:
        return PolicyReviewEngine(config, rules).review(diff_map, deterministic)
    except LLMError as exc:
        review.error = str(exc)
        return review


def build_report(
    config: Config,
    ctx: PullRequestContext,
    diff_map: DiffMap | None,
    working_dirs: list[str],
    tool_results: list[ToolResult],
    llm: LLMReview | None,
) -> ReviewReport:
    findings = [f for t in tool_results for f in t.findings]
    if llm is not None:
        findings += llm.accepted

    effective_map = diff_map or DiffMap({})
    map_findings(findings, effective_map)
    findings = deduplicate(findings)
    hidden = 0
    if diff_map is not None:
        findings, hidden = apply_scope(findings, diff_map, config.scope)
    findings, below = filter_min_severity(findings, config.min_severity)
    findings = rank(findings)
    return ReviewReport(
        findings=findings,
        tool_results=tool_results,
        llm=llm,
        changed_files=effective_map.changed_files if diff_map is not None else [],
        working_dirs=working_dirs,
        head_sha=ctx.head_sha,
        base_sha=ctx.base_sha or ctx.base_ref,
        verdict=compute_verdict(findings),
        hidden_count=hidden + below,
    )


def publish(config: Config, ctx: PullRequestContext, report: ReviewReport, markdown: str, conclusion: str) -> bool:
    if config.dry_run:
        log.info("Dry run: not publishing to GitHub")
        return True
    if not (config.github_token and ctx.is_pull_request):
        log.warning("Not publishing: GITHUB_TOKEN, GITHUB_REPOSITORY and PR_NUMBER are all required")
        return True
    publisher = GitHubPublisher(config.github_token, ctx.repository, config.github_api_url)
    ok = True
    try:
        publisher.upsert_comment(ctx.pr_number, markdown, config.comment_marker)  # type: ignore[arg-type]
    except PublishError as exc:
        ok = False
        log.error("Publishing the PR comment failed: %s", exc)
        _github_annotation("warning", f"Terraform AI Reviewer could not update the PR comment: {exc}")
    if ctx.head_sha:
        try:
            publisher.publish_check_run(
                config.check_run_name,
                ctx.head_sha,
                report,
                markdown,
                conclusion,
                path_prefix=repo_prefix(config.workspace),
                details_url=f"{config.github_server_url}/{ctx.repository}/pull/{ctx.pr_number}",
            )
        except PublishError as exc:
            ok = False
            log.error("Publishing the check run failed: %s", exc)
            _github_annotation("warning", f"Terraform AI Reviewer could not publish the check run: {exc}")
    return ok


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.base:
        os.environ["BASE_SHA"] = args.base
    if args.head:
        os.environ["HEAD_SHA"] = args.head
    try:
        config = load_config(workspace=args.workspace, dry_run=args.dry_run, output=args.output)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    ctx = detect_context(config.github_token, config.github_api_url)

    # ---- Stage 0: diff + scope
    diff_map = load_diff(args, config, ctx)
    changed = diff_map.changed_files if diff_map is not None else []
    if diff_map is not None and not changed and not config.working_dirs:
        working_dirs: list[str] = []
        log.info("No Terraform files changed; nothing to scan")
    else:
        working_dirs = discover_working_dirs(config, changed)
    log.info("Changed Terraform files: %d; working directories: %s", len(changed), working_dirs or "-")

    # ---- Stage 1: deterministic tools
    tool_results = DeterministicRunner(config).run(working_dirs) if working_dirs else []

    # ---- Stage 2: LLM policy review
    deterministic_findings = [f for t in tool_results for f in t.findings]
    llm = run_llm(config, diff_map, deterministic_findings) if changed or diff_map is None else None
    if llm is not None and llm.error:
        _github_annotation("warning", f"LLM policy review incomplete: {llm.error}")

    # ---- Stage 3: process + publish
    report = build_report(config, ctx, diff_map, working_dirs, tool_results, llm)
    blob_base = (
        f"{config.github_server_url}/{ctx.repository}/blob/{ctx.head_sha}" if ctx.repository and ctx.head_sha else ""
    )
    markdown = MarkdownRenderer(
        report, marker=config.comment_marker, blob_base=blob_base, path_prefix=repo_prefix(config.workspace)
    ).render()

    failed = bool(
        config.fail_on_severity
        and any(f.severity.rank >= config.fail_on_severity.rank for f in report.findings)
    )
    conclusion = "failure" if failed else ("neutral" if report.findings else "success")

    if config.output_file:
        config.output_file.parent.mkdir(parents=True, exist_ok=True)
        config.output_file.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        log.info("Wrote report to %s", config.output_file)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    if config.dry_run or not ctx.is_pull_request:
        print(markdown)

    publish(config, ctx, report, markdown, conclusion)

    counts = report.counts()
    log.info(
        "Done: verdict=%s %s",
        report.verdict,
        " ".join(f"{s.value.lower()}={counts[s]}" for s in Severity),
    )
    if failed:
        log.error("Findings at or above FAIL_ON_SEVERITY=%s", config.fail_on_severity.value)  # type: ignore[union-attr]
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
