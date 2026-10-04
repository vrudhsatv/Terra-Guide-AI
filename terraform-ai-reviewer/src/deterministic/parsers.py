"""Convert raw tool output into normalized :class:`Finding` objects.

Every parser is a pure function of (tool output, working dir, workspace) so it can be unit
tested with captured output and no tools installed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from models import Finding, Severity, Source

log = logging.getLogger(__name__)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def normalize_path(path: str, working_dir: str, workspace: Path) -> str:
    """Return a workspace-relative POSIX path for a tool-reported file path."""
    if not path:
        return ""
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(workspace)
        except ValueError:
            # Checkov reports "/main.tf" relative to the scanned dir, which looks absolute.
            candidate = Path(working_dir) / path.lstrip("/")
    else:
        candidate = Path(working_dir) / candidate
    return Path(os.path.normpath(candidate)).as_posix()


def _load_json(text: str, tool: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some tools print banners/warnings before the JSON document.
        for opener in ("{", "["):
            idx = text.find(opener)
            if idx != -1:
                try:
                    return json.loads(text[idx:])
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"{tool} did not return valid JSON: {text[:300]}")


# --------------------------------------------------------------------------- terraform


def parse_terraform_init_failure(stderr: str, working_dir: str) -> Finding:
    message = first_error_block(stderr) or "terraform init failed"
    return Finding(
        source=Source.TERRAFORM_INIT,
        rule_id="terraform-init",
        severity=Severity.HIGH,
        file="",
        line=0,
        title=f"`terraform init` failed in `{working_dir}`",
        description=message,
        recommendation="Fix provider/module sources and version constraints so the configuration can be initialized.",
    )


def first_error_block(text: str, limit: int = 1200) -> str:
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", text or "").strip()
    idx = cleaned.find("Error:")
    return (cleaned[idx:] if idx != -1 else cleaned)[:limit]


def parse_terraform_validate(stdout: str, working_dir: str, workspace: Path) -> list[Finding]:
    data = _load_json(stdout, "terraform validate") or {}
    findings: list[Finding] = []
    for diag in data.get("diagnostics", []):
        rng = diag.get("range") or {}
        file = normalize_path(rng.get("filename", ""), working_dir, workspace) if rng else ""
        severity = Severity.HIGH if diag.get("severity") == "error" else Severity.MEDIUM
        summary = diag.get("summary", "Validation diagnostic").strip()
        findings.append(
            Finding(
                source=Source.TERRAFORM_VALIDATE,
                rule_id=f"validate:{diag.get('severity', 'error')}",
                severity=severity,
                file=file,
                line=int((rng.get("start") or {}).get("line", 0)) if rng else 0,
                end_line=int((rng.get("end") or {}).get("line", 0)) or None if rng else None,
                title=summary if file else f"{summary} (in `{working_dir}`)",
                description=(diag.get("detail") or "").strip(),
                evidence=((diag.get("snippet") or {}).get("code") or "").strip(),
            )
        )
    return findings


def parse_terraform_fmt(stdout: str, working_dir: str, workspace: Path) -> list[Finding]:
    """Parse ``terraform fmt -check -diff -list=true`` output: one finding per file, at the first changed line."""
    first_line: dict[str, int] = {}
    hunks: dict[str, int] = {}
    listed: list[str] = []
    current: str | None = None
    old_no = 0
    in_hunk = False
    for raw in stdout.splitlines():
        if raw.startswith("--- old/"):
            current = raw[len("--- old/"):].strip()
            first_line.setdefault(current, 0)
            hunks.setdefault(current, 0)
            in_hunk = False
            continue
        if raw.startswith("+++ new/"):
            continue
        match = _HUNK_RE.match(raw)
        if match and current is not None:
            old_no = int(match.group(1))
            hunks[current] += 1
            in_hunk = True
            continue
        if in_hunk and current is not None:
            if raw.startswith(("-", "+")):
                if not first_line[current]:
                    first_line[current] = max(old_no, 1)
                if raw.startswith("-"):
                    old_no += 1
                continue
            if raw.startswith(" ") or raw == "":
                old_no += 1
                continue
            in_hunk = False  # e.g. the next file name printed by -list=true
        stripped = raw.strip()
        if stripped.endswith((".tf", ".tfvars", ".tftest.hcl")) and " " not in stripped:
            listed.append(stripped)
            current = None

    files = list(dict.fromkeys([*listed, *first_line]))
    findings: list[Finding] = []
    for name in files:
        count = hunks.get(name, 0)
        findings.append(
            Finding(
                source=Source.TERRAFORM_FMT,
                rule_id="terraform-fmt",
                severity=Severity.LOW,
                file=normalize_path(name, working_dir, workspace),
                line=first_line.get(name) or 1,
                title="File is not formatted with `terraform fmt`",
                description=f"{count} formatting hunk(s) differ from canonical style." if count else "",
                recommendation="Run `terraform fmt -recursive` and commit the result.",
            )
        )
    return findings


# --------------------------------------------------------------------------- tflint

_TFLINT_SEVERITY = {"error": Severity.HIGH, "warning": Severity.MEDIUM, "notice": Severity.LOW, "info": Severity.INFO}


def parse_tflint(stdout: str, working_dir: str, workspace: Path) -> tuple[list[Finding], list[str]]:
    """Return (findings, tool errors) from ``tflint --format=json``."""
    data = _load_json(stdout, "tflint") or {}
    findings: list[Finding] = []
    for issue in data.get("issues", []):
        rule = issue.get("rule") or {}
        rng = issue.get("range") or {}
        findings.append(
            Finding(
                source=Source.TFLINT,
                rule_id=rule.get("name", "tflint"),
                severity=_TFLINT_SEVERITY.get(str(rule.get("severity", "")).lower(), Severity.MEDIUM),
                file=normalize_path(rng.get("filename", ""), working_dir, workspace),
                line=int((rng.get("start") or {}).get("line", 0)),
                end_line=int((rng.get("end") or {}).get("line", 0)) or None,
                title=(issue.get("message") or rule.get("name", "TFLint issue")).strip(),
                guideline=rule.get("link", ""),
            )
        )
    errors = [e.get("message", str(e)) for e in data.get("errors", [])]
    return findings, errors


# --------------------------------------------------------------------------- checkov


def parse_checkov(stdout: str, working_dir: str, workspace: Path, default_severity: Severity) -> list[Finding]:
    data = _load_json(stdout, "checkov")
    if data is None:
        return []
    reports = data if isinstance(data, list) else [data]
    findings: list[Finding] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        for check in (report.get("results") or {}).get("failed_checks", []):
            line_range = check.get("file_line_range") or [0, 0]
            abs_path = check.get("file_abs_path") or ""
            file = (
                normalize_path(abs_path, working_dir, workspace)
                if abs_path and Path(abs_path).is_absolute() and Path(abs_path).exists()
                else normalize_path(check.get("file_path", ""), working_dir, workspace)
            )
            findings.append(
                Finding(
                    source=Source.CHECKOV,
                    rule_id=check.get("check_id", "CKV"),
                    severity=Severity.parse(check.get("severity"), default=default_severity),
                    file=file,
                    line=int(line_range[0] or 0),
                    end_line=int(line_range[1] or 0) or None,
                    title=(check.get("check_name") or check.get("check_id", "Checkov check failed")).strip(),
                    resource=check.get("resource", ""),
                    guideline=check.get("guideline") or "",
                )
            )
    return findings
