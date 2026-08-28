# ============================================================
# FILE_NAME: deploy_lambda.py
# AUTHOR: vsharmro
# DATE: 2026-04-11
# VERSION: 1.0
# PURPOSE: Prompts user for input config store., ensures iam role, builds package. uses config file echo
#   {"tool":"recommend_fis_experiments","arguments":{"finding":{"summary":"network latency"}}} > payload.json. uses config
#   file --payload fileb://payload.json response.json.
# DEPENDENCIES: config_store, json, os, shutil, subprocess, sys, time, utils, zipfile
# EXPORTS: get_account_id, ensure_iam_role, build_package, deploy_function, main
# ISSUE : None
# ============================================================
#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from utils import run_aws
import config_store

REGION = "us-east-1"
FUNCTION_NAME = "fis-recommender-mcp-client"
ROLE_NAME = "fis-mcp-lambda-role"
RUNTIME = "python3.11"
TIMEOUT = 120
MEMORY = 512
PACKAGE_DIR = "lambda_package"
ZIP_FILE = "lambda_deployment.zip"


# ============================================================
# NAME: get_account_id
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11 / 2026-04-12
# PURPOSE: Exits on error, outputs to stdout.
# CALLED BY: main
# ISSUE : None
# ============================================================
def get_account_id():
    result = run_aws(["sts", "get-caller-identity"], region=REGION)
    if not result:
        print("❌ Could not get AWS account ID. Check credentials.")
        sys.exit(1)
    return result["Account"]


# ============================================================
# NAME: ensure_iam_role
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11 / 2026-04-12
# PARAMETERS: account_id
# PURPOSE: Processes JSON data, outputs to stdout, using IAM, Lambda, SNS.
# CALLED BY: main
# ISSUE : None
# ============================================================
def ensure_iam_role(account_id):
    print("🔍 Checking IAM role...")
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

    sns_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": "sns:Publish",
            "Resource": f"arn:aws:sns:{REGION}:{account_id}:devops-agent-fis-recommendations"
        }]
    })
    run_aws([
        "iam", "put-role-policy",
        "--role-name", ROLE_NAME,
        "--policy-name", "sns-publish",
        "--policy-document", sns_policy,
    ], region=REGION, check=True)

    secrets_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": f"arn:aws:secretsmanager:{REGION}:{account_id}:secret:fis-mcp/*"
        }]
    })
    run_aws([
        "iam", "put-role-policy",
        "--role-name", ROLE_NAME,
        "--policy-name", "secrets-read",
        "--policy-document", secrets_policy,
    ], region=REGION, check=True)

    arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
    print(f"✅ Created IAM role: {arn}")
    print("   Waiting 10s for role propagation...")
    time.sleep(10)
    return arn


# ============================================================
# NAME: build_package
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11 / 2026-04-12
# PURPOSE: Performs pattern matching, iterates with conditional logic, outputs to stdout, using Lambda.
# CALLED BY: main
# ISSUE : None
# ============================================================
def build_package():
    print("📦 Building deployment package...")

    if os.path.exists(PACKAGE_DIR):
        shutil.rmtree(PACKAGE_DIR)
    os.makedirs(PACKAGE_DIR)

    subprocess.run([
        sys.executable, "-m", "pip", "install",
        "-q", "--target", PACKAGE_DIR,
        "--platform", "manylinux2014_x86_64",
        "--implementation", "cp",
        "--python-version", "3.11",
        "--only-binary=:all:",
        "-r", "lambda_requirements.txt",
    ], check=True)

    shutil.copy("lambda_function.py", PACKAGE_DIR)

    if os.path.exists(ZIP_FILE):
        os.remove(ZIP_FILE)

    with zipfile.ZipFile(ZIP_FILE, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(PACKAGE_DIR):
            for f in files:
                full_path = os.path.join(root, f)
                arc_name = os.path.relpath(full_path, PACKAGE_DIR)
                zf.write(full_path, arc_name)

    shutil.rmtree(PACKAGE_DIR)

    size_mb = os.path.getsize(ZIP_FILE) / (1024 * 1024)
    print(f"✅ Package created: {ZIP_FILE} ({size_mb:.1f} MB)")
    return ZIP_FILE


# ============================================================
# NAME: deploy_function
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11 / 2026-04-12
# PARAMETERS: role_arn, env_vars
# PURPOSE: Processes JSON data, outputs to stdout, using Lambda.
# CALLED BY: main
# ISSUE : None
# ============================================================
def deploy_function(role_arn, env_vars):
    env_json = json.dumps({"Variables": env_vars})

    existing = run_aws([
        "lambda", "get-function",
        "--function-name", FUNCTION_NAME,
    ], region=REGION)

    if existing:
        print("🔄 Updating Lambda function code...")
        run_aws([
            "lambda", "update-function-code",
            "--function-name", FUNCTION_NAME,
            "--zip-file", f"fileb://{ZIP_FILE}",
        ], region=REGION, check=True)

        print("   Waiting for code update...")
        time.sleep(5)

        print("🔄 Updating Lambda configuration...")
        run_aws([
            "lambda", "update-function-configuration",
            "--function-name", FUNCTION_NAME,
            "--environment", env_json,
            "--timeout", str(TIMEOUT),
            "--memory-size", str(MEMORY),
        ], region=REGION, check=True)
        print(f"✅ Updated Lambda: {FUNCTION_NAME}")
    else:
        print("🚀 Creating Lambda function...")
        run_aws([
            "lambda", "create-function",
            "--function-name", FUNCTION_NAME,
            "--runtime", RUNTIME,
            "--role", role_arn,
            "--handler", "lambda_function.lambda_handler",
            "--zip-file", f"fileb://{ZIP_FILE}",
            "--timeout", str(TIMEOUT),
            "--memory-size", str(MEMORY),
            "--environment", env_json,
        ], region=REGION, check=True)
        print(f"✅ Created Lambda: {FUNCTION_NAME}")

    os.remove(ZIP_FILE)


# ============================================================
# NAME: main
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11 / 2026-04-12
# PURPOSE: Outputs to stdout.
# CALLED BY: <module>
# ISSUE : None
# ============================================================
def main():
    print("=" * 60)
    print("FIS Recommender — Lambda Client Deployment")
    print("=" * 60)
    print()
    print("Deploys a Lambda that calls the MCP server via AgentCore")
    print("using FIS OAuth (client_credentials).")
    print()

    agent_arn = config_store.prompt("Agent ARN", "agent_arn")
    client_id = config_store.prompt("Cognito FIS Client ID", "client_id")
    client_secret = config_store.prompt("Cognito FIS Client Secret", "client_secret")
    token_url = config_store.prompt("Cognito Token URL", "exchange_url")
    scope = config_store.prompt("OAuth Scope", "fis_scope", "default-fis-resource-server/read")
    sns_topic = config_store.prompt("SNS Topic ARN (optional, for notifications)", "sns_topic_arn")

    if not all([agent_arn, client_id, client_secret, token_url]):
        print("❌ All values are required.")
        return

    print()
    account_id = get_account_id()
    print(f"   Account: {account_id}")
    print(f"   Region:  {REGION}")
    print()

    role_arn = ensure_iam_role(account_id)
    build_package()

    # Store client secret in Secrets Manager
    secret_name = f"fis-mcp/{FUNCTION_NAME}/cognito-client-secret"
    print(f"🔐 Storing client secret in Secrets Manager ({secret_name})...")
    existing_secret = run_aws([
        "secretsmanager", "describe-secret",
        "--secret-id", secret_name,
    ], region=REGION)
    if existing_secret:
        run_aws([
            "secretsmanager", "put-secret-value",
            "--secret-id", secret_name,
            "--secret-string", client_secret,
        ], region=REGION, check=True)
    else:
        run_aws([
            "secretsmanager", "create-secret",
            "--name", secret_name,
            "--secret-string", client_secret,
        ], region=REGION, check=True)
    secret_arn = f"arn:aws:secretsmanager:{REGION}:{account_id}:secret:{secret_name}"
    print(f"✅ Secret stored: {secret_name}")

    env_vars = {
        "AGENT_ARN": agent_arn,
        "COGNITO_CLIENT_ID": client_id,
        "COGNITO_CLIENT_SECRET_ARN": secret_arn,
        "COGNITO_TOKEN_URL": token_url,
        "COGNITO_SCOPE": scope,
    }
    if sns_topic:
        env_vars["SNS_TOPIC_ARN"] = sns_topic

    deploy_function(role_arn, env_vars)

    print()
    print("=" * 60)
    print("DEPLOYMENT COMPLETE")
    print("=" * 60)
    print(f"  Function: {FUNCTION_NAME}")
    print(f"  Region:   {REGION}")
    print(f"  Runtime:  {RUNTIME}")
    print()
    print("Test with:")
    print(f"  echo {{\"tool\":\"recommend_fis_experiments\",\"arguments\":{{\"finding\":{{\"summary\":\"network latency\"}}}}}} > payload.json")
    print(f"  aws lambda invoke --function-name {FUNCTION_NAME} --region {REGION} --payload fileb://payload.json response.json")
    print(f"  python -c \"import json; print(json.dumps(json.load(open('response.json')), indent=2))\"")
    print("=" * 60)


if __name__ == "__main__":
    main()
