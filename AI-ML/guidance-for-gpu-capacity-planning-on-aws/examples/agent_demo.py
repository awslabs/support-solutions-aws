# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
End-to-end agent demo with public AWS API tools.

Run:
    python examples/agent_demo.py [scenario]

Scenarios:
    feasible      — g5.2xlarge in us-east-1 (should pass all checks)
    blocked       — g7e.2xlarge in sa-east-1 (not offered → alternatives)
    quota         — 200 instances in a region (likely exceeds quota)
    pricing       — multi-region pricing comparison (SageMaker)
    ec2_compare   — SageMaker vs EC2 hourly cost (exercises get_ec2_pricing)
    large_fleet   — 22 g6 in af-south-1 (exercises get_spot_placement_score)
    model_deploy  — Llama 70B sizing (exercises get_reference_data + availability + pricing)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent import CapacityDecisionAgent
from src.tools import ALL_TOOLS


SCENARIOS = {
    "feasible": (
        "I need to deploy 20 ml.g5.2xlarge instances for SageMaker real-time "
        "inference in us-east-1. Check availability, pricing, and my quota. "
        "Is this deployable today?"
    ),
    "blocked": (
        "We want 150 ml.g7e.2xlarge instances in sa-east-1 for SageMaker hosting. "
        "Check if this instance type is available there. If not, what are the best "
        "GPU alternatives in sa-east-1 and what would they cost?"
    ),
    "quota": (
        "Our ML team needs to scale to 200 ml.g6.2xlarge instances for inference "
        "in us-west-2. Check availability, pricing, and whether our quota supports "
        "this. What's the monthly cost projection?"
    ),
    "pricing": (
        "Compare SageMaker hosting prices for ml.g5.2xlarge across us-east-1, "
        "us-west-2, and sa-east-1. What's the regional price difference?"
    ),
    "ec2_compare": (
        "I'm choosing between SageMaker managed hosting and self-managed EC2 "
        "for serving a 7B model on g6.2xlarge in us-east-1. Compare the hourly costs "
        "and call out when SageMaker overhead is worth it."
    ),
    "large_fleet": (
        "I'm planning 22 g6 instances in af-south-1 for production inference launching "
        "in 2 weeks. Assess capacity risk: availability, Spot Placement Score, quota, "
        "and pricing. What's the verdict?"
    ),
    "model_deploy": (
        "I want to deploy a Llama 3 70B model in eu-west-1 for real-time inference. "
        "Recommend the right instance type, check availability and pricing, and tell "
        "me which GPU chip it'll run on."
    ),
}


def main() -> int:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "feasible"
    if scenario not in SCENARIOS:
        print(f"Unknown scenario '{scenario}'. Choose from: {list(SCENARIOS)}")
        return 1

    user_message = SCENARIOS[scenario]

    agent = CapacityDecisionAgent(verbose=True)
    for tool_spec in ALL_TOOLS:
        agent.register_tool(**tool_spec)

    print("=" * 80)
    print(f"SCENARIO: {scenario}")
    print("=" * 80)
    print(f"\nUSER:\n{user_message}\n")
    print("=" * 80)
    print("Tools registered:", ", ".join(agent.tools.keys()))
    print("Model:", agent.model_id)
    print("=" * 80)

    result = agent.run(user_message)

    print("\n" + "=" * 80)
    print("AGENT OUTPUT:")
    print("=" * 80)
    print(result["final_text"])
    print("\n" + "=" * 80)
    print("STATS:")
    print("=" * 80)
    print(f"  Iterations:    {result['iterations']}")
    print(f"  Stop reason:   {result['stop_reason']}")
    print(f"  Tool calls:    {len(result['tool_calls'])}")
    for name, args, output in result["tool_calls"]:
        ok = output.get("ok", False)
        marker = "✅" if ok else "❌"
        print(f"    {marker} {name}({json.dumps(args, separators=(',', ':'))})")
    usage = result["usage"]
    total = usage["input_tokens"] + usage["output_tokens"]
    print(f"  Tokens: input={usage['input_tokens']}, output={usage['output_tokens']}, total={total}")
    print(f"  (Per-token cost varies by model — see Bedrock pricing for {agent.model_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
