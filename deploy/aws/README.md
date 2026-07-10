# AWS ECS/Fargate

AWS compatibility is provided as a Terraform module for ECS/Fargate. It assumes
the surrounding AWS baseline already exists:

- VPC and private subnets
- ALB and target group
- ECS cluster
- RDS or another Postgres reachable through `DATABASE_URL`
- Secrets Manager entries
- ECR image built from the root `Dockerfile`

Build and push the image to ECR, run migrations once with the same image:

```bash
docker build -t votrix-managed-agents .
docker tag votrix-managed-agents AWS_ACCOUNT.dkr.ecr.REGION.amazonaws.com/votrix-managed-agents:TAG
docker push AWS_ACCOUNT.dkr.ecr.REGION.amazonaws.com/votrix-managed-agents:TAG
aws ecs run-task \
  --cluster CLUSTER \
  --launch-type FARGATE \
  --task-definition MIGRATION_TASK_DEFINITION \
  --network-configuration file://network.json
```

Then apply `deploy/aws/ecs-fargate` with `image` set to the ECR image URI. The
web and worker task definitions use the same image with different commands:

- Web: `scripts/start-web.sh`
- Worker: `scripts/start-worker.sh`

This keeps AWS support out of application code while still matching the standard
ECS/Fargate + ECR + RDS + Secrets Manager + ALB deployment shape.
