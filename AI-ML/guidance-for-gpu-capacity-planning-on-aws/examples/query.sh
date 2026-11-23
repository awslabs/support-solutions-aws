#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# GPU Capacity Planning Agent — CLI query tool (long-running / Function URL)
#
# Usage:
#   ./examples/query.sh "Your capacity question"
#   ./examples/query.sh --model haiku "Simple lookup"
#   ./examples/query.sh --model sonnet "Complex multi-region tradeoff analysis"
#   ./examples/query.sh --model nova "Sandbox-safe query"
#
# Model aliases (resolved to Bedrock model IDs on the allowlist):
#   haiku      -> us.anthropic.claude-haiku-4-5-20251001-v1:0  (default)
#   sonnet     -> us.anthropic.claude-sonnet-4-5-20250929-v1:0 (strongest reasoning)
#   nova       -> us.amazon.nova-pro-v1:0                     (Amazon-family, mid tier)
#   nova-lite  -> us.amazon.nova-lite-v1:0                    (cheap, 1-2 tool queries)
#   nova-micro -> us.amazon.nova-micro-v1:0                   (cheapest, feasibility checks)
#
# Uses the Function URL (up to 180s timeout) for complex multi-tool queries.
# Requires: awscurl (pip install awscurl), python3, AWS credentials.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-gpu-capacity-agent}"

# -----------------------------------------------------------------------------
# Preflight — this script relies on awscurl to SigV4-sign the request to
# the Function URL (which is AuthType=AWS_IAM). awscurl is NOT part of
# the AWS CLI; it's a separate PyPI package. Previously, a missing
# awscurl combined with `2>/dev/null` on the invocation produced a
# totally silent failure — the response body was empty and the user
# got no answer and no clue why. Fail loudly instead.
# -----------------------------------------------------------------------------
if ! command -v awscurl >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Error: awscurl is not installed.

This script needs awscurl to SigV4-sign requests to the Lambda Function
URL (AuthType=AWS_IAM). Install it with:

    pip install awscurl

Then re-run this command. If you don't need Function URL support (e.g.
your queries are < 29 seconds), use ./examples/query-quick.sh instead —
it hits the API Gateway endpoint and only needs curl.
EOF
    exit 127
fi

# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------

MODEL_ID=""
QUERY=""
JSON_MODE=""

usage() {
    cat <<'EOF'
Usage: query.sh [--model <alias|model-id>] [query]

Options:
  --model, -m  Bedrock model alias or full model ID. Defaults to the
               stack default (Haiku 4.5) when omitted. Aliases:
                 haiku      -- strong tool-use, mid-low cost (default)
                 sonnet     -- strongest reasoning, higher cost
                 nova       -- Amazon-family mid tier
                 nova-lite  -- cheap tier, best for 1-2 tool queries
                 nova-micro -- cheapest, simple feasibility checks only
  --json,  -j  Print the raw JSON response for piping to jq; suppresses
               the formatted output and banner.
  --help,  -h  Show this help message.

Positional:
  query        Natural-language capacity question.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model|-m)
            if [[ $# -lt 2 ]]; then
                echo "Error: --model requires a value" >&2
                usage >&2
                exit 2
            fi
            MODEL_ID="$2"
            shift 2
            ;;
        --json|-j)
            JSON_MODE=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --)
            shift
            QUERY="${*:-}"
            break
            ;;
        -*)
            echo "Error: unknown option $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            QUERY="$1"
            shift
            ;;
    esac
done

QUERY="${QUERY:-What GPU instances are available in us-east-1?}"

case "$MODEL_ID" in
    "")         RESOLVED_MODEL_ID="" ;;
    haiku)      RESOLVED_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0" ;;
    sonnet)     RESOLVED_MODEL_ID="us.anthropic.claude-sonnet-4-5-20250929-v1:0" ;;
    nova)       RESOLVED_MODEL_ID="us.amazon.nova-pro-v1:0" ;;
    nova-lite)  RESOLVED_MODEL_ID="us.amazon.nova-lite-v1:0" ;;
    nova-micro) RESOLVED_MODEL_ID="us.amazon.nova-micro-v1:0" ;;
    *)          RESOLVED_MODEL_ID="$MODEL_ID" ;;
esac

# -----------------------------------------------------------------------------
# Function URL discovery (cached across runs)
# -----------------------------------------------------------------------------

FUNCTION_URL_CACHE="/tmp/.gpu-agent-function-url"
if [[ -f "$FUNCTION_URL_CACHE" ]]; then
    FUNCTION_URL=$(cat "$FUNCTION_URL_CACHE")
else
    FUNCTION_URL=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$REGION" \
        --query 'Stacks[0].Outputs[?OutputKey==`FunctionUrl`].OutputValue' \
        --output text)
    echo "$FUNCTION_URL" > "$FUNCTION_URL_CACHE"
fi

# -----------------------------------------------------------------------------
# Build request body and invoke
# -----------------------------------------------------------------------------

PAYLOAD=$(QUERY="$QUERY" MODEL_ID="$RESOLVED_MODEL_ID" python3 -c '
import json, os
payload = {"query": os.environ["QUERY"]}
model_id = os.environ.get("MODEL_ID", "")
if model_id:
    payload["model_id"] = model_id
print(json.dumps(payload))
')

if [[ -z "$JSON_MODE" ]]; then
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🤖 GPU Capacity Planning Agent"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📝 Query: $QUERY"
if [[ -n "$RESOLVED_MODEL_ID" ]]; then
    echo "🧠 Model: $RESOLVED_MODEL_ID"
else
    echo "🧠 Model: <stack default>"
fi
echo ""
echo "⏳ Thinking..."
echo ""
fi

TMPFILE=$(mktemp)
TMPERR=$(mktemp)
# Capture awscurl's stderr separately so transient errors (expired
# credentials, bad Function URL, SigV4 failures) can be surfaced if the
# response body ends up empty. awscurl's own exit code is unreliable —
# it often returns 0 with an empty response and a non-empty stderr.
if ! awscurl --service lambda --region "$REGION" -X POST \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$FUNCTION_URL" > "$TMPFILE" 2> "$TMPERR"; then
    echo "Error: awscurl invocation failed." >&2
    if [[ -s "$TMPERR" ]]; then
        echo "awscurl stderr:" >&2
        cat "$TMPERR" >&2
    fi
    rm -f "$TMPFILE" "$TMPERR"
    exit 1
fi
# Guard against the empty-response silent-failure case even when
# awscurl's exit was 0 (sometimes it swallows network errors).
if [[ ! -s "$TMPFILE" ]]; then
    echo "Error: awscurl returned an empty response from $FUNCTION_URL." >&2
    if [[ -s "$TMPERR" ]]; then
        echo "awscurl stderr (likely the real cause):" >&2
        cat "$TMPERR" >&2
    else
        echo "No stderr output — check AWS credentials, Function URL, and" >&2
        echo "that the Lambda has the lambda:InvokeFunctionUrl permission." >&2
    fi
    rm -f "$TMPFILE" "$TMPERR"
    exit 1
fi
rm -f "$TMPERR"

if [[ -n "$JSON_MODE" ]]; then
    python3 -c "
import json, sys
with open('$TMPFILE') as f:
    data = json.load(f)
if isinstance(data, dict) and isinstance(data.get('body'), str):
    data = json.loads(data['body'])
json.dump(data, sys.stdout, indent=2)
sys.stdout.write('\n')
"
    rm -f "$TMPFILE"
    exit 0
fi

# Parse and display
python3 -c "
import json

# Bedrock on-demand rate card (USD per 1M tokens, as of Jul 2026).
# Matched against the resolved model_id by substring so version-dated
# IDs (e.g. us.anthropic.claude-haiku-4-5-20251001-v1:0) still resolve.
# When a new model lands and this table lags, the footer prints the
# served_model string and 'unknown rate' instead of a wrong number.
_RATES = [
    # (match_substring, input_per_M, output_per_M, label)
    ('claude-haiku-4-5',  1.00,  5.00,  'Haiku 4.5'),
    ('claude-sonnet-4-5', 3.00,  15.00, 'Sonnet 4.5'),
    ('claude-opus-4-5',   15.00, 75.00, 'Opus 4.5'),
    ('nova-pro',          0.80,  3.20,  'Nova Pro'),
    ('nova-lite',         0.06,  0.24,  'Nova Lite'),
    ('nova-micro',        0.035, 0.14,  'Nova Micro'),
]

def cost_for(model_id, input_tokens, output_tokens):
    for substr, in_rate, out_rate, label in _RATES:
        if substr in model_id:
            cost = (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
            return f'Cost ({label}): ~\${cost:.4f}'
    return 'Cost: unknown rate (see aws.amazon.com/bedrock/pricing)'

with open('$TMPFILE') as f:
    raw = f.read()
try:
    data = json.loads(raw)
    # Handle both direct response and API Gateway wrapped response
    if 'body' in data and isinstance(data['body'], str):
        result = json.loads(data['body'])
    else:
        result = data

    # Distinguish the three response shapes the handler can emit:
    #   (a) success:   {'answer': '...', 'tool_calls': N, 'usage': {...}, ...}
    #   (b) 4xx/5xx:   {'error': '...', 'request_id': '...', 'disclaimer': '...'}
    #   (c) malformed: anything else
    # Surface the error field up-front with request_id so operators can
    # correlate to Lambda logs instead of chasing a silent 'thinking' hang.
    answer = result.get('answer', '')
    error = result.get('error', '')

    if answer:
        # Print the answer (markdown formatted)
        print(answer)
    elif error:
        print(f'❌ Error: {error}')
        req_id = result.get('request_id', '')
        if req_id:
            print(f'   request_id: {req_id} (grep Lambda logs for this ID)')
    else:
        api_gw_msg = result.get('message', '')
        if api_gw_msg:
            print(f'❌ API Gateway: {api_gw_msg}')
        else:
            print('No answer received (response body shape unexpected).')
            print(json.dumps(result, indent=2)[:500])

    # Print metadata footer — fields may be missing on error paths.
    usage_block = result.get('usage', {})
    input_tokens = usage_block.get('input_tokens', 0)
    output_tokens = usage_block.get('output_tokens', 0)
    total_tokens = usage_block.get('total_tokens', input_tokens + output_tokens)
    tool_calls = result.get('tool_calls', 0)
    stop_reason = result.get('stop_reason', 'unknown')
    incomplete = result.get('incomplete', False)
    served_model = result.get('model_id', '<unknown>')

    cost_label = cost_for(served_model, input_tokens, output_tokens)

    if error or (not answer and not result.get('message')):
        status = '❌ Error'
    elif incomplete:
        status = '⚠️  Incomplete'
    else:
        status = '✅ Complete'

    print()
    print('━' * 80)
    print(f'🧠 Served by: {served_model}')
    print(f'📊 Tools called: {tool_calls} | Tokens: {total_tokens:,} | '
          f'{cost_label} | '
          f'Status: {status}')

    # Show tool outcomes if any failed
    outcomes = result.get('tool_outcomes', {})
    failed = outcomes.get('failed', [])
    if failed:
        print(f'⚠️  Failed tools: {[f[\"tool\"] for f in failed]}')

    print('━' * 80)
except Exception as e:
    print(f'Error parsing response: {e}')
    print(raw[:500])
"
rm -f "$TMPFILE"
