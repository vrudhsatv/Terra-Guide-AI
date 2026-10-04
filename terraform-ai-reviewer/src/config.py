"""Environment-driven configuration. Every knob can be set per CI platform."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from models import Severity

REVIEWER_ROOT = Path(__file__).resolve().parent.parent

ALL_TOOLS = ("init", "validate", "fmt", "tflint", "checkov", "llm")
SCOPES = ("changed_lines", "changed_files", "all")


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in _env(name).split(",") if item.strip()]


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    workspace: Path
    rules_file: Path
    working_dirs: list[str]
    diff_globs: list[str]
    skip_tools: set[str]
    scope: str
    min_severity: Severity
    fail_on_severity: Severity | None
    output_file: Path | None
    dry_run: bool

    # GitHub
    github_token: str
    github_api_url: str
    github_server_url: str
    check_run_name: str
    comment_marker: str

    # LLM
    llm_provider: str  # anthropic | openai | gemini | none
    llm_model: str
    llm_api_key: str
    llm_max_tokens: int
    llm_timeout_s: int
    llm_effort: str
    llm_max_diff_chars: int
    llm_line_tolerance: int

    # Tooling
    tflint_config: Path | None
    checkov_default_severity: Severity
    checkov_extra_args: list[str] = field(default_factory=list)
    command_timeout_s: int = 600

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "none" and "llm" not in self.skip_tools

    def tool_enabled(self, tool: str) -> bool:
        return tool not in self.skip_tools


DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-4.1",
    "gemini": "gemini-2.5-flash",
}

API_KEY_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _detect_provider() -> str:
    explicit = _env("LLM_PROVIDER").lower()
    if explicit:
        if explicit not in (*DEFAULT_MODELS, "none"):
            raise ConfigError(f"LLM_PROVIDER must be one of {', '.join([*DEFAULT_MODELS, 'none'])}; got {explicit!r}")
        return explicit
    for provider, var in API_KEY_VARS.items():
        if _env(var):
            return provider
    return "none"


def load_config(*, workspace: str | None = None, dry_run: bool | None = None, output: str | None = None) -> Config:
    ws = Path(workspace or _env("WORKSPACE_DIR") or os.getcwd()).resolve()
    if not ws.is_dir():
        raise ConfigError(f"Workspace directory does not exist: {ws}")

    rules_raw = _env("RULES_FILE_PATH", str(REVIEWER_ROOT / "rules" / "org_policies.md"))
    rules_file = Path(rules_raw)
    if not rules_file.is_absolute():
        # Relative paths are resolved against the reviewed repo first, then the reviewer itself.
        rules_file = ws / rules_raw if (ws / rules_raw).exists() else REVIEWER_ROOT / rules_raw

    scope = _env("REPORT_SCOPE", "changed_files").lower()
    if scope not in SCOPES:
        raise ConfigError(f"REPORT_SCOPE must be one of {SCOPES}; got {scope!r}")

    skip_tools = {t.lower() for t in _env_list("SKIP_TOOLS")}
    unknown = skip_tools - set(ALL_TOOLS)
    if unknown:
        raise ConfigError(f"SKIP_TOOLS contains unknown tools {sorted(unknown)}; valid: {ALL_TOOLS}")

    fail_raw = _env("FAIL_ON_SEVERITY").upper()
    fail_on = None if fail_raw in ("", "NONE", "OFF") else Severity.parse(fail_raw)

    provider = _detect_provider()
    api_key = _env(API_KEY_VARS[provider]) if provider in API_KEY_VARS else ""

    tflint_cfg_raw = _env("TFLINT_CONFIG")
    if tflint_cfg_raw:
        tflint_config: Path | None = Path(tflint_cfg_raw).resolve()
    elif (ws / ".tflint.hcl").exists():
        tflint_config = ws / ".tflint.hcl"
    else:
        tflint_config = REVIEWER_ROOT / "config" / ".tflint.hcl"

    out = output or _env("REPORT_OUTPUT_FILE")
    return Config(
        workspace=ws,
        rules_file=rules_file,
        working_dirs=_env_list("TF_WORKING_DIRS"),
        diff_globs=_env_list("DIFF_GLOBS") or ["*.tf", "*.tfvars"],
        skip_tools=skip_tools,
        scope=scope,
        min_severity=Severity.parse(_env("MIN_SEVERITY", "LOW")),
        fail_on_severity=fail_on,
        output_file=Path(out).resolve() if out else None,
        dry_run=dry_run if dry_run is not None else _env_bool("DRY_RUN", False),
        github_token=_env("GITHUB_TOKEN") or _env("GH_TOKEN"),
        github_api_url=_env("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        github_server_url=_env("GITHUB_SERVER_URL", "https://github.com").rstrip("/"),
        check_run_name=_env("CHECK_RUN_NAME", "Terraform AI Reviewer"),
        comment_marker=_env("COMMENT_MARKER", "<!-- terraform-ai-reviewer:summary -->"),
        llm_provider=provider,
        llm_model=_env("LLM_MODEL", DEFAULT_MODELS.get(provider, "")),
        llm_api_key=api_key,
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 64000 if provider == "anthropic" else 16000),
        llm_timeout_s=_env_int("LLM_TIMEOUT", 600),
        llm_effort=_env("LLM_EFFORT", "high").lower(),
        llm_max_diff_chars=_env_int("LLM_MAX_DIFF_CHARS", 120_000),
        llm_line_tolerance=_env_int("LLM_LINE_TOLERANCE", 3),
        tflint_config=tflint_config if tflint_config and tflint_config.exists() else None,
        checkov_default_severity=Severity.parse(_env("CHECKOV_DEFAULT_SEVERITY", "MEDIUM")),
        checkov_extra_args=_env("CHECKOV_EXTRA_ARGS").split(),
        command_timeout_s=_env_int("COMMAND_TIMEOUT", 600),
    )
