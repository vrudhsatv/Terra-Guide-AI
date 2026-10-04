"""Reject unsupported or hallucinated LLM findings.

A model finding survives only if it can be traced to:
  1. a file that is part of the PR diff,
  2. a real line in that file which is (or is next to) a changed line,
  3. a rule ID that exists in the policy file (when the policy file uses IDs),
  4. evidence text that actually appears in the file near the cited line.
Line numbers are re-anchored to where the evidence really is, which fixes off-by-N citations.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from llm.rules import FREE_TEXT_RULE_ID, RuleSet
from models import Finding, Severity, Source
from processor.mapper import DiffMap

_LINE_PREFIX_RE = re.compile(r"^\s*\d+\s*[+\- ]\s?")
_WS_RE = re.compile(r"\s+")
_MIN_EVIDENCE_CHARS = 3


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _clean_evidence(evidence: str) -> list[str]:
    """Strip diff decorations the model may have copied (line numbers, +/- markers)."""
    cleaned: list[str] = []
    for raw in evidence.splitlines():
        line = _LINE_PREFIX_RE.sub("", raw)
        if line.startswith(("+", "-")) and not line.startswith(("++", "--")):
            line = line[1:]
        line = _norm(line)
        if len(line) >= _MIN_EVIDENCE_CHARS:
            cleaned.append(line)
    return cleaned


def _matches(evidence_line: str, file_line: str) -> bool:
    file_norm = _norm(file_line)
    if not file_norm:
        return False
    return evidence_line in file_norm or (len(file_norm) >= 8 and file_norm in evidence_line)


class FindingValidator:
    def __init__(self, diff_map: DiffMap, rules: RuleSet, workspace: Path, tolerance: int):
        self.diff_map = diff_map
        self.rules = rules
        self.workspace = workspace
        self.tolerance = max(0, tolerance)
        self._file_cache: dict[str, list[str] | None] = {}

    def _lines(self, path: str) -> list[str] | None:
        if path not in self._file_cache:
            target = self.workspace / path
            try:
                self._file_cache[path] = target.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self._file_cache[path] = None
        return self._file_cache[path]

    def _normalize_path(self, path: str) -> str:
        path = path.strip().strip("`").replace("\\", "/")
        for prefix in ("./", "b/", "a/"):
            if path.startswith(prefix) and path[len(prefix):] in self.diff_map.files:
                return path[len(prefix):]
        return path

    def _locate_evidence(self, evidence: list[str], lines: list[str], line: int, path: str) -> int | None:
        """Return the 1-based line where the evidence starts, preferring the cited neighbourhood."""
        first = evidence[0]
        window = range(max(1, line - self.tolerance), min(len(lines), line + self.tolerance) + 1)

        def full_match(start: int) -> bool:
            if not _matches(first, lines[start - 1]):
                return False
            # Remaining evidence lines must appear shortly after the first one.
            span = lines[start - 1 : start - 1 + len(evidence) + self.tolerance + 2]
            return all(any(_matches(ev, fl) for fl in span) for ev in evidence[1:])

        for candidate in sorted(window, key=lambda n: (abs(n - line), n)):
            if full_match(candidate):
                return candidate
        # The model may have cited the wrong line entirely; accept the evidence only if it
        # uniquely matches a line this PR changed.
        diff_file = self.diff_map.get(path)
        hits = [n for n in sorted(diff_file.added_lines) if n <= len(lines) and full_match(n)] if diff_file else []
        return hits[0] if len(hits) == 1 else None

    def validate(self, item: Any) -> tuple[Finding | None, str]:
        if not isinstance(item, dict):
            return None, "finding is not an object"
        required = ("rule_id", "file", "line", "title")
        missing = [k for k in required if item.get(k) in (None, "")]
        if missing:
            return None, f"missing fields: {', '.join(missing)}"

        path = self._normalize_path(str(item["file"]))
        diff_file = self.diff_map.get(path)
        if diff_file is None:
            return None, f"file '{path}' is not part of the PR diff"
        lines = self._lines(path)
        if lines is None:
            return None, f"file '{path}' is not readable in the workspace"

        try:
            line = int(item["line"])
        except (TypeError, ValueError):
            return None, f"line '{item['line']}' is not an integer"
        if not 1 <= line <= len(lines):
            return None, f"line {line} is outside '{path}' (1-{len(lines)})"

        rule_id = str(item["rule_id"]).strip().upper()
        rule = self.rules.lookup(rule_id) if self.rules.has_ids else None
        if self.rules.has_ids and rule is None:
            return None, f"rule '{rule_id}' does not exist in the policy file"
        if not self.rules.has_ids:
            rule_id = FREE_TEXT_RULE_ID

        evidence_lines = _clean_evidence(str(item.get("evidence") or ""))
        if evidence_lines:
            anchored = self._locate_evidence(evidence_lines, lines, line, path)
            if anchored is None:
                return None, f"evidence not found near {path}:{line}"
            line = anchored

        if line not in diff_file.added_lines:
            snapped = diff_file.nearest_added(line, 0 if evidence_lines else self.tolerance)
            if snapped is not None:
                line = snapped
            elif line not in diff_file.hunk_lines:
                return None, f"{path}:{line} is not part of the changes in this PR"

        if rule is not None and rule.severity_explicit:
            severity = rule.severity
        else:
            severity = Severity.parse(item.get("severity"), default=rule.severity if rule else Severity.MEDIUM)

        return (
            Finding(
                source=Source.LLM,
                rule_id=rule_id,
                severity=severity,
                file=path,
                line=line,
                title=str(item["title"]).strip(),
                description=str(item.get("description") or "").strip(),
                recommendation=str(item.get("recommendation") or "").strip(),
                evidence="\n".join(evidence_lines),
            ),
            "",
        )

    def validate_all(self, items: list[Any]) -> tuple[list[Finding], list[dict[str, Any]]]:
        accepted: list[Finding] = []
        rejected: list[dict[str, Any]] = []
        for item in items:
            finding, reason = self.validate(item)
            if finding is None:
                rejected.append({"finding": item, "reason": reason})
            else:
                accepted.append(finding)
        return accepted, rejected
