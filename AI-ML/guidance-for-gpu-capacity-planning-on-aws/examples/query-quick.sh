#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# GPU Capacity Planning Agent — Quick query via API Gateway
#
# Usage:
#   ./examples/query-quick.sh "Your capacity question"
#   ./examples/query-quick.sh --model haiku "Simple lookup"
#   ./examples/query-quick.sh --model sonnet "Complex multi-region tradeoff analysis"
#   ./examples/query-quick.sh --model nova "Sandbox-safe query"
#
# Model aliases (resolved to Bedrock model IDs on the allowlist):
#   haiku      -> us.anthropic.claude-haiku-4-5-20251001-v1:0  (default)
#   sonnet     -> us.anthropic.claude-sonnet-4-5-20250929-v1:0 (strongest reasoning)
#   nova       -> us.amazon.nova-pro-v1:0                     (Amazon-family, mid tier)
#   nova-lite  -> us.amazon.nova-lite-v1:0                    (cheap, 1-2 tool queries)
#   nova-micro -> us.amazon.nova-micro-v1:0                   (cheapest, feasibility checks)
#
# You can also pass a full Bedrock model ID directly (it will be sent
# verbatim to the API and validated against the deployment's allowlist).
# Requests without --model use the stack default (Haiku 4.5).
#
# Uses API Gateway (29s timeout) — best for simple 1-2 tool queries.
# For long-running queries (30-60s), use examples/query.sh which
# targets the Function URL (up to 180s).
#
# Requires: curl, python3, and a deployed stack in $STACK_NAME (default:
# gpu-capacity-agent). AWS credentials must be able to read the stack's
# API Gateway endpoint and API key from CloudFormation.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-gpu-capacity-agent}"

# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------

MODEL_ID=""
QUERY=""
JSON_MODE=""

usage() {
    cat <<'EOF'
Usage: query-quick.sh [--model <alias|model-id>] [query]

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
  query        Natural-language capacity question. Defaults to a demo
               query if omitted.

Examples:
  query-quick.sh "Is p4d.24xlarge available in us-east-1?"
  query-quick.sh --model sonnet "Compare p4d/p5 for a 100-node cluster"
  query-quick.sh --model nova-micro "Simple instance availability check"
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

# Resolve short aliases to full Bedrock model IDs. A value that doesn't
# match a known alias is passed through verbatim — this lets callers
# supply any model ID the deployment's allowlist accepts (including
# region-specific inference profiles like eu.* or apac.*).
case "$MODEL_ID" in
    "")
        # No override; stack default (Haiku 4.5) will serve.
        RESOLVED_MODEL_ID=""
        ;;
    haiku)
        RESOLVED_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0"
        ;;
    sonnet)
        RESOLVED_MODEL_ID="us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        ;;
    nova)
        RESOLVED_MODEL_ID="us.amazon.nova-pro-v1:0"
        ;;
    nova-lite)
        RESOLVED_MODEL_ID="us.amazon.nova-lite-v1:0"
        ;;
    nova-micro)
        RESOLVED_MODEL_ID="us.amazon.nova-micro-v1:0"
        ;;
    *)
        RESOLVED_MODEL_ID="$MODEL_ID"
        ;;
esac

# -----------------------------------------------------------------------------
# Endpoint + API key discovery (cached across runs)
# -----------------------------------------------------------------------------

API_URL_CACHE="/tmp/.gpu-agent-api-url"
API_KEY_CACHE="/tmp/.gpu-agent-api-key"

if [[ -f "$API_URL_CACHE" && -f "$API_KEY_CACHE" ]]; then
    API_URL=$(cat "$API_URL_CACHE")
    API_KEY=$(cat "$API_KEY_CACHE")
else
    API_URL=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$REGION" \
        --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' \
        --output text)
    API_KEY=$(aws apigateway get-api-keys \
        --name-query "$STACK_NAME" \
        --include-values \
        --region "$REGION" \
        --query "items[0].value" \
        --output text)
    echo "$API_URL" > "$API_URL_CACHE"
    echo "$API_KEY" > "$API_KEY_CACHE"
fi

# -----------------------------------------------------------------------------
# Build request body and invoke
# -----------------------------------------------------------------------------

# Build the JSON payload safely via python -c so quoting inside QUERY
# doesn't break the curl command. Includes model_id only when the
# caller supplied one so the stack default applies otherwise.
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
echo "🤖 GPU Capacity Planning Agent (quick mode)"
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
curl -s -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "x-api-key: $API_KEY" \
    -d "$PAYLOAD" > "$TMPFILE"

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
            return f'Cost ({label}): ~\${cost:.4f}', label
    return 'Cost: unknown rate (see aws.amazon.com/bedrock/pricing)', None

with open('$TMPFILE') as f:
    raw = f.read()
try:
    result = json.loads(raw)

    # Distinguish the three response shapes the handler can emit:
    #   (a) success:   {'answer': '...', 'tool_calls': N, 'usage': {...}, 'model_id': '...', ...}
    #   (b) 4xx/5xx:   {'error': '...', 'request_id': '...', 'disclaimer': '...'}
    #   (c) malformed: anything else (e.g. API GW returned {\"message\": \"Forbidden\"})
    #
    # Previously we always printed result.get('answer', 'No answer received.')
    # which hid the error field and made Marketplace-blocked or validation
    # failures look like silent 'thinking' hangs. Surface the error up-front
    # with the request_id so operators can correlate to Lambda logs.
    answer = result.get('answer', '')
    error = result.get('error', '')

    if answer:
        print(answer)
    elif error:
        print(f'❌ Error: {error}')
        req_id = result.get('request_id', '')
        if req_id:
            print(f'   request_id: {req_id} (grep Lambda logs for this ID)')
    else:
        # Response was JSON but had neither answer nor error — likely a
        # proxied API Gateway error (e.g. {\"message\": \"Forbidden\"}) or
        # a bug where the handler returned an empty body.
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
    incomplete = result.get('incomplete', False)
    served_model = result.get('model_id', '<unknown>')

    cost_label, _ = cost_for(served_model, input_tokens, output_tokens)

    # Status reflects success (end_turn) vs incomplete (token-budget / timeout)
    # vs error (the handler returned an error field, no answer).
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
    print('━' * 80)
except Exception as e:
    print(f'Error parsing response: {e}')
    print(raw[:500])
"
rm -f "$TMPFILE"
