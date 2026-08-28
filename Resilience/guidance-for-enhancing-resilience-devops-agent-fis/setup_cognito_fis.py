# ============================================================
# FILE_NAME: setup_cognito_fis.py
# AUTHOR: vsharmro
# DATE: 2026-04-12
# VERSION: 1.0
# PURPOSE: Prompts user for input, processes sub.run, saves config store.. uses HTTP client, OAuth. connects to . connects
#   to .
# DEPENDENCIES: base64, config_store, subprocess, sys, utils
# EXPORTS: prompt, find_existing_pool, find_existing_client, main
# ISSUE : None
# ============================================================
#!/usr/bin/env python3

from utils import run_aws
import config_store

DEFAULT_REGION = "us-east-1"
DEFAULT_POOL_NAME = "FisMcpPool"
DEFAULT_CLIENT_NAME = "FisMcpFISClient"
DEFAULT_DOMAIN = "fismcp"
RS_IDENTIFIER = "default-fis-resource-server"

# ============================================================
# NAME: prompt
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: label, default
# PURPOSE: Prompts user for input.
# CALLED BY: main
# ISSUE : None
# ============================================================
def prompt(label, default):

    value = input(f"  {label} [{default}]: ").strip()
    return value if value else default

# ============================================================
# NAME: find_existing_pool
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: pool_name, region
# PURPOSE: Iterates with conditional logic, using Cognito.
# CALLED BY: main
# ISSUE : None
# ============================================================
def find_existing_pool(pool_name, region):
    result = run_aws(["cognito-idp", "list-user-pools", "--max-results", "60"], region=region)
    if result:
        for pool in result.get("UserPools", []):
            if pool["Name"] == pool_name:
                return pool["Id"]
    return None

# ============================================================
# NAME: find_existing_client
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: pool_id, client_name, region
# PURPOSE: Iterates with conditional logic, using Cognito.
# CALLED BY: main
# ISSUE : None
# ============================================================
def find_existing_client(pool_id, client_name, region):
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
# NAME: main
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PURPOSE: Performs pattern matching, outputs to stdout.
# CALLED BY: <module>
# ISSUE : None
# ============================================================
def main():
    print("=" * 60)
    print("FIS MCP Server — Cognito FIS Setup")
    print("=" * 60)
    print()

    print("Press Enter to accept defaults, or type a new value.")
    print()

    region = prompt("AWS Region", DEFAULT_REGION)
    pool_name = prompt("User Pool name", DEFAULT_POOL_NAME)
    client_name = prompt("FIS Client name", DEFAULT_CLIENT_NAME)
    cognito_domain = prompt("Cognito domain", DEFAULT_DOMAIN)
    print()

    # --- User Pool ---
    pool_id = find_existing_pool(pool_name, region)
    if pool_id:
        print(f"✅ User Pool already exists: {pool_id}")
    else:
        pool = run_aws([
            "cognito-idp", "create-user-pool",
            "--pool-name", pool_name,
            "--policies", '{"PasswordPolicy":{"MinimumLength":12,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":true}}',
        ], region=region, check=True)
        pool_id = pool["UserPool"]["Id"]
        print(f"✅ Created User Pool: {pool_id}")


    # --- Cognito Domain (required for client_credentials token endpoint) ---
    existing_domain = None
    pool_desc = run_aws([
        "cognito-idp", "describe-user-pool",
        "--user-pool-id", pool_id,
    ], region=region)
    if pool_desc:
        existing_domain = pool_desc.get("UserPool", {}).get("Domain")
    if existing_domain:
        domain = existing_domain
        print(f"✅ Cognito domain already exists: {domain}")
    else:
        result = run_aws([
            "cognito-idp", "create-user-pool-domain",
            "--domain", cognito_domain,
            "--user-pool-id", pool_id,
        ], region=region)

        if result is not None:
            domain = cognito_domain
            print(f"✅ Created Cognito domain: {domain}")
        else:
            domain = None
            print(f"⚠️  Could not create domain '{cognito_domain}'. Create manually.")
            return

    # --- Resource Server (required for client_credentials) ---
    existing_rs = run_aws([
        "cognito-idp", "describe-resource-server",
        "--user-pool-id", pool_id,
        "--identifier", RS_IDENTIFIER,
    ], region=region)

    if existing_rs:
        print(f"✅ Resource Server already exists: {RS_IDENTIFIER}")
    else:
        run_aws([
            "cognito-idp", "create-resource-server",
            "--user-pool-id", pool_id,
            "--identifier", RS_IDENTIFIER,
            "--name", "FIS Resource Server",
            "--scopes", "ScopeName=read,ScopeDescription=FIS read access",
        ], region=region, check=True)
        print(f"✅ Created Resource Server: {RS_IDENTIFIER}")
    fis_scope = f"{RS_IDENTIFIER}/read"

    # --- FIS App Client ---
    client_id = find_existing_client(pool_id, client_name, region)
    client_secret = None
    if client_id:
        desc = run_aws([
            "cognito-idp", "describe-user-pool-client",
            "--user-pool-id", pool_id,
            "--client-id", client_id,
        ], region=region)
        if desc:
            client_secret = desc.get("UserPoolClient", {}).get("ClientSecret")
            flows = desc.get("UserPoolClient", {}).get("AllowedOAuthFlows", [])
        if client_secret and "client_credentials" in flows:
            print(f"✅ FIS Client already exists: {client_id}")
        else:
            print(f"⚠️  Existing client not configured for FIS. Recreating...")
            run_aws([
                "cognito-idp", "delete-user-pool-client",
                "--user-pool-id", pool_id,
                "--client-id", client_id,
            ], region=region)
            client_id = None

    if not client_id:
        client = run_aws([
            "cognito-idp", "create-user-pool-client",
            "--user-pool-id", pool_id,
            "--client-name", client_name,
            "--generate-secret",
            "--allowed-o-auth-flows", "client_credentials",
            "--allowed-o-auth-scopes", fis_scope,
            "--allowed-o-auth-flows-user-pool-client",
        ], region=region, check=True)
        client_id = client["UserPoolClient"]["ClientId"]
        client_secret = client["UserPoolClient"].get("ClientSecret")
        print(f"✅ Created FIS Client: {client_id}")

    # --- Test token exchange ---
    token_url = f"https://{domain}.auth.{region}.amazoncognito.com/oauth2/token"
    print(f"\n🔧 Testing client_credentials token exchange...")

    import base64
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": fis_scope}).encode()
        req = urllib.request.Request(token_url, data=data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Authorization", f"Basic {auth_header}")
        resp = urllib.request.urlopen(req)
        token_response = resp.read().decode()
        if "access_token" in token_response:
            print(f"✅ Token exchange successful")
        else:
            print(f"❌ Token exchange failed: {token_response}")
    except Exception as e:
        print(f"❌ Token exchange failed: {e}")

    # --- Output Summary ---
    discovery_url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
    exchange_url = f"https://{domain}.auth.{region}.amazoncognito.com/oauth2/token"

    # Save all values for subsequent scripts
    config_store.save({
        "region": region,
        "pool_id": pool_id,
        "pool_name": pool_name,
        "cognito_domain": domain,
        "client_id": client_id,
        "client_secret": client_secret,
        "discovery_url": discovery_url,
        "exchange_url": exchange_url,
        "m2m_scope": fis_scope,
    })
    print()
    print("=" * 60)
    print("FIS COGNITO SETUP COMPLETE")
    print("=" * 60)

    print(f"  Pool ID:        {pool_id}")

    print(f"  Client ID:      {client_id}")

    masked_secret = f"****{client_secret[-4:]}" if client_secret and len(client_secret) > 4 else "****"

    print(f"  Client Secret:  {masked_secret}")

    print(f"  Discovery URL:  {discovery_url}")

    print(f"  Exchange URL:   {exchange_url}")

    print(f"  Scope:          {fis_scope}")

    print()

    print("FOR AGENTCORE CONFIGURE (OAuth = yes):")

    print(f"  Discovery URL:  {discovery_url}")

    print(f"  Client ID:      {client_id}")
    print()

    print("FOR DEVOPS AGENT CONSOLE (OAuth Client Credentials):")

    print(f"  Client ID:      {client_id}")

    print(f"  Client Secret:  {masked_secret}")

    print(f"  Exchange URL:   {exchange_url}")

    print(f"  Scope:          {fis_scope}")

    print()
    print("  ⚠️  Full secret saved to .fis_config.json (not committed to git)")

    print("=" * 60)

if __name__ == "__main__":
    main()

