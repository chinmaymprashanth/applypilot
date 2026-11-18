# ECS Fargate: the rented computer that runs the container, and the schedule that starts it.

# ---- Networking: the default network every AWS account has -------------------------------------------------------
# The task gets a temporary public IP only to reach the internet (job sites, Anthropic, S3). Cheaper than a
# NAT gateway (~$32/month). The security group allows NO incoming connections: nothing can reach the container.
data "aws_vpc" "default" { default = true }

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "ApplyPilot daily run: outbound only"
  vpc_id      = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---- Cluster: a named group for tasks, allowed to use regular and Spot capacity -----------------------------------
resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

# ---- Task definitions: "run this image, with this much CPU/memory, these settings and secrets" ---------------------
locals {
  container = {
    name      = local.name
    image     = "${aws_ecr_repository.app.repository_url}:latest"
    essential = true
    # Seconds between "please stop" (SIGTERM) and a hard kill. 120 is Fargate's maximum and matches Spot's
    # two-minute warning, so the run has time to upload its data and release the lock.
    stopTimeout = 120
    environment = [
      { name = "APPLYPILOT_BUCKET", value = aws_s3_bucket.state.id },
      { name = "SUMMARY_EMAIL", value = var.email },
      { name = "TZ", value = var.timezone },
      { name = "AWS_REGION", value = var.region },
    ]
    secrets = [ # read from Parameter Store at start; never stored in the image or in Terraform
      { name = "ANTHROPIC_API_KEY", valueFrom = "${local.ssm_path}/anthropic_api_key" },
      { name = "GMAIL_TOKEN", valueFrom = "${local.ssm_path}/gmail_token" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "run"
      }
    }
  }
}

resource "aws_ecs_task_definition" "daily" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([local.container])
}

# The same, started with --backup: it exits at once if today's run already finished.
resource "aws_ecs_task_definition" "backup" {
  family                   = "${local.name}-backup"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([merge(local.container, { command = ["--backup"] })])
}

# ---- EventBridge Scheduler: the alarm clocks -------------------------------------------------------------------------
resource "aws_scheduler_schedule" "daily" {
  name                         = "${local.name}-daily"
  schedule_expression          = "cron(${var.run_minute} ${var.run_hour} * * ? *)"
  schedule_expression_timezone = var.timezone
  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy { maximum_retry_attempts = 3 } # e.g. no Spot capacity free at that moment
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.daily.arn_without_revision # always the newest revision
      capacity_provider_strategy {                                             # ~70% cheaper, may be interrupted
        capacity_provider = "FARGATE_SPOT"
        weight            = 1
      }
      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = true
      }
    }
  }
}

resource "aws_scheduler_schedule" "backup" {
  name                         = "${local.name}-backup"
  schedule_expression          = "cron(${var.run_minute} ${var.run_hour + var.backup_after_hours} * * ? *)"
  schedule_expression_timezone = var.timezone
  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy { maximum_retry_attempts = 3 }
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.backup.arn_without_revision
      capacity_provider_strategy { # regular Fargate: can't be interrupted
        capacity_provider = "FARGATE"
        weight            = 1
      }
      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = true
      }
    }
  }
}
