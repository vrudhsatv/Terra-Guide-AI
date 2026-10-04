from pathlib import Path

import pytest

from llm.rules import FREE_TEXT_RULE_ID, load_rules
from llm.validator import FindingValidator
from models import Severity, Source
from processor.mapper import DiffMap

RULES = """\
# Policies

## ORG-001: Mandatory tags
Severity: LOW
All resources need tags.

### Example
tags = { Owner = "x" }

## ORG-002: No public buckets
**Severity:** CRITICAL
No public ACLs.

## ORG-003: Something without severity
Text.

# Appendix
Not a rule.
"""

MAIN_TF = """\
resource "aws_s3_bucket" "logs" {
  bucket = "prod-logs"
  acl    = "public-read"
}

resource "aws_sqs_queue" "q" {
  name = "q"
}
"""

DIFF = """\
diff --git a/infra/main.tf b/infra/main.tf
--- a/infra/main.tf
+++ b/infra/main.tf
@@ -1,4 +1,4 @@
 resource "aws_s3_bucket" "logs" {
   bucket = "prod-logs"
-  acl    = "private"
+  acl    = "public-read"
 }
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "main.tf").write_text(MAIN_TF)
    (tmp_path / "rules.md").write_text(RULES)
    return tmp_path


def test_rules_parsing(workspace):
    rules = load_rules(workspace / "rules.md")
    assert set(rules.rules) == {"ORG-001", "ORG-002", "ORG-003"}
    assert rules.rules["ORG-001"].severity is Severity.LOW
    assert "### Example" in rules.rules["ORG-001"].body  # deeper headings stay in the rule
    assert rules.rules["ORG-002"].severity is Severity.CRITICAL and rules.rules["ORG-002"].severity_explicit
    assert not rules.rules["ORG-003"].severity_explicit
    assert "Appendix" not in rules.rules["ORG-003"].body


def test_missing_rules_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_rules(tmp_path / "nope.md")


def _validator(workspace, tolerance=3, rules_text=None):
    if rules_text is not None:
        (workspace / "rules.md").write_text(rules_text)
    return FindingValidator(DiffMap.from_text(DIFF), load_rules(workspace / "rules.md"), workspace, tolerance)


def _item(**overrides):
    base = {
        "rule_id": "ORG-002",
        "file": "infra/main.tf",
        "line": 3,
        "severity": "HIGH",
        "title": "Public ACL",
        "description": "public-read exposes data",
        "evidence": 'acl    = "public-read"',
        "recommendation": "Use private",
    }
    base.update(overrides)
    return base


def test_accepts_valid_finding_and_enforces_policy_severity(workspace):
    finding, reason = _validator(workspace).validate(_item())
    assert reason == ""
    assert finding.source is Source.LLM and finding.line == 3
    assert finding.severity is Severity.CRITICAL  # the policy file wins over the model


def test_reanchors_off_by_n_line_using_evidence(workspace):
    finding, _ = _validator(workspace).validate(_item(line=1, evidence='    3 +   acl    = "public-read"'))
    assert finding is not None and finding.line == 3


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"file": "infra/other.tf"}, "not part of the PR diff"),
        ({"rule_id": "ORG-999"}, "does not exist"),
        ({"line": 99}, "outside"),
        ({"line": "abc"}, "not an integer"),
        ({"evidence": 'acl = "authenticated-read"'}, "evidence not found"),
        ({"title": ""}, "missing fields"),
    ],
)
def test_rejects_hallucinations(workspace, overrides, reason):
    finding, why = _validator(workspace).validate(_item(**overrides))
    assert finding is None and reason in why


def test_rejects_line_outside_changes_without_evidence(workspace):
    finding, why = _validator(workspace, tolerance=1).validate(_item(line=7, evidence=""))
    assert finding is None and "not part of the changes" in why


def test_snaps_to_nearest_changed_line_without_evidence(workspace):
    finding, _ = _validator(workspace).validate(_item(line=4, evidence=""))
    assert finding.line == 3


def test_free_text_rules_accept_any_rule_id(workspace):
    finding, _ = _validator(workspace, rules_text="Buckets must be private.").validate(_item(rule_id="whatever"))
    assert finding.rule_id == FREE_TEXT_RULE_ID and finding.severity is Severity.HIGH


def test_normalizes_diff_prefix_in_path(workspace):
    finding, _ = _validator(workspace).validate(_item(file="b/infra/main.tf"))
    assert finding is not None and finding.file == "infra/main.tf"
