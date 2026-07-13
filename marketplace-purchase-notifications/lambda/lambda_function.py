"""
AWS Marketplace Purchase Agreement Enrichment Lambda (v5 - Cross-Account + PO + Product Name)

Triggered by EventBridge when a Marketplace agreement is created, amended, or ending.
Assumes a role in the source account to read agreement details, then enriches with:
  - Dollar amount (estimatedCharges from DescribeAgreement)
  - Product name (from Marketplace Discovery API - get_product)
  - Pricing terms (from GetAgreementTerms)
  - Purchase order reference (from ListAgreementInvoiceLineItems)
  - Purchaser identity (from CloudTrail LookupEvents)

Then publishes an enriched notification to SNS.
"""

import json
import os
import logging
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Clients (hub account - for SNS only)
sns_client = boto3.client('sns', region_name='us-east-1')
sts_client = boto3.client('sts', region_name='us-east-1')

SNS_TOPIC_ARN = os.environ['SNS_TOPIC_ARN']
CROSS_ACCOUNT_ROLE_NAME = os.environ.get('CROSS_ACCOUNT_ROLE_NAME', 'MarketplacePurchaseEnricher-CrossAccount')


def lambda_handler(event, context):
    """Main handler — receives EventBridge event, enriches, publishes to SNS."""
    logger.info(f"Received event: {json.dumps(event)}")

    detail = event.get('detail', {})
    agreement_id = detail.get('agreement', {}).get('id')
    detail_type = event.get('detail-type', 'Unknown')
    source_account = event.get('account', '')
    buyer_account = detail.get('acceptor', {}).get('accountId', 'Unknown')
    seller_account = detail.get('proposer', {}).get('accountId', 'Unknown')
    intent = detail.get('agreement', {}).get('intent', 'Unknown')

    if not agreement_id:
        logger.error("No agreement ID found in event")
        return {'statusCode': 400, 'body': 'No agreement ID'}

    # Determine which account to query
    target_account = source_account or buyer_account
    logger.info(f"Agreement {agreement_id} is in account {target_account}")

    # Get cross-account clients
    cross_account_clients = get_cross_account_clients(target_account)

    # Step 1: Get agreement details (dollar amount, dates, resource IDs)
    agreement_details = get_agreement_details(agreement_id, cross_account_clients)

    # Step 2: Get pricing terms
    pricing_summary = get_pricing_terms(agreement_id, cross_account_clients)

    # Step 3: Get product name via Marketplace Discovery API
    product_name = get_product_name(agreement_details, cross_account_clients)

    # Step 4: Get purchase order reference
    po_reference = get_purchase_order(agreement_id, cross_account_clients)

    # Step 5: Get purchaser identity from CloudTrail
    purchaser_identity = get_purchaser_identity(agreement_id, target_account, cross_account_clients)

    # Build enriched notification
    notification = build_notification(
        detail_type=detail_type,
        agreement_id=agreement_id,
        intent=intent,
        buyer_account=buyer_account,
        seller_account=seller_account,
        agreement_details=agreement_details,
        pricing_summary=pricing_summary,
        product_name=product_name,
        po_reference=po_reference,
        purchaser_identity=purchaser_identity,
        event_time=event.get('time', 'Unknown')
    )

    # Publish to SNS
    publish_to_sns(notification, detail_type, product_name, {
        **agreement_details,
        'buyer_account': buyer_account,
        'pricing_type': ', '.join(pricing_summary),
    })

    return {'statusCode': 200, 'body': 'Notification sent'}


def get_cross_account_clients(account_id):
    """Assume role in the source account and return boto3 clients."""
    hub_account = os.environ.get('AWS_ACCOUNT_ID', '')

    if account_id == hub_account or not account_id:
        logger.info("Event is from hub account — using local credentials")
        return {
            'agreement': boto3.client('marketplace-agreement', region_name='us-east-1'),
            'discovery': boto3.client('marketplace-discovery', region_name='us-east-1'),
            'cloudtrail': boto3.client('cloudtrail', region_name='us-east-1'),
        }

    role_arn = f"arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_ROLE_NAME}"
    logger.info(f"Assuming role {role_arn}")

    try:
        assumed = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName='MarketplaceEnricher',
            DurationSeconds=900
        )
        credentials = assumed['Credentials']

        session = boto3.Session(
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )

        return {
            'agreement': session.client('marketplace-agreement', region_name='us-east-1'),
            'discovery': session.client('marketplace-discovery', region_name='us-east-1'),
            'cloudtrail': session.client('cloudtrail', region_name='us-east-1'),
        }
    except Exception as e:
        logger.error(f"Failed to assume role in {account_id}: {e}")
        return {
            'agreement': boto3.client('marketplace-agreement', region_name='us-east-1'),
            'discovery': boto3.client('marketplace-discovery', region_name='us-east-1'),
            'cloudtrail': boto3.client('cloudtrail', region_name='us-east-1'),
        }


def get_agreement_details(agreement_id, clients):
    """Call DescribeAgreement to get estimated charges, dates, and resource info."""
    try:
        response = clients['agreement'].describe_agreement(agreementId=agreement_id)
        return {
            'estimated_value': response.get('estimatedCharges', {}).get('agreementValue', 'N/A'),
            'currency': response.get('estimatedCharges', {}).get('currencyCode', 'USD'),
            'start_time': str(response.get('startTime', 'N/A')),
            'end_time': str(response.get('endTime', 'N/A')),
            'acceptance_time': str(response.get('acceptanceTime', 'N/A')),
            'status': response.get('status', 'Unknown'),
            'agreement_type': response.get('agreementType', 'Unknown'),
            'resources': response.get('proposalSummary', {}).get('resources', []),
            'offer_id': response.get('proposalSummary', {}).get('offerId', 'N/A'),
        }
    except Exception as e:
        logger.warning(f"Failed to describe agreement {agreement_id}: {e}")
        return {
            'estimated_value': 'Error retrieving', 'currency': 'N/A',
            'start_time': 'N/A', 'end_time': 'N/A', 'acceptance_time': 'N/A',
            'status': 'Unknown', 'agreement_type': 'Unknown',
            'resources': [], 'offer_id': 'N/A',
        }


def get_pricing_terms(agreement_id, clients):
    """Call GetAgreementTerms to get detailed pricing breakdown."""
    try:
        response = clients['agreement'].get_agreement_terms(agreementId=agreement_id)
        terms = response.get('acceptedTerms', [])
        summary = []
        for term in terms:
            if 'fixedUpfrontPricingTerm' in term:
                t = term['fixedUpfrontPricingTerm']
                summary.append(f"Fixed Upfront: {t.get('currencyCode', 'USD')} {t.get('price', 'N/A')} ({t.get('duration', 'N/A')})")
            elif 'recurringPaymentTerm' in term:
                t = term['recurringPaymentTerm']
                price_info = t.get('price', {})
                summary.append(f"Recurring: {price_info.get('currencyCode', 'USD')} {price_info.get('amount', 'N/A')}/{t.get('billingPeriod', 'N/A')}")
            elif 'usageBasedPricingTerm' in term:
                summary.append("Usage-Based Pricing (pay-as-you-go)")
            elif 'configurableUpfrontPricingTerm' in term:
                summary.append(f"Configurable Upfront")
            elif 'paymentScheduleTerm' in term:
                t = term['paymentScheduleTerm']
                schedule = t.get('schedule', [])
                summary.append(f"Payment Schedule: {len(schedule)} installment(s)")
            elif 'freeTrialPricingTerm' in term:
                summary.append("Free Trial")
        return summary if summary else ['No pricing terms found']
    except Exception as e:
        logger.warning(f"Failed to get agreement terms for {agreement_id}: {e}")
        return [f'Error retrieving terms: {str(e)}']


def get_product_name(agreement_details, clients):
    """Use the Marketplace Discovery API (buyer-side) to get product title and vendor."""
    resources = agreement_details.get('resources', [])
    if not resources:
        return 'Unknown Product'

    resource_id = resources[0].get('id')
    resource_type = resources[0].get('type', 'Unknown')

    if not resource_id:
        return 'Unknown Product'

    try:
        response = clients['discovery'].get_product(productId=resource_id)
        product_name = response.get('productName', resource_id)
        manufacturer = response.get('manufacturer', {}).get('displayName', '')

        if manufacturer:
            return f"{product_name} by {manufacturer} ({resource_type})"
        else:
            return f"{product_name} ({resource_type})"
    except Exception as e:
        logger.warning(f"Failed to get product name for {resource_id}: {e}")
        return f'{resource_id} ({resource_type})'


def get_purchase_order(agreement_id, clients):
    """Try to get PO reference from invoice line items (ListAgreementCharges not yet in boto3)."""
    try:
        # Try list_agreement_invoice_line_items which may contain PO info
        response = clients['agreement'].list_agreement_invoice_line_items(
            agreementId=agreement_id,
            groupBy='INVOICE_ID'
        )
        items = response.get('invoiceLineItemGroupSummaries', [])
        for item in items:
            po = item.get('purchaseOrderReference') or item.get('purchaseOrder', {}).get('number')
            if po:
                return po
        return 'N/A'
    except Exception as e:
        logger.warning(f"Failed to get purchase order for {agreement_id}: {e}")
        # Fallback: try raw API call for ListAgreementCharges
        try:
            raw_client = clients['agreement']._service_model
            # If the API isn't in the model, we can't call it
            return 'N/A (API not available in runtime)'
        except:
            return 'N/A'


def get_purchaser_identity(agreement_id, account_id, clients):
    """Look up CloudTrail to find who initiated the Marketplace purchase."""
    try:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=1)

        response = clients['cloudtrail'].lookup_events(
            LookupAttributes=[{
                'AttributeKey': 'EventSource',
                'AttributeValue': 'aws-marketplace.amazonaws.com'
            }],
            StartTime=start_time,
            EndTime=end_time,
            MaxResults=20
        )

        for event in response.get('Events', []):
            cloud_trail_event = json.loads(event.get('CloudTrailEvent', '{}'))
            event_name = cloud_trail_event.get('eventName', '')
            if 'Agreement' in event_name or 'Subscribe' in event_name or 'Accept' in event_name:
                user_identity = cloud_trail_event.get('userIdentity', {})
                return {
                    'arn': user_identity.get('arn', 'N/A'),
                    'username': user_identity.get('userName', user_identity.get('principalId', 'N/A')),
                    'type': user_identity.get('type', 'Unknown'),
                    'source_ip': cloud_trail_event.get('sourceIPAddress', 'N/A')
                }

        return {'arn': 'Not found (CloudTrail may take up to 15 min)', 'username': 'N/A', 'type': 'N/A', 'source_ip': 'N/A'}
    except Exception as e:
        logger.warning(f"Failed to look up CloudTrail: {e}")
        return {'arn': f'Error: {str(e)}', 'username': 'N/A', 'type': 'N/A', 'source_ip': 'N/A'}


def build_notification(detail_type, agreement_id, intent, buyer_account,
                       seller_account, agreement_details, pricing_summary,
                       product_name, po_reference, purchaser_identity, event_time):
    """Build the enriched notification message."""
    estimated_value = agreement_details.get('estimated_value', 'N/A')
    currency = agreement_details.get('currency', 'USD')

    message = f"""
════════════════════════════════════════════════════════════════
  AWS MARKETPLACE PURCHASE NOTIFICATION
════════════════════════════════════════════════════════════════

EVENT:           {detail_type}
TIME:            {event_time}
INTENT:          {intent}

────────────────────────────────────────────────────────────────
  PRODUCT & PRICING
────────────────────────────────────────────────────────────────
PRODUCT:         {product_name}
AGREEMENT ID:    {agreement_id}
OFFER ID:        {agreement_details.get('offer_id', 'N/A')}

ESTIMATED VALUE: {currency} {estimated_value}
PURCHASE ORDER:  {po_reference}
PRICING TERMS:
"""
    for term in pricing_summary:
        message += f"  • {term}\n"

    message += f"""
────────────────────────────────────────────────────────────────
  AGREEMENT DETAILS
────────────────────────────────────────────────────────────────
STATUS:          {agreement_details.get('status', 'N/A')}
TYPE:            {agreement_details.get('agreement_type', 'N/A')}
START DATE:      {agreement_details.get('start_time', 'N/A')}
END DATE:        {agreement_details.get('end_time', 'N/A')}
ACCEPTED:        {agreement_details.get('acceptance_time', 'N/A')}

────────────────────────────────────────────────────────────────
  ACCOUNTS
────────────────────────────────────────────────────────────────
BUYER ACCOUNT:   {buyer_account}
SELLER ACCOUNT:  {seller_account}

────────────────────────────────────────────────────────────────
  PURCHASER (WHO INITIATED)
────────────────────────────────────────────────────────────────
IAM IDENTITY:    {purchaser_identity.get('arn', 'N/A')}
USERNAME:        {purchaser_identity.get('username', 'N/A')}
IDENTITY TYPE:   {purchaser_identity.get('type', 'N/A')}
SOURCE IP:       {purchaser_identity.get('source_ip', 'N/A')}

────────────────────────────────────────────────────────────────
  ACTION REQUIRED
────────────────────────────────────────────────────────────────
→ Initiate PO process for this Marketplace purchase
→ Review agreement in AWS Marketplace console:
  https://console.aws.amazon.com/marketplace/home#/agreements/{agreement_id}

════════════════════════════════════════════════════════════════
"""
    return message


def publish_to_sns(message, detail_type, product_name, agreement_details):
    """Publish the enriched notification to SNS."""
    estimated_value = agreement_details.get('estimated_value', 'N/A')
    currency = agreement_details.get('currency', 'USD')
    buyer_account = agreement_details.get('buyer_account', '')

    # Build a useful subject line regardless of data availability
    if estimated_value and estimated_value not in ('N/A', 'Error retrieving', '', '0'):
        subject = f"[Marketplace PO] {product_name} — {currency} {estimated_value}"
    elif 'Usage-Based' in str(agreement_details.get('pricing_type', '')):
        subject = f"[Marketplace PO] {product_name} — Usage-Based Subscription"
    elif product_name and product_name not in ('Unknown Product',):
        subject = f"[Marketplace PO] {product_name} — New Agreement"
    else:
        subject = f"[Marketplace PO] New Purchase — Account {buyer_account}"

    # Prefix with event type for Ending events
    if 'Ending' in detail_type:
        subject = f"[Marketplace RENEWAL] {product_name} — Expiring Soon"

    # SNS subject max 100 chars
    if len(subject) > 100:
        subject = subject[:97] + "..."

    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message,
            MessageAttributes={
                'event_type': {'DataType': 'String', 'StringValue': detail_type},
                'product_name': {'DataType': 'String', 'StringValue': product_name[:256]}
            }
        )
        logger.info(f"Published enriched notification to {SNS_TOPIC_ARN}")
    except Exception as e:
        logger.error(f"Failed to publish to SNS: {e}")
        raise
