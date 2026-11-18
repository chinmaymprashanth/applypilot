# Settings. Your own values go in terraform.tfvars (git-ignored); see terraform.tfvars.example.

variable "email" {
  description = "Where the daily summary and budget alerts go (also the email's sender, verified by SES)"
  type        = string
}

variable "region" {
  description = "AWS region. us-east-1 (N. Virginia): cheapest, and a US address for job sites."
  type        = string
  default     = "us-east-1"
}

variable "timezone" {
  description = "Your time zone, for the schedule and for what 'today' means"
  type        = string
  default     = "Asia/Kolkata"
}

variable "run_hour" {
  description = "Hour (0-23, your time zone) the daily run starts"
  type        = number
  default     = 7
}

variable "run_minute" {
  type    = number
  default = 0
}

variable "backup_after_hours" {
  description = "The backup run starts this many hours later, only if the Spot run didn't finish"
  type        = number
  default     = 3
}

variable "cpu" {
  description = "1024 = 1 vCPU"
  type        = number
  default     = 1024
}

variable "memory" {
  description = "MB of memory"
  type        = number
  default     = 2048
}

variable "monthly_budget_usd" {
  type    = string
  default = "10"
}
