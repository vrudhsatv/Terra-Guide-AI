"""Shared data model for every stage of the reviewer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @classmethod
    def parse(cls, value: Any, default: "Severity | None" = None) -> "Severity":
        """Lenient parser: accepts tool-specific spellings ("error", "warning", ...)."""
        if isinstance(value, Severity):
            return value
        text = str(value or "").strip().upper()
        aliases = {
            "ERROR": cls.HIGH,
            "WARNING": cls.MEDIUM,
            "WARN": cls.MEDIUM,
            "NOTICE": cls.LOW,
            "MODERATE": cls.MEDIUM,
            "INFORMATIONAL": cls.INFO,
            "NONE": cls.INFO,
        }
        if text in cls.__members__:
            return cls[text]
        if text in aliases:
            return aliases[text]
        if default is not None:
            return default
        raise ValueError(f"Unknown severity: {value!r}")


_SEVERITY_RANK = {
    Severity.CRITICAL: 5,
    Severity.HIGH: 4,
    Severity.MEDIUM: 3,
    Severity.LOW: 2,
    Severity.INFO: 1,
}

SEVERITY_ICONS = {
    Severity.CRITICAL: "🟥",
    Severity.HIGH: "🟧",
    Severity.MEDIUM: "🟨",
    Severity.LOW: "🟦",
    Severity.INFO: "⬜",
}


class Source(str, Enum):
    TERRAFORM_INIT = "terraform-init"
    TERRAFORM_VALIDATE = "terraform-validate"
    TERRAFORM_FMT = "terraform-fmt"
    TFLINT = "tflint"
    CHECKOV = "checkov"
    LLM = "llm-policy"

    @property
    def is_deterministic(self) -> bool:
        return self is not Source.LLM


@dataclass
class Finding:
    """A single normalized issue, regardless of which tool produced it."""

    source: Source
    rule_id: str
    severity: Severity
    file: str  # repository-relative POSIX path ("" when not tied to a file)
    line: int  # 1-based; 0 when not tied to a line
    title: str
    description: str = ""
    recommendation: str = ""
    end_line: int | None = None
    resource: str = ""
    guideline: str = ""
    evidence: str = ""
    # Filled in by the processor stage.
    in_diff: bool = False
    duplicates: list[str] = field(default_factory=list)

    @property
    def location(self) -> str:
        if not self.file:
            return "(repository)"
        return f"{self.file}:{self.line}" if self.line > 0 else self.file

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source"] = self.source.value
        data["severity"] = self.severity.value
        return data


class ToolStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"  # tool ran and reported findings
    ERROR = "error"  # tool crashed / could not run
    SKIPPED = "skipped"


@dataclass
class ToolResult:
    tool: str
    status: ToolStatus
    findings: list[Finding] = field(default_factory=list)
    message: str = ""
    duration_s: float = 0.0


@dataclass
class LLMReview:
    provider: str
    model: str
    summary: str = ""
    verdict: str = ""
    accepted: list[Finding] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)  # {"finding": {...}, "reason": "..."}
    error: str = ""
    skipped_reason: str = ""


@dataclass
class ReviewReport:
    """Everything the publisher needs."""

    findings: list[Finding]
    tool_results: list[ToolResult]
    llm: LLMReview | None
    changed_files: list[str]
    working_dirs: list[str]
    head_sha: str
    base_sha: str
    verdict: str
    hidden_count: int = 0  # findings dropped by scope/severity filters

    def counts(self) -> dict[Severity, int]:
        out = {s: 0 for s in Severity}
        for f in self.findings:
            out[f.severity] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "changed_files": self.changed_files,
            "working_dirs": self.working_dirs,
            "counts": {k.value: v for k, v in self.counts().items()},
            "hidden_count": self.hidden_count,
            "findings": [f.to_dict() for f in self.findings],
            "tools": [
                {
                    "tool": t.tool,
                    "status": t.status.value,
                    "message": t.message,
                    "finding_count": len(t.findings),
                    "duration_s": round(t.duration_s, 2),
                }
                for t in self.tool_results
            ],
            "llm": None
            if self.llm is None
            else {
                "provider": self.llm.provider,
                "model": self.llm.model,
                "summary": self.llm.summary,
                "verdict": self.llm.verdict,
                "accepted": len(self.llm.accepted),
                "rejected": self.llm.rejected,
                "error": self.llm.error,
                "skipped_reason": self.llm.skipped_reason,
            },
        }
