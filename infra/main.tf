# ApplyPilot on AWS. `terraform apply` creates everything below; `terraform destroy` removes it all.
#
#   EventBridge Scheduler (7:00 daily, Spot) ──> ECS Fargate task (Docker image from ECR)
#        └─ 9:30 backup (regular Fargate) ─┘        ├─ secrets from Parameter Store
#                                                   ├─ data/ archive + summaries in S3
#                                                   ├─ logs in CloudWatch
#                                                   └─ summary email through SES

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "applypilot" } } # every resource is labelled, so its cost is easy to find
}

data "aws_caller_identity" "me" {}

locals {
  name     = "applypilot"
  bucket   = "applypilot-${data.aws_caller_identity.me.account_id}" # bucket names are global: add the account id
  ssm_path = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter/applypilot"
}

# ---- 1. Budget alarm: an email if the month's AWS bill heads past the limit ----------------------------------
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.email]
  }
}

# ---- 2. S3: the data archive, daily summaries, run markers ---------------------------------------------------
resource "aws_s3_bucket" "state" {
  bucket        = local.bucket
  force_destroy = true # `terraform destroy` may delete it even with files inside
}

resource "aws_s3_bucket_public_access_block" "state" { # nothing in it can ever be made public
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" { # every upload keeps the previous copy: free backups
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" { # encrypted on disk
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" { # old copies are deleted after 30 days
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

# ---- 3. ECR: where the Docker image is stored -----------------------------------------------------------------
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE" # "latest" moves to each new build
  force_delete         = true
  image_scanning_configuration { scan_on_push = true } # AWS checks each image for known vulnerabilities
}

resource "aws_ecr_lifecycle_policy" "app" { # keep only the 5 newest images (storage is billed per GB)
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the 5 newest images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# ---- 4. CloudWatch Logs: everything the run prints ---------------------------------------------------------------
resource "aws_cloudwatch_log_group" "app" {
  name              = "/${local.name}"
  retention_in_days = 14
}

# ---- 5. SES: the daily email (AWS emails you a link to confirm the address) --------------------------------------
resource "aws_sesv2_email_identity" "me" {
  email_identity = var.email
}
