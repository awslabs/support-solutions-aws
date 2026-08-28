# ============================================================
# FILE_NAME: utils.py
# AUTHOR: vsharmro
# DATE: 2026-04-12
# VERSION: 1.0
# PURPOSE: Provides run_aws.
# DEPENDENCIES: json, os, subprocess
# EXPORTS: run_aws
# ISSUE :
#   - Possibly unused import: 'os' at line 3.
# ============================================================
import json
import subprocess


# ============================================================
# NAME: run_aws
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: args, region, capture, check
# PURPOSE: Processes JSON data, with error handling.
# ISSUE : None
# ============================================================
def run_aws(args, region="us-east-1", capture=True, check=False):
    cmd = ["aws"] + args + ["--region", region, "--output", "json"]
    try:
        result = subprocess.run(cmd, capture_output=capture, text=True, check=check)
        if capture and result.stdout.strip():
            return json.loads(result.stdout)
        return None
    except subprocess.CalledProcessError:
        return None
    except json.JSONDecodeError:
        return result.stdout.strip() if capture else None
