# AWS Blogs — Sample Code & Solutions

This repository contains companion code and templates for AWS blog posts.

## Posts

### [Automate AWS Marketplace Purchase Notifications for Procurement Teams](marketplace-purchase-notifications/)

Build an enriched notification pipeline that alerts procurement teams the moment a Marketplace purchase occurs — with product name, dollar amount, pricing terms, purchaser identity, and PO number.

**Services used:** Amazon EventBridge, AWS Lambda, Amazon SNS, AWS Marketplace Agreement API, Marketplace Discovery API, AWS CloudTrail

**Key files:**
- [`lambda/lambda_function.py`](marketplace-purchase-notifications/lambda/lambda_function.py) — Enrichment Lambda (Python 3.12)
- [`cloudformation/linked-account-resources.yaml`](marketplace-purchase-notifications/cloudformation/linked-account-resources.yaml) — StackSet template for linked accounts
- [`index.html`](marketplace-purchase-notifications/index.html) — Blog post (formatted)

---

## License

This sample code is made available under the MIT-0 license. See the LICENSE file.
