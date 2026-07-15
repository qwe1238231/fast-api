# infra/ — AWS deployment (Terraform)

Infrastructure-as-code for deploying the ticket system to AWS (Seoul,
`ap-northeast-2`). Built phase by phase; **low-cost / ephemeral** by design.

## Golden rule: apply → learn → destroy

Always-on this stack costs ~$60–145/month. You don't need it always on — bring
it up to practice, then tear it down:

```bash
cd infra
terraform apply     # bring resources up
# ... do your thing ...
terraform destroy   # tear them ALL down — pay only for the hours used
```

The biggest cost risk is **forgetting to destroy** on a shared account. When in
doubt, `terraform destroy`.

## Cost notes (dev sizing)

- Skipping the **NAT Gateway** (~$33/mo) on purpose — Fargate tasks will sit in
  public subnets for dev.
- ALB (~$20/mo) only added when we practice HTTPS.
- RDS / ElastiCache use the smallest `t4g.micro` sizes.

## Phases

- **B (current): ECR** — `ecr.tf`. Blocked until the IAM user has
  `AmazonEC2ContainerRegistryFullAccess` (or `PowerUserAccess`).
- C: VPC · D: RDS + ElastiCache · E: ECS + ALB · F: Secrets + CloudWatch + CI/CD

## Usage

```bash
terraform init       # one-time: download the AWS provider
terraform validate   # check the config is well-formed (no AWS calls)
terraform plan        # preview what will change (read-only AWS calls)
terraform apply       # create/update resources
terraform output      # show outputs (e.g. the ECR repo URL)
```

State is local (`terraform.tfstate`) — fine for solo work; don't commit it
(it can contain secrets). See `.gitignore`.
