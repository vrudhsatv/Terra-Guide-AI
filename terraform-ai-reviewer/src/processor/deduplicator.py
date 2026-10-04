"""Stage 3a: merge duplicate findings across tools and rank them."""

from __future__ import annotations

import re

from models import Finding, Severity

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "is", "are", "be", "to", "of", "for", "and", "or", "in", "on", "with", "that", "this",
    "should", "must", "not", "no", "ensure", "enabled", "resource", "terraform",
}


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1}


def _similar(a: Finding, b: Finding, threshold: float) -> bool:
    ta, tb = _tokens(f"{a.title} {a.rule_id}"), _tokens(f"{b.title} {b.rule_id}")
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def _same_place(a: Finding, b: Finding, slack: int) -> bool:
    if a.file != b.file:
        return False
    a_end = a.end_line or a.line
    b_end = b.end_line or b.line
    return a.line - slack <= b_end and b.line - slack <= a_end


def _better(a: Finding, b: Finding) -> bool:
    """True if ``a`` should be kept over ``b``."""
    return (a.severity.rank, a.source.is_deterministic, a.in_diff) > (b.severity.rank, b.source.is_deterministic, b.in_diff)


def deduplicate(findings: list[Finding], *, line_slack: int = 2, similarity: float = 0.5) -> list[Finding]:
    """Collapse exact duplicates, then near-duplicates reported by different tools at the same spot.

    The surviving finding records what it absorbed in ``duplicates`` (e.g. ``["tflint:aws_x"]``).
    """
    exact: dict[tuple[str, int, str, str], Finding] = {}
    for f in findings:
        key = (f.file, f.line, f.source.value, f.rule_id.upper())
        if key in exact:
            continue  # e.g. checkov scanning a parent and a nested working dir
        exact[key] = f

    kept: list[Finding] = []
    for f in sorted(exact.values(), key=lambda x: (x.file, x.line)):
        match = next(
            (
                k
                for k in kept
                if k.source != f.source and _same_place(k, f, line_slack) and _similar(k, f, similarity)
            ),
            None,
        )
        if match is None:
            kept.append(f)
            continue
        winner, loser = (f, match) if _better(f, match) else (match, f)
        winner.duplicates = [*winner.duplicates, *loser.duplicates, f"{loser.source.value}:{loser.rule_id}"]
        winner.in_diff = winner.in_diff or loser.in_diff
        if winner is f:
            kept[kept.index(match)] = f
    return kept


def rank(findings: list[Finding]) -> list[Finding]:
    """Most important first: severity, then lines changed by this PR, then deterministic before LLM."""
    return sorted(
        findings,
        key=lambda f: (-f.severity.rank, not f.in_diff, not f.source.is_deterministic, f.file, f.line, f.rule_id),
    )


def filter_min_severity(findings: list[Finding], minimum: Severity) -> tuple[list[Finding], int]:
    kept = [f for f in findings if f.severity.rank >= minimum.rank]
    return kept, len(findings) - len(kept)


def compute_verdict(findings: list[Finding]) -> str:
    """Advisory recommendation, using the same thresholds as the original Terrascan/Gemini reviewer."""
    counts = {s: 0 for s in Severity}
    for f in findings:
        counts[f.severity] += 1
    if counts[Severity.CRITICAL] or counts[Severity.HIGH] or counts[Severity.MEDIUM] >= 4:
        return "REJECT"
    if counts[Severity.MEDIUM]:
        return "APPROVE_WITH_CHANGES"
    return "APPROVE"
