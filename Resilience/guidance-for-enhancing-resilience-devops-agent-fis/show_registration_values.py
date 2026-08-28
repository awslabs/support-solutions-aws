# ============================================================
# FILE_NAME: show_registration_values.py
# AUTHOR: vsharmro
# DATE: 2026-04-12
# VERSION: 1.0
# PURPOSE: Loads agentcore config, saves config store.. uses HTTP client. connects to . connects to .
# DEPENDENCIES: config_store, os, sys, urllib, utils, yaml
# EXPORTS: load_agentcore_config, find_cognito_pool, find_cognito_client, get_cognito_domain, main
# ISSUE : None
# ============================================================
#!/usr/bin/env python3
import sys
import os
from urllib.parse import quote

try:
    import yaml
except ImportError:
    yaml = None

from utils import run_aws
import config_store

CONFIG_FILE = ".bedrock_agentcore.yaml"


# ============================================================
# NAME: load_agentcore_config
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PURPOSE: Reads file content, iterates with conditional logic, exits on error, outputs to stdout, using Bedrock.
# CALLED BY: main
# ISSUE : None
# ============================================================
def load_agentcore_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ {CONFIG_FILE} not found. Run 'agentcore configure' and 'agentcore deploy' first.")
        sys.exit(1)

    with open(CONFIG_FILE, "r") as f:
        if yaml:
            config = yaml.safe_load(f)
        else:
            # Minimal fallback: grep for the values we need
            content = f.read()
            config = None
            agent_arn = None
            region = None
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("agent_arn:"):
                    agent_arn = stripped.split(":", 1)[1].strip()
                if stripped.startswith("region:") and not region:
                    region = stripped.split(":", 1)[1].strip()
            return agent_arn, region

    default_agent = config.get("default_agent")
    agent = config.get("agents", {}).get(default_agent, {})
    agent_arn = agent.get("bedrock_agentcore", {}).get("agent_arn")
    region = agent.get("aws", {}).get("region")
    return agent_arn, region


# ============================================================
# NAME: find_cognito_pool
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: pool_name, region
# PURPOSE: Iterates with conditional logic, using Cognito.
# CALLED BY: main
# ISSUE : None
# ============================================================
def find_cognito_pool(pool_name, region):
    result = run_aws(["cognito-idp", "list-user-pools", "--max-results", "60"], region=region)
    if result:
        for pool in result.get("UserPools", []):
            if pool["Name"] == pool_name:
                return pool["Id"]
    return None


# ============================================================
# NAME: find_cognito_client
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: pool_id, client_name, region
# PURPOSE: Iterates with conditional logic, using Cognito.
# CALLED BY: main
# ISSUE : None
# ============================================================
def find_cognito_client(pool_id, client_name, region):
    result = run_aws([
        "cognito-idp", "list-user-pool-clients",
        "--user-pool-id", pool_id, "--max-results", "60",
    ], region=region)
    if result:
        for client in result.get("UserPoolClients", []):
            if client["ClientName"] == client_name:
                return client["ClientId"]
    return None


# ============================================================
# NAME: get_cognito_domain
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: pool_id, region
# PURPOSE: Computes and returns a result, using Cognito.
# CALLED BY: main
# ISSUE : None
# ============================================================
def get_cognito_domain(pool_id, region):
    result = run_aws([
        "cognito-idp", "describe-user-pool",
        "--user-pool-id", pool_id,
    ], region=region)
    if result:
        return result.get("UserPool", {}).get("Domain")
    return None


# ============================================================
# NAME: main
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PURPOSE: Iterates with conditional logic, exits on error, outputs to stdout.
# CALLED BY: <module>
# ISSUE :
#   - High cyclomatic complexity (11 branches) — consider refactoring.
# ============================================================
def main():
    agent_arn, region = load_agentcore_config()

    if not agent_arn:
        print("❌ Agent ARN not found in config. Run 'agentcore deploy' first.")
        sys.exit(1)
    if not region:
        region = "us-east-1"

    encoded_arn = quote(agent_arn, safe="")
    endpoint = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"

    # Save agent ARN and endpoint for subsequent scripts
    config_store.save({
        "agent_arn": agent_arn,
        "endpoint": endpoint,
        "region": region,
    })

    # Try both pool names used in the project
    # Check Cognito in the saved config region first, then agent region
    cognito_region = config_store.get("region", region)
    pool_id = None
    client_id = None
    client_secret = None
    for search_region in dict.fromkeys([cognito_region, region, "us-east-1"]):
        for pool_name, client_name in [("FisMcpPool", "FisMcpFISClient"), ("FisMcpPool", "FisMcpClient"), ("MyUserPool", "MyClient")]:
            pool_id = find_cognito_pool(pool_name, search_region)
            if pool_id:
                client_id = find_cognito_client(pool_id, client_name, search_region)
                if client_id:
                    desc = run_aws([
                        "cognito-idp", "describe-user-pool-client",
                        "--user-pool-id", pool_id,
                        "--client-id", client_id,
                    ], region=search_region)
                    if desc:
                        client_secret = desc.get("UserPoolClient", {}).get("ClientSecret")
                    cognito_region = search_region
                    break
        if client_id:
            break

    cognito_domain = None
    if pool_id:
        cognito_domain = get_cognito_domain(pool_id, cognito_region)

    # --- Output ---
    print()
    print("=" * 70)
    print("DEVOPS AGENT CONSOLE — MCP SERVER REGISTRATION VALUES")
    print("=" * 70)
    print()
    print(f"  Agent ARN:     {agent_arn}")
    print(f"  Region:        {region}")
    print()
    print(f"  Endpoint:")
    print(f"    {endpoint}")
    print()

    if client_id:
        print(f"  Client ID:     {client_id}")
    else:
        print("  Client ID:     ❌ Not found. Run setup_cognito_cross.py first.")

    if client_secret:
        masked_secret = f"****{client_secret[-4:]}" if len(client_secret) > 4 else "****"
        print(f"  Client Secret: {masked_secret} (full value saved to .fis_config.json)")
    else:
        print("  Client Secret: ❌ Not found.")

    if pool_id:
        print(f"  Discovery URL: https://cognito-idp.{cognito_region}.amazonaws.com/{pool_id}/.well-known/openid-configuration")
    else:
        print("  Discovery URL: ❌ No Cognito pool found.")

    print()
    if cognito_domain:
        exchange_url = f"https://{cognito_domain}.auth.{cognito_region}.amazoncognito.com/oauth2/token"
        auth_url = f"https://{cognito_domain}.auth.{cognito_region}.amazoncognito.com/oauth2/authorize"
        print(f"  Exchange URL:")
        print(f"    {exchange_url}")
        print()
        print(f"  Authorization URL:")
        print(f"    {auth_url}")
    else:
        print("  Exchange URL:      ❌ No Cognito domain found.")
        print("  Authorization URL: ❌ No Cognito domain found.")
        print()
        print("  To create a Cognito domain, run:")
        print(f"    aws cognito-idp create-user-pool-domain --domain YOUR_DOMAIN --user-pool-id {pool_id or 'POOL_ID'} --region {region}")
        print("  Then re-run this script.")

    print()
    print(f"  Scopes:        default-fis-resource-server/read")
    print(f"  Auth Flow:     OAuth Client Credentials")

    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
