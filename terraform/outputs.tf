output "lambda_name" {
  value = aws_lambda_function.scanner.function_name
}

output "report_bucket" {
  value = aws_s3_bucket.reports.id
}

output "mode" {
  value = var.enable_delete ? "DELETE (after 7-day grace period)" : "DRY RUN"
}
