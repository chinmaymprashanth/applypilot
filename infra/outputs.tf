# Values Terraform prints after `apply` (also: `terraform output`).

output "bucket" {
  description = "Put this in .env as APPLYPILOT_BUCKET"
  value       = aws_s3_bucket.state.id
}

output "ecr_repository" {
  description = "Where Docker images are pushed"
  value       = aws_ecr_repository.app.repository_url
}

output "run_now" {
  description = "Start a run right now (on Spot), e.g. to test"
  value = join(" ", [
    "aws ecs run-task --cluster ${aws_ecs_cluster.main.name} --task-definition ${aws_ecs_task_definition.daily.family}",
    "--capacity-provider-strategy capacityProvider=FARGATE_SPOT,weight=1",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", data.aws_subnets.default.ids)}],securityGroups=[${aws_security_group.task.id}],assignPublicIp=ENABLED}'",
  ])
}

output "logs" {
  description = "Follow the run's output live"
  value       = "aws logs tail ${aws_cloudwatch_log_group.app.name} --follow"
}
