"""System and review prompts for the organizational-policy LLM pass."""

from __future__ import annotations

import json

from models import Finding

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]

# JSON Schema for the model's answer. Used for native structured output where the provider
# supports it and embedded in the prompt for the others; the validator re-checks it either way.
REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-4 sentence assessment of how the change measures up against the organizational policies.",
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string", "description": "ID of the violated policy exactly as written in the policy file."},
                    "file": {"type": "string", "description": "File path exactly as shown after '### File:'."},
                    "line": {"type": "integer", "description": "New-side line number shown in the left column of the diff."},
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "title": {"type": "string", "description": "One-line statement of the violation."},
                    "description": {"type": "string", "description": "Why this line violates the policy."},
                    "evidence": {"type": "string", "description": "Verbatim code copied from the cited line (without the line number or +/- marker)."},
                    "recommendation": {"type": "string", "description": "Concrete fix, ideally as corrected HCL."},
                },
                "required": ["rule_id", "file", "line", "severity", "title", "description", "evidence", "recommendation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a principal DevOps engineer reviewing a Terraform pull request on behalf of the \
platform team. Your only job in this pass is to check the changed Terraform code against the organization's \
written policies, which are given to you below.

Deterministic tools (terraform validate, terraform fmt, tflint, checkov) have already run. Their findings are \
listed so you do not repeat them: syntax, formatting, lint and generic security-scanner issues are out of scope \
for you unless a written policy demands something stricter than what the tool reported.

How to review:
- Read every policy, then read the diff. Lines prefixed with "+" were added or modified by this PR; lines \
prefixed with a space are unchanged context; lines prefixed with "-" were removed and have no new line number.
- Report a finding only when an added or modified line ("+") violates a specific written policy, or when the \
PR adds a resource that is missing something a policy requires. For a missing attribute, cite the line that \
opens the resource block.
- Cite the new-side line number shown in the left column, the file path exactly as shown after "### File:", \
and the rule ID exactly as written in the policy file. Copy the cited line's code verbatim into "evidence".
- Use the severity the policy assigns. If the policy states none, judge it: CRITICAL for exposure of data or \
credentials to the internet, HIGH for security or compliance gaps, MEDIUM for reliability or cost risks, LOW \
for conventions such as naming and tagging.
- Respect exceptions the policies describe (for example, an explicit exemption comment).
- One finding per violation per location. If the same violation repeats on many resources, report each \
resource.
- If nothing violates a policy, return an empty findings list. An empty list is a good outcome, not a failure; \
findings are checked automatically against the diff and anything that cannot be traced to a real changed line \
and a real policy is discarded.

Respond with a single JSON object matching the schema you are given and nothing else."""


def build_review_prompt(
    rules_text: str,
    rendered_diff: str,
    deterministic: list[Finding],
    *,
    chunk_index: int,
    chunk_count: int,
    include_schema: bool,
) -> str:
    det = [
        {
            "tool": f.source.value,
            "rule": f.rule_id,
            "severity": f.severity.value,
            "location": f.location,
            "message": f.title,
        }
        for f in deterministic
    ]
    parts = [
        "<organizational_policies>",
        rules_text.strip(),
        "</organizational_policies>",
        "",
        "<deterministic_findings>",
        json.dumps(det, indent=1) if det else "[]",
        "</deterministic_findings>",
        "",
    ]
    if chunk_count > 1:
        parts.append(
            f"This PR is large, so the diff is split into {chunk_count} parts; this is part {chunk_index + 1}. "
            "Review only the files in this part.\n"
        )
    parts += ["<diff>", rendered_diff.rstrip(), "</diff>", ""]
    if include_schema:
        parts += [
            "Return JSON that validates against this schema:",
            json.dumps(REVIEW_SCHEMA, indent=1),
            "",
        ]
    parts.append("Review the diff against the organizational policies and return the JSON object.")
    return "\n".join(parts)
