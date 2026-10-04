# Organizational Terraform Policies

> **SAMPLE POLICIES — replace with your organization's rules.**
> This file is plain text for the LLM policy pass. Keep the format below so findings can be
> verified: each rule is a heading `## <ID>: <title>`, optionally followed by `Severity: <LEVEL>`
> (CRITICAL, HIGH, MEDIUM, LOW, INFO) and any explanation, examples or exceptions.
> Findings that cite an ID not present in this file are discarded automatically.

## ORG-001: Mandatory resource tags
Severity: LOW

Every taggable AWS resource must set the tags `Environment`, `Owner`, `CostCenter` and `Application`,
either directly or through the provider's `default_tags` block.

## ORG-002: No public S3 buckets
Severity: CRITICAL

S3 buckets must not be readable or writable by the public. Every `aws_s3_bucket` must have an
`aws_s3_bucket_public_access_block` with all four settings set to `true`. ACLs `public-read` and
`public-read-write` are forbidden.

## ORG-003: Encryption at rest
Severity: HIGH

Storage resources (S3, EBS, RDS, DynamoDB, SQS, SNS, CloudWatch log groups, Secrets Manager) must be
encrypted at rest. Use customer-managed KMS keys for anything holding customer data.

## ORG-004: No open ingress from the internet
Severity: HIGH

Security group rules must not allow ingress from `0.0.0.0/0` or `::/0` on any port other than 80 and
443 on a public load balancer. SSH (22) and RDP (3389) must never be open to the internet.

Exception: a rule carrying the comment `# policy-exception: ORG-004 <ticket>` on the same line.

## ORG-005: Least-privilege IAM
Severity: HIGH

IAM policies must not grant `"Action": "*"` or `"Resource": "*"` together with write actions.
Wildcard service actions such as `s3:*` need a justification comment.

## ORG-006: No hardcoded secrets
Severity: CRITICAL

Passwords, tokens, access keys and private keys must never appear as literals in Terraform code or
`.tfvars` files. Use Secrets Manager / SSM Parameter Store data sources or variables marked
`sensitive = true`.

## ORG-007: Naming convention
Severity: LOW

Resource names (the `name` / `bucket` / `identifier` argument) must follow
`<environment>-<application>-<component>` in lowercase kebab-case, for example `prod-payments-api`.

## ORG-008: Pinned versions
Severity: MEDIUM

Providers must declare a version constraint in `required_providers`. Modules from a registry or git
must pin a version (`version = "..."` or `?ref=<tag>`), never a branch.

## ORG-009: Deletion protection for stateful data
Severity: MEDIUM

Production databases (`aws_db_instance`, `aws_rds_cluster`) must set `deletion_protection = true`
and `skip_final_snapshot = false`.
