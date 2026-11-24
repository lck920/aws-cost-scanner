variable "region" {
  description = "Region where the scanner itself is deployed (it still scans every enabled region)."
  type        = string
  default     = "ap-southeast-1"
}

variable "email" {
  description = "Where the daily report is sent. You must click the confirmation link AWS emails you."
  type        = string
}

variable "enable_delete" {
  description = "false = report only. true = delete flagged resources after the grace period, and attach delete IAM permissions."
  type        = bool
  default     = false
}

variable "schedule" {
  description = "When to run, in Asia/Singapore time."
  type        = string
  default     = "cron(0 8 * * ? *)" # 08:00 daily
}
