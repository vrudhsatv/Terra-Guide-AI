# AI-Powered Terraform PR Reviewer — POC

## 1. Core Technologies

- GitHub Actions
- GitHub Pull Request
- Terraform
- Terrascan (deterministic IaC security scanning)
- AWS Lambda (serverless AI review agent)
- AWS Secrets Manager (API key storage)
- AI Reviewer (Gemini)
- Risk-Based Decision Policy
- CI Merge Gate

## 2. Core Principle

A deterministic scanner identifies objective security issues, the AI applies a strict risk-based decision policy on top of those findings, and the CI pipeline enforces the resulting verdict as a merge gate.

The system does not deploy infrastructure from the review pipeline and does not merge Pull Requests. It fails the PR check when the AI verdict is **REJECT**; the human reviewer retains the ability to inspect, override, and make the final merge decision.

The pipeline is **fail-closed**: if the AI response is unclear or any error occurs, the verdict defaults to **REJECT**. A silent approval is never possible.

## 3. Problem Statement

Terraform Pull Requests require reviewers to check multiple areas at the same time.

A reviewer may need to determine:

**Security**
- Is a security group publicly accessible?
- Are IAM permissions too broad?
- Is encryption enabled?
- Is an S3 bucket publicly accessible?
- Is the load balancer serving traffic without HTTPS?

**Risk Level**
- How severe are the issues, taken together?
- Is the change safe enough to ship now, safe with follow-up fixes, or unsafe?

**Consistency**
- Different reviewers apply different thresholds.
- Manual severity judgment is slow and inconsistent across PRs.

The goal of this POC is to demonstrate that a scanner + AI combination can apply a **consistent, policy-driven risk decision** to every Terraform PR automatically.

## 4. Solution Overview

The POC follows a **deterministic scan + AI decision** architecture, deployed on serverless AWS.

### 4.1 High-Level Flow

```
Terraform PR
     |
     v
GitHub Actions
     |
     v
Terrascan Scan          (deterministic pass)
     |
     v
Findings Extractor      (normalize scan output)
     |
     v
Prompt Builder          (findings + decision policy)
     |
     v
AI Reviewer (Gemini)
     |
     v
Verdict Extractor       (APPROVE | APPROVE_WITH_CHANGES | REJECT)
     |
  +--+------------------+
  |                     |
  v                     v
GitHub PR Comment    CI Merge Gate
(verdict + review)   (fail check on REJECT)
  |                     |
  +----------+----------+
             v
      Human Reviewer    (final decision)
```

### 4.2 Detailed Implementation Flow

This is how the high-level flow is implemented in practice, end to end from PR creation to the merge-protecting GitHub check.

```
Developer
  |
  v
Creates / Updates PR  (changes under terraform-review-agent/**)
  |
  | pull_request event triggers workflow
  v
GitHub Actions (.github/workflows/main.yml)
  - Checkout repository
  - Configure AWS credentials (repo secrets)
  - Install Terrascan v1.18.3
  - terrascan init
  |
  v
Terrascan Scan
  - Scans terraform-review-agent/terraform
  - AWS policy set, JSON output
  - Report uploaded as workflow artifact
  |
  | terrascan_report.json as payload
  v
AWS Lambda: terraform-ai-review-agent  (lambda/lambda_function.py)
  |
  +---------------------+---------------------+
  |                     |                     |
  v                     v                     v
Findings Extractor   Prompt Builder        Secrets Manager
- Severity summary   - Decision policy     - Gemini API key
- Structured         - Output format       - Cached per
  violations         - Findings JSON         Lambda container
  |                     |
  +----------+----------+
             v
     Gemini API (gemini-2.5-flash)
             |
             v
        AI Response
  - Issues by severity
  - Required remediation
  - Risk justification
  - Final verdict
             |
             v
     Verdict Extractor
  - Parses "Final verdict" from response
  - Defaults to REJECT if unclear (fail-safe)
             |
             v
     Lambda Response
  { verdict, summary, ai_review }
             |
             v
GitHub Actions
  - Prints AI review in workflow log
  - Posts AI review as PR comment
  - exit 1 if verdict == REJECT
             |
    +--------+---------+
    v                  v
GitHub PR Comment   GitHub Check
  - Verdict           - Passed / Failed
  - Full AI review
             |
             v
Merge protection (to configure) → Human Reviewer
```

## 5. Architecture Philosophy

The architecture intentionally keeps responsibilities separate.

| Component | Responsibility |
| --- | --- |
| GitHub Actions | Start the review automatically for PRs touching Terraform code. |
| Terrascan | Perform deterministic IaC security scanning against AWS policies. |
| Findings Extractor | Normalize raw Terrascan output into a structured findings set. |
| Prompt Builder | Combine findings with the strict risk-based decision policy. |
| AI Reviewer (Gemini) | Analyze findings, explain risk, and produce a policy-bound verdict. |
| Verdict Extractor | Parse the verdict deterministically; default to REJECT when unclear. |
| Secrets Manager | Store the Gemini API key outside code and CI logs. |
| PR Comment Publisher | Post the verdict and full AI review as a comment on the PR conversation. |
| CI Merge Gate | Fail the PR check on REJECT so merge protection can block the PR. |

The final decision remains with the **human reviewer**: the pipeline publishes a pass/fail check and a readable AI review, and branch protection (to configure) determines whether a failed check blocks merging.

## 6. GitHub Actions

GitHub Actions is the entry point of the pipeline. A developer creates or updates a PR, GitHub raises the event, and the `Terraform-AI-Review-Agent` workflow runs.

### 6.1 Trigger Events

- `pull_request` events on changes under `terraform-review-agent/**`

### 6.2 Workflow Steps (`.github/workflows/main.yml`)

1. Checkout repository
2. Configure AWS credentials (from repository secrets)
3. Install Terrascan v1.18.3
4. `terrascan init`
5. Run Terrascan against `terraform-review-agent/terraform` (AWS policies, JSON output)
6. Upload the Terrascan report as a workflow artifact
7. Invoke the `terraform-ai-review-agent` Lambda with the report as payload
8. Post the verdict and full AI review as a comment on the PR
9. Print the AI review and fail the job if the verdict is REJECT

### 6.3 Output

A pass/fail GitHub check on the PR, an AI review comment posted directly on the PR conversation, the full AI review text in the workflow log, and the raw Terrascan report as a downloadable artifact.

## 7. Terrascan (Deterministic Pass)

Terrascan performs Infrastructure-as-Code security and policy scanning before any AI is involved.

Typical areas covered:

- Security groups and public exposure
- IAM
- S3
- Encryption
- Logging
- Network security
- Load balancer configuration

The scan runs with `set +e` so that policy violations do not stop the pipeline at this stage — the findings are the input to the AI decision, not an immediate failure.

Output: `terrascan_report.json` containing a scan summary (counts by severity) and per-violation details (rule, severity, description, resource, file, line).

## 8. Findings Extractor

Inside the Lambda, the raw Terrascan report is reduced to the fields the AI actually needs.

### 8.1 Structure

```
Findings
|
+-- summary
|     +-- total_violations
|     +-- high / medium / low counts
|
+-- violations[]
      +-- rule_id, rule_name
      +-- severity, description
      +-- resource_type, resource_name
      +-- file, line
```

This keeps the prompt compact, removes scanner noise (scan errors are ignored), and gives the AI a consistent input shape regardless of how verbose the raw report is.

## 9. Prompt Builder and Decision Policy

The Prompt Builder assembles a structured prompt that positions the AI as a senior DevOps / Terraform security reviewer acting as a CI/CD security gate, and embeds a **strict decision policy** the verdict must follow.

### 9.1 Decision Policy

- **REJECT** if:
  - Any HIGH or CRITICAL severity issue exists
  - OR MEDIUM severity issues ≥ 4
  - OR the Application Load Balancer has no HTTPS listener at all
- **APPROVE_WITH_CHANGES** if:
  - MEDIUM severity issues are 1–3
- **APPROVE** if:
  - Only LOW or INFO issues exist

This encodes "security without blocking velocity": hard failures for real risk, conditional approval for fixable issues, and clean approval for noise-level findings.

### 9.2 Required Output Format

The AI must respond with:

1. Security issues ordered by severity (summary only)
2. Required remediation (actionable items only)
3. Risk justification (1–2 lines)
4. Final verdict: `APPROVE | APPROVE_WITH_CHANGES | REJECT`

## 10. AI Reviewer (Gemini)

| AI Tool | Model | Role | Status |
| --- | --- | --- | --- |
| Gemini | gemini-2.5-flash | Reviews findings and issues the policy-bound verdict | Active (only provider in POC) |

- The Lambda calls the Gemini REST API directly (no SDK dependency).
- The API key is fetched from **AWS Secrets Manager** at runtime and cached for the lifetime of the Lambda container — it never appears in code, CI configuration, or logs.
- The call runs with a 30-second timeout; API errors are captured and surface as a failed review rather than a crash.

## 11. Verdict Extractor (Fail-Safe)

The AI's free-text response is converted into a machine-readable decision:

- The extractor looks for the **"Final verdict"** marker in the response.
- It matches `REJECT`, then `APPROVE_WITH_CHANGES`, then `APPROVE` — in that order, so the most restrictive verdict wins on ambiguity.
- If no verdict can be identified, or any exception occurs anywhere in the Lambda, the result is **REJECT**.

```
Clear verdict  → use it
Unclear / error → REJECT (fail closed)
```

A silent approval is structurally impossible.

## 12. GitHub PR Comment

After the Lambda responds, the workflow posts the review directly on the Pull Request conversation using `actions/github-script`, so the developer sees the outcome without opening the workflow logs.

### 12.1 Comment Content

- Verdict badge: ✅ `APPROVE` / ⚠️ `APPROVE_WITH_CHANGES` / ❌ `REJECT`
- The full AI review (issues by severity, required remediation, risk justification, final verdict)
- A note that the final decision remains with the human reviewer

### 12.2 Example

```
🤖 AI Terraform Review

Verdict: ❌ REJECT

🚨 Security issues
- HIGH: Security group allows unrestricted ingress (0.0.0.0/0)

🛠 Required remediation
- Restrict inbound access to required ports and trusted source ranges

⚖️ Risk justification
Publicly exposed workload across all ports.

📌 Final verdict: REJECT

---
Automated review — final decision remains with the human reviewer.
```

### 12.3 Fail-Safe Behavior

- If the Lambda response cannot be parsed, the comment still posts with a ❌ `REJECT (fail-safe)` verdict and the parse error, so the PR always receives visible feedback.
- The comment is posted **before** the merge-gate step runs, so a REJECT verdict never prevents the review from reaching the PR.
- The workflow grants its `GITHUB_TOKEN` the `pull-requests: write` permission (scoped to this job) to allow comment creation.

## 13. CI Merge Gate

The final workflow step reads the verdict from the Lambda response:

```
🧠 AI Verdict: REJECT
❌ AI Agent rejected Terraform changes   → job fails (exit 1)

🧠 AI Verdict: APPROVE / APPROVE_WITH_CHANGES
✅ AI Agent approved                     → job passes
```

Branch protection can be configured to require this check before a PR is merged (to configure). Until then, the failed check is a strong visible signal to the human reviewer.

## 14. Supporting Serverless Infrastructure

The review agent itself runs serverless, and the repository includes a realistic AWS workload (under `terraform/`) that serves as the infrastructure being reviewed:

- **AWS Lambda** — the AI review agent
- **Amazon ECS (Fargate)** — demo application runtime
- **Application Load Balancer + ACM** — traffic routing and HTTPS enforcement (the decision policy explicitly checks for the HTTPS listener)
- **VPC / security groups / IAM** — network and permission surface that Terrascan evaluates
- **Secrets Manager** — Gemini API key
- **S3 backend** — Terraform state

This gives the POC real, non-trivial Terraform to review rather than toy examples.

## 15. Example End-to-End Scenario

### 15.1 PR Change

```hcl
resource "aws_security_group" "app" {
  ingress {
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
```

### 15.2 Step 1 — GitHub Actions Trigger

A `pull_request` event on `terraform-review-agent/**` triggers the workflow automatically.

### 15.3 Step 2 — Terrascan

```
HIGH
Security group allows unrestricted ingress (0.0.0.0/0)
```

The report is saved as `terrascan_report.json` and uploaded as an artifact.

### 15.4 Step 3 — Findings Extractor

```
summary:    { high: 1, medium: 0, low: 0 }
violations: [ aws_security_group / unrestricted ingress / security.tf:42 ]
```

### 15.5 Step 4 — Prompt Builder

Findings are embedded into the prompt together with the strict decision policy and required output format.

### 15.6 Step 5 — AI Reviewer (Gemini)

Gemini summarizes the issue, lists remediation (restrict ports and source ranges), justifies the risk, and — because a HIGH issue exists — the policy forces:

```
📌 Final verdict: REJECT
```

### 15.7 Step 6 — Verdict Extractor

`REJECT` is parsed from the response.

### 15.8 Step 7 — GitHub PR Comment

The workflow posts the verdict and the full AI review as a comment on the PR conversation:

```
🤖 AI Terraform Review
Verdict: ❌ REJECT
...
```

### 15.9 Step 8 — CI Merge Gate

The workflow prints the AI review and exits with code 1. The GitHub check on the PR is **Failed**.

### 15.10 Step 9 — Human Review

The reviewer reads the AI review in the check output, requests the fix, and the developer pushes a corrected commit — which re-triggers the pipeline automatically.

## 16. Reviewer Includes

The POC includes:

- GitHub Actions trigger on Terraform PR changes
- Path-scoped triggering (`terraform-review-agent/**`)
- Deterministic Terrascan security scan (AWS policy set)
- Scan report published as a workflow artifact
- Findings normalization (severity summary + structured violations)
- Prompt Builder embedding a strict risk-based decision policy
- HTTPS enforcement check for the ALB
- AI review via Gemini (serverless Lambda, direct REST call)
- API key isolation via AWS Secrets Manager with container-level caching
- Deterministic verdict extraction with fail-closed REJECT default
- Fail-closed error handling across the Lambda (errors can never approve)
- AI review posted as a GitHub PR comment (verdict badge + full review)
- CI merge gate failing the PR check on REJECT, with merge protection to configure
- Human-controlled final decision

## 17. Known Limitations and Next Steps

This POC intentionally keeps the pipeline small. The following evolutions map it toward the full target design:

| Area | POC today | Next step |
| --- | --- | --- |
| Deterministic checks | Terrascan only | Add `terraform fmt` / `validate`, TFLint, Checkov; reuse existing check results |
| Org standards | Encoded in the AI prompt (HTTPS rule) | Dedicated custom org rules (tags, naming, allowed configurations) |
| AI provider | Gemini only | LLM Factory with Claude primary and Gemini / OpenAI fallback |
| Sanitization | Not required (scanner findings only reach the AI, not raw diffs) | Sensitive-value detection and redaction before any diff is sent to an LLM |
| Findings handling | AI consolidates in one pass | Finding Engine: normalize, correlate, deduplicate, validate across sources |
| PR feedback | PR summary comment + workflow log + artifact | Inline comments on the exact changed lines |
| Scope of analysis | Full directory scan | Diff-aware Change Analyzer scoped to changed resources |
| Merge control | Check fails on REJECT; protection to configure | Branch protection requiring the check, with documented override path |
