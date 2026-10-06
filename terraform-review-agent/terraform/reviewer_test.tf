# TEST FILE for the Terraform AI Reviewer: every block below intentionally breaks
# an org policy (rules/org_policies.md). Delete this file after the test PR.

# ORG-002 (public S3), ORG-003 (no encryption), ORG-001 (missing tags), ORG-007 (naming)
resource "aws_s3_bucket" "test_public" {
  bucket = "MyTestBucket_Public"
}

resource "aws_s3_bucket_acl" "test_public" {
  bucket = aws_s3_bucket.test_public.id
  acl    = "public-read"
}

# ORG-004: SSH and RDP open to the internet
resource "aws_security_group" "test_open" {
  name        = "test-open-sg"
  description = "Reviewer test security group"

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 3389
    to_port     = 3389
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ORG-005: wildcard action and resource
resource "aws_iam_policy" "test_admin" {
  name = "test-admin-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "*"
      Resource = "*"
    }]
  })
}

# ORG-006 (hardcoded password), ORG-009 (no deletion protection), ORG-003 (unencrypted)
resource "aws_db_instance" "test_db" {
  identifier          = "test-db"
  engine              = "mysql"
  instance_class      = "db.t3.micro"
  allocated_storage   = 20
  username            = "admin"
  password            = "SuperSecret123!"
  publicly_accessible = true
  storage_encrypted   = false
  deletion_protection = false
  skip_final_snapshot = true
}
