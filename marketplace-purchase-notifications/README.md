# Automate AWS Marketplace Purchase Notifications for Procurement Teams

Enriched notification pipeline that captures AWS Marketplace purchase events across an AWS Organization, enriches them with product name, pricing, PO number, and purchaser identity, and delivers procurement-ready notifications via Amazon SNS.

## Architecture

```
Linked Accounts                          Hub Account
┌──────────────────┐                     ┌─────────────────────────────────┐
│ EventBridge Rule │──── PutEvents ────►│ EventBridge Rule                │
│ (forward events) │                     │         │                       │
└──────────────────┘                     │         ▼                       │
                                         │ Lambda (Enrichment)             │
                                         │   • STS AssumeRole             │
                                         │   • DescribeAgreement ($)      │
                                         │   • GetProduct (name)          │
                                         │   • GetAgreementTerms          │
                                         │   • CloudTrail (who)           │
                                         │         │                       │
                                         │         ▼                       │
                                         │ SNS → Email / Slack / ITSM     │
                                         └─────────────────────────────────┘
```

## Deployment

### Hub Account

1. Create SNS topic and subscribe your procurement team
2. Deploy the Lambda function (`lambda/lambda_function.py`)
3. Create the EventBridge rule targeting the Lambda
4. Allow linked accounts to forward events to the hub event bus

### Linked Accounts (via CloudFormation StackSet)

Deploy `cloudformation/linked-account-resources.yaml` to each account that makes Marketplace purchases.

**Parameter:** `HubAccountId` — the 12-digit account ID where the Lambda runs.

```bash
aws cloudformation create-stack-set \
  --stack-set-name MarketplacePurchaseNotification \
  --template-body file://cloudformation/linked-account-resources.yaml \
  --parameters ParameterKey=HubAccountId,ParameterValue=123456789012 \
  --capabilities CAPABILITY_NAMED_IAM

aws cloudformation create-stack-instances \
  --stack-set-name MarketplacePurchaseNotification \
  --accounts '["111111111111","222222222222","333333333333"]' \
  --regions '["us-east-1"]'
```

## Identifying Target Accounts

Not every account needs this. Find which accounts have Marketplace spend:

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-01-01,End=2026-07-01 \
  --granularity MONTHLY \
  --filter '{"Dimensions":{"Key":"BILLING_ENTITY","Values":["AWS Marketplace"]}}' \
  --group-by '[{"Type":"DIMENSION","Key":"LINKED_ACCOUNT"}]' \
  --metrics UnblendedCost
```

## Environment Variables (Lambda)

| Variable | Description |
|----------|-------------|
| `SNS_TOPIC_ARN` | ARN of the SNS topic for notifications |
| `CROSS_ACCOUNT_ROLE_NAME` | Name of the role to assume in linked accounts (default: `MarketplacePurchaseEnricher-CrossAccount`) |

## IAM Permissions Summary

| Role | Account | Trusts | Permissions |
|------|---------|--------|-------------|
| `MarketplacePurchaseEnricher-LambdaRole` | Hub | Lambda service | Marketplace read, SNS publish, STS AssumeRole, CloudWatch Logs |
| `MarketplacePurchaseEnricher-CrossAccount` | Each linked | Hub Lambda role | Marketplace read, Discovery API, CloudTrail read |
| `MarketplaceEventForwarder-Role` | Each linked | EventBridge service | PutEvents to hub event bus |

## Simplified Alternative

If you don't need enriched details, use AWS User Notifications (zero code) or EventBridge → SNS direct (3 CLI commands). See the blog post for details.

## License

MIT-0
