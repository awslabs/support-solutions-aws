# ============================================================
# FILE_NAME: cleanup.py
# AUTHOR: vsharmro
# DATE: 2026-04-11
# VERSION: 1.0
# PURPOSE: Provides public functions:cleanup_agentcore,
#   cleanup_cognito, cleanup_lambda, cleanup_config_files.
#   Includes a CLI entry point.
# DEPENDENCIES: os, subprocess, sys, utils
# EXPORTS: cleanup_agentcore, cleanup_cognito, cleanup_lambda
#   cleanup_config_files
# ISSUE :
#   - No module-level docstring.
#   - Possibly unused import: 'sys' at line 4.
# ============================================================
#!/usr/bin/env python3
import os
import subprocess
from utils import run_aws

REGION = "us-east-1"


# ============================================================
# NAME: cleanup_agentcore
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11
# PURPOSE: Reads file content, iterates with conditional
#   logic, outputs to stdout.
# CALLED BY: <module>
# ISSUE : None
# ============================================================
def cleanup_agentcore():
    print("=" * 60)
    print("AGENTCORE CLEANUP")
    print("=" * 60)

    yaml_path = ".bedrock_agentcore.yaml"
    if not os.path.exists(yaml_path):
        print("No .bedrock_agentcore.yaml found. Skipping agentcore cleanup.")
        return

    agents = []
    with open(yaml_path) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("agent_arn:"):
                arn = stripped.split(":", 1)[1].strip()
                if arn and arn != "null":
                    agents.append(arn)

    if not agents:
        print("No agent ARNs found in yaml.")
        return

    print(f"\nFound {len(agents)} agent(s):")
    for arn in agents:
        print(f"  {arn}")

    confirm = input("\nDelete ALL these agents? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Skipped agentcore cleanup.")
        return

    for arn in agents:
        agent_name = arn.split("/")[-1].split("-")[0]
        print(f"\n🗑️  Destroying {agent_name}...")
        subprocess.run(
            ["agentcore", "destroy", "--agent", agent_name, "--force"],
            timeout=120,
        )


# ============================================================
# NAME: cleanup_cognito
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11
# PURPOSE: Builds a collection from results, iterates with
#   conditional logic, outputs to stdout.
# CALLED BY: <module>
# ISSUE : None
# ============================================================
def cleanup_cognito():
    print()
    print("=" * 60)
    print("COGNITO CLEANUP")
    print("=" * 60)

    result = run_aws(["cognito-idp", "list-user-pools", "--max-results", "60"], region=REGION)
    if not result:
        print("No user pools found.")
        return

    pools = result.get("UserPools", [])
    target_pools = [p for p in pools if p["Name"] in ("FisMcpPool", "MyUserPool")]

    if not target_pools:
        print("No FisMcpPool or MyUserPool found.")
        return

    print(f"\nFound {len(target_pools)} pool(s):")
    for p in target_pools:
        print(f"  {p['Name']} — {p['Id']}")

    confirm = input("\nDelete ALL these pools and their clients/users/domains? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Skipped Cognito cleanup.")
        return

    for p in target_pools:
        pool_id = p["Id"]
        pool_name = p["Name"]
        print(f"\n🗑️  Cleaning up {pool_name} ({pool_id})...")

        desc = run_aws([
            "cognito-idp", "describe-user-pool",
            "--user-pool-id", pool_id,
        ], region=REGION)
        if desc:
            domain = desc.get("UserPool", {}).get("Domain")
            if domain:
                print(f"   Deleting domain: {domain}")
                run_aws([
                    "cognito-idp", "delete-user-pool-domain",
                    "--domain", domain,
                    "--user-pool-id", pool_id,
                ], region=REGION)

        run_aws([
            "cognito-idp", "delete-user-pool",
            "--user-pool-id", pool_id,
        ], region=REGION)
        print(f"   ✅ Deleted {pool_name}")


# ============================================================
# NAME: cleanup_lambda
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11
# PURPOSE: Outputs to stdout.
# CALLED BY: <module>
# ISSUE : None
# ============================================================
def cleanup_lambda():
    print()
    print("=" * 60)
    print("LAMBDA CLEANUP")
    print("=" * 60)

    function_name = "fis-recommender-mcp-client"
    role_name = "fis-mcp-lambda-role"

    result = run_aws(["lambda", "get-function", "--function-name", function_name], region=REGION)
    if result:
        confirm = input(f"\nDelete Lambda function '{function_name}'? (yes/no): ").strip().lower()
        if confirm == "yes":
            run_aws(["lambda", "delete-function", "--function-name", function_name], region=REGION)
            print(f"   ✅ Deleted Lambda: {function_name}")

            run_aws([
                "iam", "detach-role-policy",
                "--role-name", role_name,
                "--policy-arn", "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            ], region=REGION)
            run_aws(["iam", "delete-role-policy", "--role-name", role_name, "--policy-name", "cognito-access"], region=REGION)
            run_aws(["iam", "delete-role-policy", "--role-name", role_name, "--policy-name", "sns-publish"], region=REGION)
            run_aws(["iam", "delete-role", "--role-name", role_name], region=REGION)
            print(f"   ✅ Deleted IAM role: {role_name}")
    else:
        print(f"No Lambda function '{function_name}' found.")


# ============================================================
# NAME: cleanup_config_files
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11
# PURPOSE: Iterates with conditional logic, outputs to stdout.
# CALLED BY: <module>
# ISSUE : None
# ============================================================
def cleanup_config_files():
    for f in [".bedrock_agentcore.yaml", ".fis_config.json"]:
        if os.path.exists(f):
            os.remove(f)
            print(f"   ✅ Deleted {f}")


if __name__ == "__main__":
    cleanup_cognito()
    cleanup_lambda()
    cleanup_agentcore()
    cleanup_config_files()
    print()
    print("=" * 60)
    print("CLEANUP COMPLETE")
    print("=" * 60)
    print("Start fresh with:")
    print("  1. python setup_cognito_fis.py")
    print("  2. agentcore configure -e server.py --protocol MCP")
    print("  3. agentcore deploy")
