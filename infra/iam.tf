# IAM: who may do what. Each role is a "badge" a service wears; its policy lists exactly what that badge allows.
# Nothing here grants more than the daily run needs (least privilege).

data "aws_iam_policy_document" "ecs_assume" { # "ECS tasks may wear this badge"
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---- Execution role: used by ECS itself to START the container --------------------------------------------------
# Pull the image from ECR, write logs, and read the two secrets to hand them to the container.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_basic" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["ssm:GetParameters"]
    resources = ["${local.ssm_path}/*"]
  }
  statement { # decrypt SecureStrings, only when the request comes through Parameter Store
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-applypilot-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# ---- Task role: used by ApplyPilot's code while it RUNS ---------------------------------------------------------
# Only its own bucket, and only sending email from your verified address.
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  statement {
    actions   = ["ses:SendEmail"]
    resources = [aws_sesv2_email_identity.me.arn]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "applypilot-run"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ---- Scheduler role: lets EventBridge Scheduler start the task ---------------------------------------------------
resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "scheduler.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.me.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "run-applypilot"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecs:RunTask", Resource = [
        "${aws_ecs_task_definition.daily.arn_without_revision}:*",
        "${aws_ecs_task_definition.backup.arn_without_revision}:*",
      ] },
      # "Pass" the two badges above to the task it starts (and no other role).
      { Effect = "Allow", Action = "iam:PassRole", Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn] },
    ]
  })
}

# ---- Your Mac: a key that can only reach ApplyPilot's bucket and secrets ---------------------------------------
# Used by `apply`, `cloud-push/pull` and `cloud-secrets` (AWS_PROFILE=applypilot in .env), so they never need
# `aws login`. The user was created by hand in CloudShell on 25 Nov 2026; the import blocks adopt it here.
# Its access key is deliberately NOT managed by Terraform (the secret would be stored in the state file):
# create or rotate it with `aws iam create-access-key --user-name applypilot-mac`.
resource "aws_iam_user" "mac" {
  name = "${local.name}-mac"
}

import {
  to = aws_iam_user.mac
  id = "applypilot-mac"
}

resource "aws_iam_user_policy" "mac" {
  name = "applypilot-only"
  user = aws_iam_user.mac.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      Resource = "${aws_s3_bucket.state.arn}/*" },
      { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.state.arn },
      { Effect = "Allow", Action = ["ssm:PutParameter", "ssm:GetParameter"], Resource = "${local.ssm_path}/*" },
      { Effect = "Allow", Action = ["kms:Encrypt", "kms:Decrypt"], Resource = "*",
      Condition = { StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" } } },
    ]
  })
}

import {
  to = aws_iam_user_policy.mac
  id = "applypilot-mac:applypilot-only"
}
