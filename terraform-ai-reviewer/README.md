# Terraform AI Reviewer

Advisory pull-request reviewer for any Terraform repository. It combines deterministic tools with an LLM:

| Stage | What runs | Output |
|---|---|---|
| 1. Deterministic | `terraform init` / `validate` / `fmt -check`, `tflint`, `checkov` | Normalized findings (file, line, severity, message) |
| 2. LLM policy check | PR diff + `rules/org_policies.md` + stage 1 results → Anthropic / OpenAI / Gemini | Policy findings. Any finding that can't be traced to a real changed line, a real rule ID and verbatim evidence is discarded |
| 3. Process and publish | Dedupe, rank, map to changed lines | **One sticky PR comment, edited in place on every push**, plus a Check Run with line annotations |

It never merges, applies or deploys. The job passes unless you set `FAIL_ON_SEVERITY`.

```
src/
├── main.py                  CLI entrypoint (all platforms)
├── config.py / ci_context.py   env config; PR detection for GitHub Actions, CodeBuild, CircleCI
├── git_utils.py             PR diff (git three-dot diff, GitHub API fallback)
├── deterministic/runner.py, parsers.py
├── llm/engine.py, prompts.py, rules.py, validator.py
├── processor/deduplicator.py, mapper.py
└── publisher/github_publisher.py
rules/org_policies.md        editable policy text (replace the sample)
scripts/install_tools.sh     pinned terraform/tflint/checkov installer (Docker, CodeBuild, CircleCI)
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | — | Publish comment and check run |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | — | LLM key. If none is set, the LLM stage is skipped |
| `LLM_PROVIDER` | first key found | `anthropic`, `openai`, `gemini` or `none` |
| `LLM_MODEL` | `claude-opus-5-5` / `gpt-4.1` / `gemini-2.5-flash` | Model override |
| `LLM_EFFORT` | `high` | Anthropic effort level |
| `RULES_FILE_PATH` | bundled `rules/org_policies.md` | Policy file (relative paths are resolved against the reviewed repo first) |
| `TF_WORKING_DIRS` | dirs of changed `.tf` files | Comma-separated directories to scan |
| `REPORT_SCOPE` | `changed_files` | `changed_lines`, `changed_files` or `all` |
| `MIN_SEVERITY` | `LOW` | Hide findings below this severity |
| `FAIL_ON_SEVERITY` | empty (advisory) | Exit 1 when a finding is at or above this severity |
| `SKIP_TOOLS` | — | Any of `init,validate,fmt,tflint,checkov,llm` |
| `PR_NUMBER`, `BASE_SHA`, `HEAD_SHA`, `BASE_REF`, `GITHUB_REPOSITORY` | auto-detected | Explicit PR context for any CI system |

Other settings: `LLM_MAX_TOKENS`, `LLM_TIMEOUT`, `LLM_MAX_DIFF_CHARS` (large diffs are split by hunk and never truncated), `LLM_LINE_TOLERANCE`, `TFLINT_CONFIG`, `CHECKOV_DEFAULT_SEVERITY`, `CHECKOV_EXTRA_ARGS`, `CHECK_RUN_NAME`, `COMMENT_MARKER`, `DIFF_GLOBS`.

### Policy file format
```markdown
## ORG-002: No public S3 buckets
Severity: CRITICAL
Explanation, examples, exceptions…
```
IDs let the validator reject findings that cite a rule that doesn't exist. A declared `Severity:` overrides whatever severity the model picks. A file with no IDs still works, but it is treated as free text.

## Integration

### GitHub Actions (primary)
* **In this repository:** `../.github/workflows/terraform-ai-reviewer.yml` builds the image from this folder and runs on PRs that touch `*.tf`.
* **In any other repository:** host this folder as its own repo, then call the reusable workflow:
  ```yaml
  jobs:
    terraform-review:
      uses: your-org/terraform-ai-reviewer/.github/workflows/pr-reviewer.yml@main
      with:
        reviewer_repository: your-org/terraform-ai-reviewer
      secrets: inherit
  ```
  Needed permissions: `contents: read`, `pull-requests: write`, `checks: write`. Add `ANTHROPIC_API_KEY` (or another provider's key) as a repository secret. PRs from forks get no secrets and a read-only token, so on those the review only appears in the job log.

### AWS CodeBuild / CodePipeline (secondary)
1. Create a Secrets Manager secret `terraform-ai-reviewer` (JSON) with `GITHUB_TOKEN` and optionally `ANTHROPIC_API_KEY`. Give the CodeBuild role `secretsmanager:GetSecretValue`.
2. **CodeBuild webhook (recommended):** a GitHub source with filter events `PULL_REQUEST_CREATED, PULL_REQUEST_UPDATED, PULL_REQUEST_REOPENED`, using `buildspec.yml`. Add this repo as a secondary source with identifier `reviewer`, or set `REVIEWER_REPO_URL`.
3. **CodePipeline V2:** use a GitHub (CodeConnections) source with a pull-request trigger, then a CodeBuild action with the env overrides `HEAD_SHA=#{SourceVariables.CommitId}`, `GITHUB_REPOSITORY=#{SourceVariables.FullRepositoryName}` and `BASE_REF=main`. The PR number is looked up from the commit.

A PAT can't create Check Runs, so on AWS the reviewer posts a commit status next to the sticky comment.

### CircleCI (third)
Copy `.circleci/config.yml` into the repo. Turn on "Only build pull requests" and create a context `terraform-ai-reviewer` holding `GITHUB_TOKEN` (plus an LLM key). For other repos, set the `reviewer_repo_url` parameter.

### Docker (anywhere)
```bash
docker build -t terraform-ai-reviewer .
docker run --rm -v "$PWD:/workspace" -e GITHUB_TOKEN -e ANTHROPIC_API_KEY \
  -e GITHUB_REPOSITORY=org/repo -e PR_NUMBER=42 terraform-ai-reviewer
```

## Run and test locally
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
./scripts/install_tools.sh            # or: docker build
pytest -q                             # unit tests, no tools/keys needed

# Review the current branch against main without posting anything:
cd /path/to/terraform-repo
python /path/to/terraform-ai-reviewer/src/main.py --base main --dry-run --output report.json
# Add ANTHROPIC_API_KEY=... to include the policy review; SKIP_TOOLS=tflint,checkov to go faster.
```
Exit codes: `0` review finished, `1` `FAIL_ON_SEVERITY` reached, `2` configuration error.
