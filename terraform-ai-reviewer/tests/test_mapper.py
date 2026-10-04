from models import Finding, Severity, Source
from processor.mapper import DiffMap, apply_scope, map_findings

DIFF = """\
diff --git a/infra/main.tf b/infra/main.tf
index 1111111..2222222 100644
--- a/infra/main.tf
+++ b/infra/main.tf
@@ -1,6 +1,7 @@
 resource "aws_s3_bucket" "logs" {
-  bucket = "old-name"
+  bucket = "new-name"
+  acl    = "public-read"
 }
 
 resource "aws_sqs_queue" "q" {
@@ -20,3 +21,4 @@ resource "aws_sqs_queue" "q" {
   name = "q"
 }
+# trailing comment
\\ No newline at end of file
diff --git a/infra/new.tf b/infra/new.tf
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/infra/new.tf
@@ -0,0 +1,2 @@
+variable "x" {}
+variable "y" {}
diff --git a/infra/gone.tf b/infra/gone.tf
deleted file mode 100644
--- a/infra/gone.tf
+++ /dev/null
@@ -1 +0,0 @@
-variable "z" {}
diff --git a/old/name.tf b/infra/renamed.tf
similarity index 90%
rename from old/name.tf
rename to infra/renamed.tf
--- a/old/name.tf
+++ b/infra/renamed.tf
@@ -1,2 +1,2 @@
-locals { a = 1 }
+locals { a = 2 }
 locals { b = 1 }
"""


def test_parse_added_lines_and_statuses():
    dm = DiffMap.from_text(DIFF)
    assert dm.changed_files == ["infra/main.tf", "infra/new.tf", "infra/renamed.tf"]
    main = dm.get("infra/main.tf")
    assert main.added_lines == {2, 3, 23}
    assert {1, 4, 5, 6, 21, 22}.issubset(main.hunk_lines)
    assert dm.get("infra/new.tf").status == "added"
    assert dm.get("infra/new.tf").added_lines == {1, 2}
    renamed = dm.get("infra/renamed.tf")
    assert renamed.status == "renamed" and renamed.old_path == "old/name.tf"
    assert renamed.added_lines == {1}


def test_nearest_added_respects_tolerance():
    dm = DiffMap.from_text(DIFF)
    assert dm.nearest_added("infra/main.tf", 5, 3) == 3
    assert dm.nearest_added("infra/main.tf", 12, 3) is None
    assert dm.nearest_added("missing.tf", 1, 3) is None


def _finding(file, line, end=None):
    return Finding(Source.CHECKOV, "CKV_X", Severity.HIGH, file, line, "t", end_line=end)


def test_map_findings_uses_ranges():
    dm = DiffMap.from_text(DIFF)
    fs = [_finding("infra/main.tf", 1, 4), _finding("infra/main.tf", 7, 9), _finding("other.tf", 1)]
    map_findings(fs, dm)
    assert [f.in_diff for f in fs] == [True, False, False]


def test_apply_scope():
    dm = DiffMap.from_text(DIFF)
    fs = [_finding("infra/main.tf", 2), _finding("infra/main.tf", 8), _finding("other.tf", 1), _finding("", 0)]
    map_findings(fs, dm)
    kept, hidden = apply_scope(fs, dm, "changed_lines")
    assert [(f.file, f.line) for f in kept] == [("infra/main.tf", 2), ("", 0)] and hidden == 2
    kept, hidden = apply_scope(fs, dm, "changed_files")
    assert len(kept) == 3 and hidden == 1
    assert apply_scope(fs, dm, "all") == (fs, 0)


def test_render_has_new_side_numbers_only():
    dm = DiffMap.from_text(DIFF)
    text = dm.render_for_prompt(["infra/main.tf"])
    assert "### File: infra/main.tf (modified)" in text
    assert '    2 +   bucket = "new-name"' in text
    assert '      -   bucket = "old-name"' in text


def test_chunking_never_splits_hunks():
    dm = DiffMap.from_text(DIFF)
    chunks = dm.chunk_hunks(budget_chars=10)
    flat = [item for chunk in chunks for item in chunk]
    assert len(flat) == sum(len(f.hunks) for f in dm.files.values())
    assert all(len(c) == 1 for c in chunks)
    assert len(dm.chunk_hunks(budget_chars=1_000_000)) == 1
