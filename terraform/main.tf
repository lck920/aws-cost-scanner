terraform {
  required_version = ">= 1.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "aws-cost-scanner" } }
}

data "aws_caller_identity" "me" {}

locals {
  name = "cost-scanner"
}

# ---------- S3: full JSON reports ----------

resource "aws_s3_bucket" "reports" {
  bucket        = "${local.name}-reports-${data.aws_caller_identity.me.account_id}" # bucket names are global
  force_destroy = true                                                              # lets `terraform destroy` delete it even with reports inside
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    id     = "expire-old-reports"
    status = "Enabled"
    filter {}
    expiration { days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

# ---------- SNS: email summary ----------

resource "aws_sns_topic" "reports" {
  name = local.name
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.reports.arn
  protocol  = "email"
  endpoint  = var.email
}

# ---------- IAM: least privilege ----------

resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "read" {
  name = "read-only-scan"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Describe/Get calls don't support resource-level permissions, so "*" is the only option here.
        Sid    = "Scan"
        Effect = "Allow"
        Action = [
          "ec2:DescribeRegions", "ec2:DescribeVolumes", "ec2:DescribeAddresses", "ec2:DescribeSnapshots",
          "ec2:DescribeInstances", "ec2:DescribeNatGateways",
          "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTargetGroups",
          "elasticloadbalancing:DescribeTargetHealth", "elasticloadbalancing:DescribeTags",
          "cloudwatch:GetMetricStatistics",
        ]
        Resource = "*"
      },
      { Sid = "WriteReport", Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.reports.arn}/reports/*" },
      { Sid = "SendEmail", Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.reports.arn },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
    ]
  })
}

# Only exists when enable_delete = true. In dry-run mode the role physically cannot delete anything.
resource "aws_iam_role_policy" "delete" {
  count = var.enable_delete ? 1 : 0
  name  = "delete-flagged"
  role  = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "FlagForDeletion"
        Effect   = "Allow"
        Action   = ["ec2:CreateTags", "elasticloadbalancing:AddTags"]
        Resource = "*"
      },
      {
        # Can only delete resources that already carry the scanner's flag tag (i.e. survived the grace period check).
        Sid    = "DeleteFlaggedOnly"
        Effect = "Allow"
        Action = [
          "ec2:DeleteVolume", "ec2:DeleteSnapshot", "ec2:ReleaseAddress", "ec2:DeleteNatGateway",
          "elasticloadbalancing:DeleteLoadBalancer",
        ]
        Resource  = "*"
        Condition = { Null = { "aws:ResourceTag/cost-scanner:first-flagged" = "false" } }
      },
    ]
  })
}

# ---------- Lambda ----------

# Terraform owns the log group so `destroy` removes it (Lambda-created ones are left behind).
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 14
}

data "archive_file" "code" {
  type        = "zip"
  source_file = "${path.module}/../src/scanner.py"
  output_path = "${path.module}/build/scanner.zip"
}

resource "aws_lambda_function" "scanner" {
  function_name    = local.name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "scanner.handler" # boto3 ships with the runtime, so no dependencies to package
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  timeout          = 300
  memory_size      = 256

  environment {
    variables = {
      DRY_RUN       = var.enable_delete ? "false" : "true"
      REPORT_BUCKET = aws_s3_bucket.reports.id
      TOPIC_ARN     = aws_sns_topic.reports.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# ---------- EventBridge Scheduler: run daily ----------

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "scheduler.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-scanner"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.scanner.arn }]
  })
}

resource "aws_scheduler_schedule" "daily" {
  name                         = local.name
  schedule_expression          = var.schedule
  schedule_expression_timezone = "Asia/Singapore"
  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.scanner.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

# ---------- Alarm: tell me if the scanner itself breaks ----------

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${local.name}-errors"
  alarm_description   = "The cost scanner Lambda failed. No report was sent."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.scanner.function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.reports.arn]
}
