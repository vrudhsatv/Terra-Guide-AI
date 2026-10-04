"""Stage 1: run terraform init/validate/fmt, tflint and checkov for each working directory."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from config import Config
from deterministic import parsers
from models import Finding, ToolResult, ToolStatus

log = logging.getLogger(__name__)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_command(args: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> CommandResult:
    log.debug("$ (cd %s && %s)", cwd, " ".join(args))
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=-1,
            stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=f"Command timed out after {timeout}s: {' '.join(args)}",
            timed_out=True,
        )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class _Accumulator:
    """Merges per-directory outcomes of one tool into a single ToolResult."""

    def __init__(self, tool: str):
        self.tool = tool
        self.findings: list[Finding] = []
        self.errors: list[str] = []
        self.started = time.monotonic()
        self.ran = False

    def add(self, findings: list[Finding]) -> None:
        self.ran = True
        self.findings.extend(findings)

    def error(self, working_dir: str, message: str) -> None:
        self.ran = True
        self.errors.append(f"`{working_dir}`: {message.strip()[:500]}")

    def result(self) -> ToolResult:
        duration = time.monotonic() - self.started
        if not self.ran:
            return ToolResult(self.tool, ToolStatus.SKIPPED, message="no directories to scan", duration_s=duration)
        if self.errors:
            status = ToolStatus.ERROR
        elif self.findings:
            status = ToolStatus.FAILED
        else:
            status = ToolStatus.PASSED
        return ToolResult(self.tool, status, self.findings, "; ".join(self.errors), duration)


def _skipped(tool: str, reason: str) -> ToolResult:
    return ToolResult(tool, ToolStatus.SKIPPED, message=reason)


class DeterministicRunner:
    def __init__(self, config: Config):
        self.config = config
        self.workspace = config.workspace
        self.timeout = config.command_timeout_s
        self.tf_env = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1"}

    # ------------------------------------------------------------------ public

    def run(self, working_dirs: list[str]) -> list[ToolResult]:
        results: list[ToolResult] = []
        self._run_terraform(working_dirs, results)
        results.append(self._run_tflint(working_dirs))
        results.append(self._run_checkov(working_dirs))
        log.info(
            "Deterministic stage: %s",
            ", ".join(f"{r.tool}={r.status.value}({len(r.findings)})" for r in results),
        )
        return results

    # ------------------------------------------------------------------ terraform

    def _run_terraform(self, working_dirs: list[str], results: list[ToolResult]) -> None:
        terraform = shutil.which("terraform")
        wanted = [t for t in ("init", "validate", "fmt") if self.config.tool_enabled(t)]
        if not terraform:
            results.extend(_skipped(f"terraform {t}", "terraform binary not found on PATH") for t in wanted)
            return

        init_acc, validate_acc, fmt_acc = (_Accumulator(f"terraform {t}") for t in ("init", "validate", "fmt"))
        initialized: set[str] = set()
        for wd in working_dirs:
            cwd = self.workspace / wd
            if self.config.tool_enabled("init"):
                res = run_command(
                    [terraform, "init", "-backend=false", "-input=false", "-no-color"], cwd, self.timeout, self.tf_env
                )
                if res.returncode == 0:
                    initialized.add(wd)
                    init_acc.add([])
                else:
                    init_acc.add([parsers.parse_terraform_init_failure(res.stderr or res.stdout, wd)])

            # validate needs a successful init; an init failure is already reported as a finding.
            if self.config.tool_enabled("validate") and (wd in initialized or not self.config.tool_enabled("init")):
                res = run_command([terraform, "validate", "-json", "-no-color"], cwd, self.timeout, self.tf_env)
                try:
                    validate_acc.add(parsers.parse_terraform_validate(res.stdout, wd, self.workspace))
                except ValueError as exc:
                    validate_acc.error(wd, f"{exc} {res.stderr}")

            if self.config.tool_enabled("fmt"):
                res = run_command(
                    [terraform, "fmt", "-check", "-diff", "-list=true", "-no-color"], cwd, self.timeout, self.tf_env
                )
                if res.returncode in (0, 3):  # 3 = files need formatting
                    fmt_acc.add(parsers.parse_terraform_fmt(res.stdout, wd, self.workspace))
                else:
                    # Exit 2 typically means a syntax error; validate reports it with a location.
                    fmt_acc.error(wd, parsers.first_error_block(res.stderr) or f"exit code {res.returncode}")

        for tool, acc in (("init", init_acc), ("validate", validate_acc), ("fmt", fmt_acc)):
            if not self.config.tool_enabled(tool):
                continue
            result = acc.result()
            if tool == "validate" and not acc.ran and working_dirs:
                result = _skipped(acc.tool, "skipped because terraform init failed")
            results.append(result)

    # ------------------------------------------------------------------ tflint

    def _run_tflint(self, working_dirs: list[str]) -> ToolResult:
        if not self.config.tool_enabled("tflint"):
            return _skipped("tflint", "disabled via SKIP_TOOLS")
        tflint = shutil.which("tflint")
        if not tflint:
            return _skipped("tflint", "tflint binary not found on PATH")
        config_args = ["--config", str(self.config.tflint_config)] if self.config.tflint_config else []
        acc = _Accumulator("tflint")
        init_done = False
        for wd in working_dirs:
            cwd = self.workspace / wd
            if not init_done:
                # Plugins are installed into ~/.tflint.d once and shared by every directory.
                init = run_command([tflint, "--init", *config_args], cwd, self.timeout)
                if init.returncode != 0:
                    acc.error(wd, f"tflint --init failed: {init.stderr or init.stdout}")
                    return acc.result()
                init_done = True
            res = run_command([tflint, "--format=json", "--no-color", "--force", *config_args], cwd, self.timeout)
            try:
                findings, errors = parsers.parse_tflint(res.stdout, wd, self.workspace)
            except ValueError as exc:
                acc.error(wd, f"{exc} {res.stderr}")
                continue
            acc.add(findings)
            for err in errors:
                acc.error(wd, err)
        return acc.result()

    # ------------------------------------------------------------------ checkov

    def _run_checkov(self, working_dirs: list[str]) -> ToolResult:
        if not self.config.tool_enabled("checkov"):
            return _skipped("checkov", "disabled via SKIP_TOOLS")
        checkov = shutil.which("checkov")
        if not checkov:
            return _skipped("checkov", "checkov not found on PATH")
        acc = _Accumulator("checkov")
        for wd in working_dirs:
            res = run_command(
                [
                    checkov,
                    "--directory", wd,
                    "--framework", "terraform",
                    "--output", "json",
                    "--quiet",
                    "--compact",
                    "--soft-fail",
                    *self.config.checkov_extra_args,
                ],
                self.workspace,
                self.timeout,
                {"LOG_LEVEL": "ERROR"},
            )
            if res.timed_out:
                acc.error(wd, res.stderr)
                continue
            try:
                acc.add(
                    parsers.parse_checkov(res.stdout, wd, self.workspace, self.config.checkov_default_severity)
                )
            except ValueError as exc:
                acc.error(wd, f"{exc} {res.stderr[-500:]}")
        return acc.result()


def discover_working_dirs(config: Config, changed_files: list[str]) -> list[str]:
    """Directories to scan: TF_WORKING_DIRS, else directories of changed .tf files, else every Terraform dir."""
    ws = config.workspace
    if config.working_dirs:
        dirs = [Path(os.path.normpath(d)).as_posix() for d in config.working_dirs]
        missing = [d for d in dirs if not (ws / d).is_dir()]
        if missing:
            log.warning("Ignoring TF_WORKING_DIRS entries that do not exist: %s", missing)
        return [d for d in dirs if (ws / d).is_dir()]

    if changed_files:
        dirs = {
            Path(f).parent.as_posix()
            for f in changed_files
            if f.endswith(".tf") and (ws / f).is_file()
        }
        return sorted(dirs)

    skip_parts = {".terraform", ".git", "node_modules", ".venv", "venv"}
    found = {
        p.parent.relative_to(ws).as_posix()
        for p in ws.rglob("*.tf")
        if not skip_parts.intersection(p.relative_to(ws).parts)
    }
    return sorted(found)
