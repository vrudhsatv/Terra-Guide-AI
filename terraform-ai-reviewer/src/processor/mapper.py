"""Unified-diff parsing and finding → changed-line mapping.

Line numbers always refer to the *new* (head) side of the diff, which is what GitHub
uses for the RIGHT side of a pull request and what the checked-out workspace contains.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Iterable

from models import Finding

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffLine:
    kind: str  # "+", "-", " "
    old_no: int | None
    new_no: int | None
    text: str


@dataclass
class Hunk:
    header: str
    new_start: int
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class DiffFile:
    path: str
    old_path: str
    status: str  # added | modified | renamed | deleted
    hunks: list[Hunk] = field(default_factory=list)
    added_lines: set[int] = field(default_factory=set)
    hunk_lines: set[int] = field(default_factory=set)  # every new-side line visible in the diff
    _sorted_added: list[int] = field(default_factory=list, repr=False)

    def finalize(self) -> None:
        self._sorted_added = sorted(self.added_lines)

    def nearest_added(self, line: int, tolerance: int) -> int | None:
        """Closest added line within ``tolerance`` lines of ``line`` (ties → earlier line)."""
        if not self._sorted_added:
            return None
        idx = bisect_left(self._sorted_added, line)
        candidates = []
        if idx < len(self._sorted_added):
            candidates.append(self._sorted_added[idx])
        if idx > 0:
            candidates.append(self._sorted_added[idx - 1])
        best = min(candidates, key=lambda n: (abs(n - line), n))
        return best if abs(best - line) <= tolerance else None

    def range_touches_added(self, start: int, end: int) -> bool:
        idx = bisect_left(self._sorted_added, start)
        return idx < len(self._sorted_added) and self._sorted_added[idx] <= end


def _strip_prefix(path: str) -> str:
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def parse_unified_diff(text: str) -> dict[str, DiffFile]:
    """Parse ``git diff`` output (or the GitHub ``.diff`` media type) into per-file maps."""
    files: dict[str, DiffFile] = {}
    current: DiffFile | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    old_path = new_path = ""
    status = "modified"

    def flush() -> None:
        nonlocal current
        if current is not None and current.status != "deleted":
            current.finalize()
            files[current.path] = current
        current = None

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            flush()
            hunk = None
            old_path = new_path = ""
            status = "modified"
            parts = raw[len("diff --git "):].split(" b/", 1)
            if len(parts) == 2:
                old_path, new_path = _strip_prefix(parts[0]), parts[1]
            continue
        if hunk is None:
            # File header section.
            if raw.startswith("new file mode"):
                status = "added"
            elif raw.startswith("deleted file mode"):
                status = "deleted"
            elif raw.startswith("rename from "):
                old_path = raw[len("rename from "):]
                status = "renamed"
            elif raw.startswith("rename to "):
                new_path = raw[len("rename to "):]
            elif raw.startswith("--- "):
                src = raw[4:]
                if src != "/dev/null":
                    old_path = _strip_prefix(src)
            elif raw.startswith("+++ "):
                dst = raw[4:]
                if dst == "/dev/null":
                    status = "deleted"
                else:
                    new_path = _strip_prefix(dst)
                current = DiffFile(path=new_path or old_path, old_path=old_path, status=status)
        match = _HUNK_RE.match(raw)
        if match:
            if current is None:
                # Diff without ---/+++ headers (e.g. a pure rename with edits is never like this,
                # but be defensive): build the file from the diff --git line.
                current = DiffFile(path=new_path or old_path, old_path=old_path, status=status)
            old_no = int(match.group(1))
            new_no = int(match.group(3))
            hunk = Hunk(header=raw, new_start=new_no)
            current.hunks.append(hunk)
            continue
        if hunk is None or current is None or not raw:
            if hunk is not None and current is not None and raw == "":
                # Some tools strip the trailing space of empty context lines.
                hunk.lines.append(DiffLine(" ", old_no, new_no, ""))
                current.hunk_lines.add(new_no)
                old_no += 1
                new_no += 1
            continue
        marker, body = raw[0], raw[1:]
        if marker == "+":
            hunk.lines.append(DiffLine("+", None, new_no, body))
            current.added_lines.add(new_no)
            current.hunk_lines.add(new_no)
            new_no += 1
        elif marker == "-":
            hunk.lines.append(DiffLine("-", old_no, None, body))
            old_no += 1
        elif marker == " ":
            hunk.lines.append(DiffLine(" ", old_no, new_no, body))
            current.hunk_lines.add(new_no)
            old_no += 1
            new_no += 1
        elif marker == "\\":
            continue  # "\ No newline at end of file"
        else:
            # Anything else ends the hunk (e.g. next file's metadata in odd outputs).
            hunk = None
    flush()
    return files


class DiffMap:
    """Query helper over a parsed diff."""

    def __init__(self, files: dict[str, DiffFile]):
        self.files = files

    @classmethod
    def from_text(cls, text: str) -> "DiffMap":
        return cls(parse_unified_diff(text))

    @property
    def changed_files(self) -> list[str]:
        return sorted(self.files)

    def get(self, path: str) -> DiffFile | None:
        return self.files.get(path)

    def is_added_line(self, path: str, line: int) -> bool:
        diff_file = self.files.get(path)
        return bool(diff_file and line in diff_file.added_lines)

    def nearest_added(self, path: str, line: int, tolerance: int) -> int | None:
        diff_file = self.files.get(path)
        return diff_file.nearest_added(line, tolerance) if diff_file else None

    def touches_diff(self, finding: Finding) -> bool:
        diff_file = self.files.get(finding.file)
        if diff_file is None or finding.line <= 0:
            return False
        end = finding.end_line if finding.end_line and finding.end_line >= finding.line else finding.line
        return diff_file.range_touches_added(finding.line, end)

    def render_for_prompt(self, paths: Iterable[str]) -> str:
        """Render every hunk of ``paths``; see :meth:`render_hunks`."""
        return self.render_hunks(
            [(path, idx) for path in paths if path in self.files for idx in range(len(self.files[path].hunks))]
        )

    def render_hunks(self, selection: Iterable[tuple[str, int]]) -> str:
        """Render selected (path, hunk index) pairs with explicit new-side line numbers.

        Format per line: ``<new_line_no or blank> <marker> <code>``. Removed lines carry no
        new line number, which makes it obvious they cannot be referenced.
        """
        out: list[str] = []
        current_path: str | None = None
        for path, idx in selection:
            diff_file = self.files[path]
            if path != current_path:
                if current_path is not None:
                    out.append("")
                out.append(f"### File: {path} ({diff_file.status})")
                current_path = path
            hunk = diff_file.hunks[idx]
            out.append(hunk.header)
            for line in hunk.lines:
                number = f"{line.new_no:>5}" if line.new_no is not None else "     "
                out.append(f"{number} {line.kind} {line.text}")
        return "\n".join(out) + ("\n" if out else "")

    def chunk_hunks(self, budget_chars: int) -> list[list[tuple[str, int]]]:
        """Group hunks into prompt-sized chunks without ever cutting a hunk in half.

        A single hunk larger than the budget becomes its own chunk rather than being truncated.
        """
        chunks: list[list[tuple[str, int]]] = []
        current: list[tuple[str, int]] = []
        size = 0
        for path in self.changed_files:
            header = len(path) + 32
            for idx, hunk in enumerate(self.files[path].hunks):
                cost = header + len(hunk.header) + sum(len(l.text) + 9 for l in hunk.lines)
                if current and size + cost > budget_chars:
                    chunks.append(current)
                    current, size = [], 0
                current.append((path, idx))
                size += cost
        if current:
            chunks.append(current)
        return chunks


def map_findings(findings: list[Finding], diff_map: DiffMap) -> None:
    """Flag each finding that touches an added/modified line of the PR."""
    for finding in findings:
        finding.in_diff = diff_map.touches_diff(finding)


def apply_scope(findings: list[Finding], diff_map: DiffMap, scope: str) -> tuple[list[Finding], int]:
    """Filter findings to the configured scope. Repository-level findings (no file) always stay."""
    if scope == "all":
        return findings, 0
    changed = set(diff_map.changed_files)
    kept: list[Finding] = []
    for finding in findings:
        if not finding.file:
            kept.append(finding)
        elif scope == "changed_files" and finding.file in changed:
            kept.append(finding)
        elif scope == "changed_lines" and finding.in_diff:
            kept.append(finding)
    return kept, len(findings) - len(kept)
