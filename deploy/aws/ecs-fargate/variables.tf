variable "name" {
  type        = string
  description = "Base name for ECS resources."
  default     = "votrix-managed-agents"
}

variable "image" {
  type        = string
  description = "Container image URI, usually from ECR."
}

variable "region" {
  type        = string
  description = "AWS region."
}

variable "cluster_arn" {
  type        = string
  description = "Existing ECS cluster ARN."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for Fargate tasks."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Security groups for Fargate tasks."
}

variable "target_group_arn" {
  type        = string
  description = "Existing ALB target group ARN for the web service."
}

variable "database_url_secret_arn" {
  type        = string
  description = "Secrets Manager ARN containing DATABASE_URL."
}

variable "model_api_key_secret_arns" {
  type        = map(string)
  description = "Map of provider environment variable names to Secrets Manager ARNs, for example ANTHROPIC_API_KEY."
  default     = {}
}

variable "default_model_provider" {
  type        = string
  description = "Default model provider registered by the Deep Agents runtime."
  default     = "anthropic"
}

variable "vma_api_key_secret_arn" {
  type        = string
  description = "Secrets Manager ARN containing VMA_API_KEY."
}

variable "s3_secret_access_key_secret_arn" {
  type        = string
  description = "Secrets Manager ARN containing S3_SECRET_ACCESS_KEY."
}

variable "s3_access_key_id_secret_arn" {
  type        = string
  description = "Secrets Manager ARN containing S3_ACCESS_KEY_ID."
}

variable "s3_bucket_name" {
  type        = string
  description = "Object storage bucket name."
}

variable "s3_public_url" {
  type        = string
  description = "Public base URL for object storage."
}

variable "s3_endpoint_url" {
  type        = string
  description = "Optional S3-compatible endpoint URL. Leave empty for AWS S3."
  default     = ""
}

variable "s3_region" {
  type        = string
  description = "S3 region."
  default     = "us-east-1"
}

variable "web_desired_count" {
  type    = number
  default = 1
}

variable "worker_desired_count" {
  type    = number
  default = 1
}
