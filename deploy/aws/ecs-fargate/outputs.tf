output "web_service_name" {
  value = aws_ecs_service.web.name
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "web_task_definition_arn" {
  value = aws_ecs_task_definition.web.arn
}

output "worker_task_definition_arn" {
  value = aws_ecs_task_definition.worker.arn
}
