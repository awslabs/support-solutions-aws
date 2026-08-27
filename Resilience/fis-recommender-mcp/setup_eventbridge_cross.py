# ============================================================
# FILE_NAME: setup_eventbridge_cross.py
# AUTHOR: vsharmro
# DATE: 2026-04-12
# VERSION: 1.0
# PURPOSE: Prompts user for input config store., ensures sns topic, Create Lambda execution role if it doesn't exist.
# DEPENDENCIES: config_store, json, os, sys, time, utils
# EXPORTS: get_account_id, ensure_sns_topic, ensure_iam_role, ensure_eventbridge_rule, get_lambda_code_path, main
# ISSUE : None
# ============================================================
#!/usr/bin/env python3
import json
import os
import sys
import time
from utils import run_aws
import config_store

REGION = "us-east-1"
SNS_TOPIC_NAME = "devops-agent-fis-recommendations"
RULE_NAME = "DevOpsAgentFISRecommendations"
FUNCTION_NAME = "DevOpsAgentFISRecommender"
ROLE_NAME = "DevOpsAgentFISRecommenderRole"


# ============================================================
# NAME: get_account_id
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PURPOSE: Exits on error, outputs to stdout.
# CALLED BY: main
# ISSUE : None
# ============================================================
def get_account_id():
    result = run_aws(["sts", "get-caller-identity"], region=REGION)
    if not result:
        print("❌ Could not get AWS account ID.")
        sys.exit(1)
    return result["Account"]


# ============================================================
# NAME: ensure_sns_topic
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: account_id
# PURPOSE: Computes and returns a result, outputs to stdout, using SNS.
# CALLED BY: main
# ISSUE : None
# ============================================================
def ensure_sns_topic(account_id):
    topic_arn = f"arn:aws:sns:{REGION}:{account_id}:{SNS_TOPIC_NAME}"
    result = run_aws(["sns", "get-topic-attributes", "--topic-arn", topic_arn], region=REGION)
    if result:
        print(f"✅ SNS topic exists: {topic_arn}")
        config_store.save({"sns_topic_arn": topic_arn})
        return topic_arn

    result = run_aws(["sns", "create-topic", "--name", SNS_TOPIC_NAME], region=REGION, check=True)
    topic_arn = result["TopicArn"]
    print(f"✅ Created SNS topic: {topic_arn}")
    config_store.save({"sns_topic_arn": topic_arn})
    return topic_arn


# ============================================================
# NAME: ensure_iam_role
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: account_id
# PURPOSE: Create Lambda execution role if it doesn't exist.
# CALLED BY: main
# ISSUE : None
# ============================================================
def ensure_iam_role(account_id):
    """Create Lambda execution role if it doesn't exist."""
    existing = run_aws(["iam", "get-role", "--role-name", ROLE_NAME], region=REGION)
    if existing:
        arn = existing["Role"]["Arn"]
        print(f"✅ IAM role exists: {arn}")
        return arn

    print("🔧 Creating IAM role...")
    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    })

    run_aws([
        "iam", "create-role",
        "--role-name", ROLE_NAME,
        "--assume-role-policy-document", trust_policy,
    ], region=REGION, check=True)

    run_aws([
        "iam", "attach-role-policy",
        "--role-name", ROLE_NAME,
        "--policy-arn", "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    ], region=REGION, check=True)

    # SNS publish permission
    sns_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": "sns:Publish",
            "Resource": f"arn:aws:sns:{REGION}:{account_id}:{SNS_TOPIC_NAME}"
        }]
    })
    run_aws([
        "iam", "put-role-policy",
        "--role-name", ROLE_NAME,
        "--policy-name", "sns-publish",
        "--policy-document", sns_policy,
    ], region=REGION, check=True)

    arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
    print(f"✅ Created IAM role: {arn}")

    print("   Waiting 10s for role propagation...")
    time.sleep(10)
    return arn


# ============================================================
# NAME: ensure_eventbridge_rule
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: account_id
# PURPOSE: Create EventBridge rule for DevOps Agent events.
# CALLED BY: main
# ISSUE : None
# ============================================================
def ensure_eventbridge_rule(account_id):
    """Create EventBridge rule for DevOps Agent events."""
    existing = run_aws([
        "events", "describe-rule", "--name", RULE_NAME,
    ], region=REGION)
    if existing:
        print(f"✅ EventBridge rule exists: {RULE_NAME}")
        return existing.get("Arn")

    event_pattern = json.dumps({
        "source": ["aws.devops-agent"],
        "detail-type": ["DevOps Agent Investigation Complete"]
    })

    result = run_aws([
        "events", "put-rule",
        "--name", RULE_NAME,
        "--event-pattern", event_pattern,
        "--state", "ENABLED",
        "--description", "Trigger FIS recommendations on DevOps Agent investigations",
    ], region=REGION, check=True)

    rule_arn = result.get("RuleArn")
    print(f"✅ Created EventBridge rule: {rule_arn}")
    return rule_arn


def get_lambda_code_path():
    """Return path to the hardened lambda_function.py (reuses F2/F8 fixes)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "lambda_function.py")


# ============================================================
# NAME: main
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PURPOSE: Writes output to file, outputs to stdout.
# CALLED BY: <module>
# ISSUE : None
# ============================================================
def main():
    print("=" * 60)
    print("FIS Recommender — EventBridge Integration Setup")
    print("=" * 60)
    print()
    print("This sets up automatic FIS recommendations on every")
    print("DevOps Agent investigation via EventBridge + Lambda + SNS.")
    print()

    account_id = get_account_id()
    print(f"   Account: {account_id}")
    print(f"   Region:  {REGION}")
    print()

    # Collect FIS values
    print("Enter values from setup_cognito_fis.py and agentcore deploy:")
    print()
    agent_arn = config_store.prompt("Agent ARN", "agent_arn")
    client_id = config_store.prompt("FIS Client ID", "client_id")
    client_secret = config_store.prompt("FIS Client Secret", "client_secret")
    token_url = config_store.prompt("Token URL", "exchange_url")
    scope = config_store.prompt("Scope", "fis_scope", "default-fis-resource-server/read")
    email = config_store.prompt("Notification email (for SNS subscription)", "notification_email")
    print()

    if not all([agent_arn, client_id, client_secret, token_url]):
        print("❌ Agent ARN and Cognito values are required.")
        return

    # Create resources
    sns_topic_arn = ensure_sns_topic(account_id)
    role_arn = ensure_iam_role(account_id)
    ensure_eventbridge_rule(account_id)

    # Use the hardened lambda_function.py (includes F2 Secrets Manager + F8 token URL validation)
    lambda_code_path = get_lambda_code_path()
    if not os.path.exists(lambda_code_path):
        print(f"❌ lambda_function.py not found at {lambda_code_path}")
        return

    print(f"\n📄 Using hardened Lambda handler: {lambda_code_path}")

    # Subscribe email to SNS
    if email:
        run_aws([
            "sns", "subscribe",
            "--topic-arn", sns_topic_arn,
            "--protocol", "email",
            "--notification-endpoint", email,
        ], region=REGION)
        print(f"📧 SNS subscription sent to {email} (check inbox to confirm)")
    else:
        print("⚠️  No email provided. To subscribe later, run:")
        print(f"  aws sns subscribe --topic-arn {sns_topic_arn} --protocol email --notification-endpoint YOUR_EMAIL --region {REGION}")

    # Summary
    print()
    print("=" * 60)
    print("EVENTBRIDGE SETUP COMPLETE")
    print("=" * 60)
    print(f"  SNS Topic:        {sns_topic_arn}")
    print(f"  EventBridge Rule: {RULE_NAME}")
    print(f"  IAM Role:         {role_arn}")
    print(f"  Lambda Code:      {lambda_code_path}")
    print()
    print("Next steps:")
    print("  1. Deploy the Lambda using: python deploy_lambda.py")
    print("     (use the generated Lambda code as lambda_function.py)")
    print("  2. Add EventBridge target:")
    print(f"     aws events put-targets --rule {RULE_NAME} \\")
    print(f"       --targets Id=1,Arn=arn:aws:lambda:{REGION}:{account_id}:function:{FUNCTION_NAME}")
    print("  3. Grant EventBridge permission to invoke Lambda:")
    print(f"     aws lambda add-permission --function-name {FUNCTION_NAME} \\")
    print(f"       --statement-id eventbridge-invoke \\")
    print(f"       --action lambda:InvokeFunction \\")
    print(f"       --principal events.amazonaws.com \\")
    print(f"       --source-arn arn:aws:events:{REGION}:{account_id}:rule/{RULE_NAME}")
    print()
    print("  Lambda environment variables needed:")
    print(f"    AGENT_ARN={agent_arn}")
    print(f"    COGNITO_CLIENT_ID={client_id}")
    masked_secret = f"****{client_secret[-4:]}" if client_secret and len(client_secret) > 4 else "****"
    print(f"    COGNITO_CLIENT_SECRET={masked_secret} (retrieve full value from .fis_config.json)")
    print(f"    COGNITO_TOKEN_URL={token_url}")
    print(f"    COGNITO_SCOPE={scope}")
    print(f"    SNS_TOPIC_ARN={sns_topic_arn}")
    print("=" * 60)


if __name__ == "__main__":
    main()
