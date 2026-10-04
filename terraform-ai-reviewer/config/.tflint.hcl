# Default TFLint configuration used when the reviewed repository has no .tflint.hcl
# (override with TFLINT_CONFIG or by committing a .tflint.hcl at the repository root).

config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

# AWS-specific rules (invalid instance types, deprecated arguments, ...).
plugin "aws" {
  enabled = true
  version = "0.36.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
