import json
from pathlib import Path

from deterministic import parsers
from models import Severity, Source

WS = Path("/repo")

# Captured from `terraform fmt -check -diff -list=true -no-color` (Terraform 1.14).
FMT_OUTPUT = """\
main.tf
--- old/main.tf
+++ new/main.tf
@@ -3,10 +3,10 @@
 }
 
 locals {
-  x    = 1
+  x         = 1
   long_name = 2
 }
 
 output "o" {
-    value = local.x
+  value = local.x
 }
vars.tf
--- old/vars.tf
+++ new/vars.tf
@@ -1,3 +1,3 @@
 variable "b" {
-default = 1
+  default = 1
 }
"""

VALIDATE_OUTPUT = json.dumps(
    {
        "valid": False,
        "diagnostics": [
            {
                "severity": "error",
                "summary": "Reference to undeclared input variable",
                "detail": 'An input variable with the name "missing" has not been declared.',
                "range": {"filename": "bad.tf", "start": {"line": 2}, "end": {"line": 2}},
                "snippet": {"code": "  value = var.missing"},
            },
            {"severity": "warning", "summary": "Deprecated thing", "detail": "no range"},
        ],
    }
)

TFLINT_OUTPUT = json.dumps(
    {
        "issues": [
            {
                "rule": {"name": "terraform_unused_declarations", "severity": "warning", "link": "https://x/doc"},
                "message": 'variable "a" is declared but not used',
                "range": {"filename": "main.tf", "start": {"line": 1}, "end": {"line": 1}},
            },
            {
                "rule": {"name": "aws_instance_invalid_type", "severity": "error", "link": ""},
                "message": '"t1.2xlarge" is an invalid value as instance_type',
                "range": {"filename": "modules/ec2/main.tf", "start": {"line": 7}, "end": {"line": 7}},
            },
        ],
        "errors": [],
    }
)

CHECKOV_SINGLE = {
    "check_type": "terraform",
    "results": {
        "failed_checks": [
            {
                "check_id": "CKV_AWS_20",
                "check_name": "S3 Bucket has an ACL defined which allows public READ access.",
                "file_path": "/main.tf",
                "file_abs_path": "/does/not/exist/main.tf",
                "file_line_range": [1, 4],
                "resource": "aws_s3_bucket.logs",
                "severity": None,
                "guideline": "https://docs.example/ckv_aws_20",
            },
            {
                "check_id": "CKV_AWS_18",
                "check_name": "Ensure the S3 bucket has access logging enabled",
                "file_path": "/main.tf",
                "file_line_range": [1, 4],
                "resource": "aws_s3_bucket.logs",
                "severity": "LOW",
            },
        ]
    },
}


def test_fmt_reports_first_changed_line_per_file():
    findings = parsers.parse_terraform_fmt(FMT_OUTPUT, "infra", WS)
    assert [(f.file, f.line) for f in findings] == [("infra/main.tf", 6), ("infra/vars.tf", 2)]
    assert all(f.source is Source.TERRAFORM_FMT and f.severity is Severity.LOW for f in findings)
    assert "2 formatting hunk" not in findings[0].description  # one hunk in main.tf
    assert findings[0].description.startswith("1 formatting hunk")


def test_fmt_list_only_output():
    findings = parsers.parse_terraform_fmt("main.tf\n", ".", WS)
    assert [(f.file, f.line) for f in findings] == [("main.tf", 1)]


def test_validate():
    findings = parsers.parse_terraform_validate(VALIDATE_OUTPUT, "infra", WS)
    assert findings[0].file == "infra/bad.tf" and findings[0].line == 2
    assert findings[0].severity is Severity.HIGH
    assert findings[0].evidence == "value = var.missing"
    assert findings[1].file == "" and findings[1].severity is Severity.MEDIUM
    assert "infra" in findings[1].title


def test_init_failure():
    f = parsers.parse_terraform_init_failure("\x1b[31mjunk\nError: Failed to query provider\nmore", "infra")
    assert f.description.startswith("Error: Failed to query provider") and f.severity is Severity.HIGH


def test_tflint():
    findings, errors = parsers.parse_tflint(TFLINT_OUTPUT, "infra", WS)
    assert errors == []
    assert [(f.file, f.line, f.severity) for f in findings] == [
        ("infra/main.tf", 1, Severity.MEDIUM),
        ("infra/modules/ec2/main.tf", 7, Severity.HIGH),
    ]
    assert findings[0].guideline == "https://x/doc"


def test_checkov_dict_and_list_and_default_severity():
    for payload in (CHECKOV_SINGLE, [CHECKOV_SINGLE, {"check_type": "secrets", "results": {}}]):
        findings = parsers.parse_checkov(json.dumps(payload), "infra", WS, Severity.MEDIUM)
        assert [(f.rule_id, f.file, f.line, f.end_line, f.severity) for f in findings] == [
            ("CKV_AWS_20", "infra/main.tf", 1, 4, Severity.MEDIUM),
            ("CKV_AWS_18", "infra/main.tf", 1, 4, Severity.LOW),
        ]


def test_checkov_summary_only_and_banner_noise():
    assert parsers.parse_checkov('{"passed": 0, "failed": 0}', ".", WS, Severity.MEDIUM) == []
    noisy = "Some warning line\n" + json.dumps(CHECKOV_SINGLE)
    assert len(parsers.parse_checkov(noisy, ".", WS, Severity.MEDIUM)) == 2


def test_normalize_path_absolute_inside_workspace(tmp_path):
    (tmp_path / "infra").mkdir()
    target = tmp_path / "infra" / "main.tf"
    target.write_text("")
    assert parsers.normalize_path(str(target), "infra", tmp_path) == "infra/main.tf"
