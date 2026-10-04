"""Load the human-readable organizational policy file.

Recommended format (IDs make findings verifiable; anything else in the file is passed through
to the model as free text):

    ## ORG-001: All S3 buckets must block public access
    Severity: HIGH
    Explanation, examples, exceptions...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from models import Severity

_RULE_HEADING_RE = re.compile(r"^#{1,6}\s+`?(?P<id>[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9_.-]+)`?\s*[:\-–—]\s*(?P<title>.+?)\s*$")
_SEVERITY_RE = re.compile(r"^\s*[*_-]*\s*severity\s*[*_]*\s*[:=]\s*[*_]*\s*(?P<sev>[A-Za-z]+)", re.IGNORECASE)

FREE_TEXT_RULE_ID = "ORG-POLICY"


@dataclass
class Rule:
    rule_id: str
    title: str
    severity: Severity
    body: str
    severity_explicit: bool = False  # True when the policy file states a severity


@dataclass
class RuleSet:
    text: str
    rules: dict[str, Rule]
    source: Path

    @property
    def has_ids(self) -> bool:
        return bool(self.rules)

    def lookup(self, rule_id: str) -> Rule | None:
        return self.rules.get(rule_id.strip().upper())


def load_rules(path: Path) -> RuleSet:
    if not path.is_file():
        raise FileNotFoundError(f"Rules file not found: {path} (set RULES_FILE_PATH)")
    text = path.read_text(encoding="utf-8")
    rules: dict[str, Rule] = {}
    current: Rule | None = None
    level = 0
    body: list[str] = []

    def close() -> None:
        if current is not None:
            current.body = "\n".join(body).strip()
            rules[current.rule_id] = current

    for line in text.splitlines():
        heading = _RULE_HEADING_RE.match(line)
        if heading:
            close()
            current = Rule(heading.group("id").upper(), heading.group("title"), Severity.MEDIUM, "")
            level = len(line) - len(line.lstrip("#"))
            body = []
            continue
        heading_level = len(line) - len(line.lstrip("#")) if line.startswith("#") else 0
        if current is not None and 0 < heading_level <= level:
            # A same-or-higher-level non-rule heading ends the rule; deeper headings are part of its body.
            close()
            current = None
            body = []
            continue
        if current is not None:
            sev = _SEVERITY_RE.match(line)
            if sev:
                current.severity = Severity.parse(sev.group("sev"), default=Severity.MEDIUM)
                current.severity_explicit = True
            body.append(line)
    close()
    return RuleSet(text=text, rules=rules, source=path)
