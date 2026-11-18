# GitHub Actions deploys new images without any stored AWS key (OIDC):
# GitHub signs a short-lived token saying "this is repo X, branch main"; AWS checks it and lends a role for ~1 hour.

variable "github_repo" {
  description = "owner/name of the GitHub repository allowed to deploy"
  type        = string
  default     = "chinmaymprashanth/applypilot"
}

variable "github_repo_immutable" {
  description = "The same repository with GitHub's permanent ids (owner@id/name@id), used in newer OIDC tokens"
  type        = string
  default     = "chinmaymprashanth@221058622/applypilot@1460085782"
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "deploy" {
  name = "${local.name}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Only pushes to main of this one repository; forks and pull requests can't deploy.
          # GitHub's newer tokens name the repo with permanent ids, so a renamed or re-created repo can't match.
          "token.actions.githubusercontent.com:sub" = [
            "repo:${var.github_repo_immutable}:ref:refs/heads/main",
            "repo:${var.github_repo}:ref:refs/heads/main",
          ]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "deploy" { # push images to the one ECR repository, nothing else
  name = "push-applypilot-image"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      { Effect = "Allow", Resource = aws_ecr_repository.app.arn, Action = [
        "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
      ] },
    ]
  })
}

output "github_deploy_role" {
  description = "Saved in GitHub as the repository variable AWS_DEPLOY_ROLE"
  value       = aws_iam_role.deploy.arn
}
