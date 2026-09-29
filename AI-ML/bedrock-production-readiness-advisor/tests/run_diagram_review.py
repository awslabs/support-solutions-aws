#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Offline test for the architecture-diagram review pipeline.

No AWS credentials and no model call: the `invoke_fn` seam in
bedrock_readiness.core.diagram_review.review() is given a stubbed response, so
this exercises diagram loading, JSON parsing, the three-layer scope filter,
finding construction, design-vs-reality reconciliation, and report rendering.

The stub response deliberately misbehaves the way a real model might -- it
returns security findings, a guardrails finding, an inline CloudFormation
template, out-of-range risk numbers, a malformed finding, and an unknown
capability key. The test asserts every one of those is handled, because the
scope boundary must not depend on the model cooperating.

Usage:
  python tests/run_diagram_review.py
  python tests/run_diagram_review.py --html      # also write an HTML report
"""

import base64
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bedrock_readiness.core import diagram_review
from bedrock_readiness.core.diagram_review import DiagramReviewError
from bedrock_readiness.core.models import (
    Config,
    CheckStatus,
    FindingSource,
    READINESS_CAPABILITIES,
)
from bedrock_readiness.core.reporter import generate_html_report
from bedrock_readiness.core.scorer import summarize_by_severity
from bedrock_readiness.modules import PILLAR_MODULES

from run_fixtures import MockScanner

# 1x1 PNG -- valid image bytes so loading is realistic. Content is irrelevant
# because the model call is stubbed.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# A deliberately badly-behaved model response.
STUB_RESPONSE = json.dumps({
    "workload_summary": "A RAG pipeline: API Gateway to Lambda, Bedrock Knowledge Base "
                        "over OpenSearch Serverless, invoking Claude for generation.",
    "workload_type": "rag-pipeline",
    "capabilities": [
        {"capability": "invocation_logging", "status": "PRESENT",
         "evidence": "Logs arrow from Bedrock to CloudWatch Logs"},
        {"capability": "genai_dashboard", "status": "ABSENT",
         "evidence": "No dashboard shown"},
        {"capability": "cross_region_inference", "status": "PRESENT",
         "evidence": "Second region drawn with a CRIS profile"},
        {"capability": "async_queue", "status": "ABSENT",
         "evidence": "Synchronous path only"},
        {"capability": "cost_monitoring", "status": "UNCLEAR",
         "evidence": "Cannot tell from the diagram"},
        {"capability": "retry_backoff", "status": "PRESENT",
         "evidence": "Retry annotation on the Lambda-to-Bedrock edge"},
        # Unknown key -- must be ignored, not invented into a capability.
        {"capability": "totally_made_up_capability", "status": "PRESENT",
         "evidence": "should be ignored"},
    ],
    "findings": [
        # --- 3 legitimate, in-scope findings: should survive ---
        {"category": "observability", "title": "No GenAI dashboard",
         "observation": "The diagram shows no CloudWatch dashboard for model metrics.",
         "recommendation": "Add a dashboard covering invocations, latency and token usage.",
         "impact": 3, "likelihood": 3, "fix_type": "config", "effort_minutes": 20},
        {"category": "architecture", "title": "Synchronous path with no buffering",
         "observation": "Requests go straight from the API to Bedrock with no queue.",
         "recommendation": "Introduce an SQS buffer so bursts do not become throttles.",
         "impact": 3, "likelihood": 3, "fix_type": "architecture", "effort_minutes": 480},
        {"category": "cost_optimization", "title": "No token cost monitoring shown",
         "observation": "No budget or token-consumption alerting appears in the diagram.",
         "recommendation": "Add a budget alarm on Bedrock spend.",
         "impact": 2, "likelihood": 3, "fix_type": "config", "effort_minutes": 15},

        # --- Layer 3 drops: disallowed category ---
        {"category": "security", "title": "No PrivateLink endpoint",
         "observation": "Bedrock is reached over the public endpoint.",
         "recommendation": "Add a VPC interface endpoint.",
         "impact": 4, "likelihood": 3},
        {"category": "data_governance", "title": "Zero Data Retention not enabled",
         "observation": "ZDR is not indicated.",
         "recommendation": "Request ZDR for this account.",
         "impact": 4, "likelihood": 2},

        # --- Layer 3 drops: allowed category, out-of-scope content ---
        {"category": "observability", "title": "Logs are not encrypted",
         "observation": "The log group has no KMS encryption at rest.",
         "recommendation": "Attach a customer managed key to the log group.",
         "impact": 3, "likelihood": 2},
        {"category": "architecture", "title": "No guardrail on the model call",
         "observation": "No Bedrock Guardrail is attached, so prompt injection is unmitigated.",
         "recommendation": "Attach a guardrail with content filters.",
         "impact": 4, "likelihood": 3},
        {"category": "quota_capacity", "title": "IAM role is over-permissioned",
         "observation": "The Lambda execution role appears to use a wildcard action.",
         "recommendation": "Apply least privilege to the IAM policy.",
         "impact": 3, "likelihood": 3},

        # --- Layer 3 drops: unfenced IaC template ---
        {"category": "architecture", "title": "Add the queue with this template",
         "observation": "AWSTemplateFormatVersion: 2010-09-09 Resources: Queue: Type: AWS::SQS::Queue",
         "recommendation": "Deploy the template above.",
         "impact": 2, "likelihood": 2},

        # --- Survives, but fenced code must be stripped from the text ---
        {"category": "quota_capacity", "title": "No quota headroom annotation",
         "observation": "The diagram does not state expected peak RPM.",
         "recommendation": "Declare expected peak RPM. ```yaml\nResources:\n  Foo:\n    Type: AWS::SQS::Queue\n``` Then request an increase.",
         "impact": 3, "likelihood": 4, "fix_type": "config", "effort_minutes": 10},

        # --- Survives, but risk numbers must be clamped into 1..4 ---
        {"category": "model_fitness", "title": "Single model for every step",
         "observation": "One model handles retrieval synthesis and generation.",
         "recommendation": "Use a smaller model for extraction.",
         "impact": 99, "likelihood": -5, "fix_type": "nonsense", "effort_minutes": 999999},

        # --- Malformed: no title -> dropped ---
        {"category": "observability", "title": "",
         "observation": "Something vague.", "recommendation": "", "impact": 2, "likelihood": 2},
    ],
    "out_of_scope_notes": [
        "The S3 bucket may not be encrypted.",
        "No WAF in front of API Gateway.",
    ],
})


def _stub_invoke(session, region, model_id, image_bytes, image_format, user_prompt):
    """Stand-in for bedrock:InvokeModel. Asserts it was handed real inputs."""
    assert image_bytes, "no image bytes were passed to the model"
    assert image_format == "png", f"unexpected image format: {image_format}"
    assert "capability" in user_prompt.lower(), "capability list missing from the prompt"
    return STUB_RESPONSE


class _Check:
    def __init__(self):
        self.failures = []
        self.passes = 0

    def ok(self, condition: bool, label: str, detail: str = ""):
        if condition:
            self.passes += 1
            print(f"  [PASS] {label}")
        else:
            self.failures.append(label)
            print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

    def summary(self) -> int:
        print(f"\n  {self.passes} passed, {len(self.failures)} failed")
        if self.failures:
            print("\n  Failures:")
            for f in self.failures:
                print(f"    - {f}")
            return 1
        return 0


def main():
    generate_html = "--html" in sys.argv
    c = _Check()

    with tempfile.TemporaryDirectory() as tmp:
        png_path = os.path.join(tmp, "architecture.png")
        with open(png_path, "wb") as fp:
            fp.write(_TINY_PNG)

        # --- Diagram loading ---------------------------------------------------
        print("\n== Diagram loading (read-only) ==")
        data, fmt, label = diagram_review.load_diagram(png_path)
        c.ok(data == _TINY_PNG and fmt == "png" and label == png_path,
             "loads a local PNG and maps the format")

        jpg_path = os.path.join(tmp, "d.jpg")
        with open(jpg_path, "wb") as fp:
            fp.write(_TINY_PNG)
        _, jfmt, _ = diagram_review.load_diagram(jpg_path)
        c.ok(jfmt == "jpeg", ".jpg is normalised to the 'jpeg' Converse format")

        for bad, why in [
            (os.path.join(tmp, "notes.txt"), "rejects an unsupported extension"),
            (os.path.join(tmp, "missing.png"), "rejects a nonexistent file"),
            ("s3://bucket-only", "rejects a malformed S3 URI"),
        ]:
            if bad.endswith("notes.txt"):
                with open(bad, "w") as fp:
                    fp.write("not an image")
            try:
                diagram_review.load_diagram(bad)
                c.ok(False, why, "no DiagramReviewError raised")
            except DiagramReviewError:
                c.ok(True, why)

        oversized = os.path.join(tmp, "big.png")
        with open(oversized, "wb") as fp:
            fp.write(b"\x00" * (diagram_review.MAX_DIAGRAM_BYTES + 10))
        try:
            diagram_review.load_diagram(oversized)
            c.ok(False, "rejects an oversized diagram", "no error raised")
        except DiagramReviewError:
            c.ok(True, "rejects an oversized diagram")

        # --- Model selection --------------------------------------------------
        print("\n== Model selection (from the account's own models) ==")
        c.ok(not hasattr(diagram_review, "DEFAULT_DIAGRAM_MODEL_ID"),
             "there is no hardcoded default model")

        def inv(models, profiles=()):
            return {"bedrock": {"foundation_models": list(models),
                                "inference_profiles": list(profiles)}}

        def fm(model_id, image=True):
            return {"modelId": model_id,
                    "inputModalities": ["TEXT", "IMAGE"] if image else ["TEXT"],
                    "outputModalities": ["TEXT"]}

        c.ok(diagram_review.select_diagram_model(inv([])) is None,
             "no models available -> no choice (caller skips)")
        c.ok(diagram_review.select_diagram_model(inv([fm("amazon.nova-micro-v1:0", image=False)])) is None,
             "text-only models are not eligible")
        c.ok(diagram_review.select_diagram_model(None) is None,
             "missing inventory -> no choice")

        pick = diagram_review.select_diagram_model(inv([
            fm("anthropic.claude-3-opus-20240229-v1:0"),
            fm("amazon.nova-lite-v1:0"),
            fm("anthropic.claude-3-5-sonnet-20241022-v2:0"),
        ]))
        c.ok(pick is not None and pick.base_id == "amazon.nova-lite-v1:0",
             "picks the cheapest image-capable model",
             pick.base_id if pick else "none")
        c.ok(pick is not None and pick.via == "direct",
             "no matching profile -> direct invocation")

        pick2 = diagram_review.select_diagram_model(inv(
            [fm("anthropic.claude-3-opus-20240229-v1:0"),
             fm("anthropic.claude-3-haiku-20240307-v1:0")],
        ))
        c.ok(pick2.base_id == "anthropic.claude-3-haiku-20240307-v1:0",
             "prefers Haiku over Opus", pick2.base_id)

        # Unknown models must not outrank known-cheap ones.
        pick3 = diagram_review.select_diagram_model(inv([
            fm("vendor.brand-new-enormous-model-v9:0"),
            fm("amazon.nova-lite-v1:0"),
        ]))
        c.ok(pick3.base_id == "amazon.nova-lite-v1:0",
             "an unrecognised model does not win by default", pick3.base_id)
        c.ok(diagram_review.select_diagram_model(
                 inv([fm("vendor.brand-new-enormous-model-v9:0")])).base_id
             == "vendor.brand-new-enormous-model-v9:0",
             "an unrecognised model is still usable when it is all there is")

        # Inference profiles: global CRIS beats geographic, both beat direct.
        base = "amazon.nova-lite-v1:0"
        geo = {"inferenceProfileId": "us." + base,
               "models": [{"modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{base}"}]}
        glob = {"inferenceProfileId": "global." + base,
                "models": [{"modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{base}"}]}
        other = {"inferenceProfileId": "us.anthropic.claude-3-opus-20240229-v1:0",
                 "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-opus-20240229-v1:0"}]}

        p_geo = diagram_review.select_diagram_model(inv([fm(base)], [geo]))
        c.ok(p_geo.invoke_id == "us." + base and p_geo.via == "geo-cris",
             "uses a geographic CRIS profile when one covers the model",
             f"{p_geo.invoke_id} / {p_geo.via}")
        p_glob = diagram_review.select_diagram_model(inv([fm(base)], [geo, glob]))
        c.ok(p_glob.invoke_id == "global." + base and p_glob.via == "global-cris",
             "prefers global CRIS over geographic", f"{p_glob.invoke_id} / {p_glob.via}")
        p_none = diagram_review.select_diagram_model(inv([fm(base)], [other]))
        c.ok(p_none.via == "direct",
             "a profile for a different model is not used", p_none.via)

        ovr = diagram_review.select_diagram_model(inv([]), model_id_override="my.model-v1:0")
        c.ok(ovr is not None and ovr.via == "override" and ovr.invoke_id == "my.model-v1:0",
             "an explicit override is honoured even with no inventory")

        c.ok(diagram_review.base_model_id("us.anthropic.claude-x") == "anthropic.claude-x",
             "CRIS prefixes are stripped when comparing model IDs")
        c.ok(diagram_review.base_model_id("anthropic.claude-x") == "anthropic.claude-x",
             "a plain model ID is unchanged")

        # --- Skip behaviour, never retried ------------------------------------
        print("\n== Skip behaviour (no retry) ==")
        try:
            diagram_review.review(diagram=png_path, session=None, region="eu-west-1",
                                  invoke_fn=_stub_invoke, scan_data=inv([]))
            c.ok(False, "skips when no image-capable model is available")
        except diagram_review.DiagramReviewSkipped as e:
            c.ok("image input" in str(e), "skips when no image-capable model is available")
            c.ok("eu-west-1" in str(e), "the skip reason names the region")
            c.ok("needs no model" in str(e),
                 "the skip reason says the readiness assessment is unaffected")

        attempts = {"n": 0}

        def _failing_invoke(session, region, model_id, image_bytes, image_format, prompt):
            attempts["n"] += 1
            err = Exception("An error occurred (AccessDeniedException) when calling Converse")
            err.response = {"Error": {"Code": "AccessDeniedException"}}
            raise err

        good_inv = inv([fm("amazon.nova-lite-v1:0")])
        try:
            diagram_review.review(diagram=png_path, session=None, region="us-east-1",
                                  invoke_fn=_failing_invoke, scan_data=good_inv)
            c.ok(False, "a failed model call raises Skipped")
        except diagram_review.DiagramReviewSkipped as e:
            c.ok(True, "a failed model call raises Skipped")
            c.ok("access denied" in str(e).lower(), "AccessDenied is classified", str(e)[:70])
        c.ok(attempts["n"] == 1, "the model is invoked exactly once, never retried",
             f"{attempts['n']} attempts")

        def _garbage_invoke(session, region, model_id, image_bytes, image_format, prompt):
            return "I'm afraid I can't help with that."

        try:
            diagram_review.review(diagram=png_path, session=None, region="us-east-1",
                                  invoke_fn=_garbage_invoke, scan_data=good_inv)
            c.ok(False, "an unparseable response is a skip, not a crash")
        except diagram_review.DiagramReviewSkipped:
            c.ok(True, "an unparseable response is a skip, not a crash")

        # A bad diagram is caller input, so it stays a hard error.
        try:
            diagram_review.review(diagram=os.path.join(tmp, "nope.png"), session=None,
                                  invoke_fn=_stub_invoke, scan_data=good_inv)
            c.ok(False, "a missing diagram is still a DiagramReviewError")
        except DiagramReviewError:
            c.ok(True, "a missing diagram is still a DiagramReviewError")
        except diagram_review.DiagramReviewSkipped:
            c.ok(False, "a missing diagram is still a DiagramReviewError",
                 "raised Skipped instead")

        # --- Review + scope filter --------------------------------------------
        print("\n== Review and scope filter ==")
        dr = diagram_review.review(
            diagram=png_path, session=None, region="us-east-1",
            workload_type="rag-pipeline", invoke_fn=_stub_invoke,
            scan_data=inv([fm("amazon.nova-lite-v1:0")], [glob]),
        )
        c.ok(dr.model_id == "global.amazon.nova-lite-v1:0",
             "the review records the model it actually used", dr.model_id)
        c.ok(dr.model_via == "global-cris", "the review records how it was reached")

        kept_ids = [f.check_id for f in dr.findings]
        blob = " ".join(
            f"{f.check_name} {f.message} {f.recommendation}" for f in dr.findings
        ).lower()

        c.ok(len(dr.findings) == 5,
             "keeps exactly the 5 in-scope findings",
             f"kept {len(dr.findings)}: {kept_ids}")
        c.ok(dr.dropped_finding_count == 7,
             "drops the 7 out-of-scope/malformed findings",
             f"dropped {dr.dropped_finding_count}")
        c.ok(dr.out_of_scope_note_count == 2,
             "counts the 2 out-of-scope notes")

        for term, label in [
            ("privatelink", "no PrivateLink content survives"),
            ("zero data retention", "no ZDR content survives"),
            ("kms", "no KMS/encryption content survives"),
            ("encrypt", "no encryption wording survives"),
            ("guardrail", "no guardrails content survives"),
            ("prompt injection", "no prompt-injection content survives"),
            ("least privilege", "no IAM least-privilege content survives"),
            ("waf", "discarded notes never reach the findings"),
            ("awstemplateformatversion", "no CloudFormation template survives"),
            ("aws::sqs::queue", "no CFN resource type survives fence stripping"),
            ("```", "no code fences survive"),
        ]:
            c.ok(term not in blob, label)

        c.ok(all(f.source == FindingSource.DESIGN for f in dr.findings),
             "every design finding is tagged source=DESIGN")
        c.ok(all(f.status == CheckStatus.WARN for f in dr.findings),
             "design findings are WARN, never a hard FAIL")
        c.ok(all(1 <= f.impact <= 4 and 1 <= f.likelihood <= 4 for f in dr.findings),
             "impact/likelihood are clamped to 1..4",
             str([(f.impact, f.likelihood) for f in dr.findings]))
        c.ok(all(f.effort_minutes <= 2880 for f in dr.findings),
             "effort_minutes is clamped")
        c.ok(all(cid.startswith("DGM-") for cid in kept_ids),
             "design check IDs are namespaced DGM-*", str(kept_ids))
        c.ok(len(set(kept_ids)) == len(kept_ids),
             "design check IDs are unique", str(kept_ids))

        # --- Capabilities ------------------------------------------------------
        print("\n== Capability statuses ==")
        c.ok(set(dr.capability_statuses) == set(READINESS_CAPABILITIES),
             "reports a status for every known capability, and only those")
        c.ok("totally_made_up_capability" not in dr.capability_statuses,
             "ignores an unknown capability key")
        c.ok(dr.capability_statuses["invocation_logging"] == "PRESENT",
             "carries PRESENT through")
        c.ok(dr.capability_statuses["genai_dashboard"] == "ABSENT",
             "carries ABSENT through")
        c.ok(dr.capability_statuses["provisioned_capacity"] == "UNCLEAR",
             "defaults unreported capabilities to UNCLEAR")

        # --- Reconciliation ---------------------------------------------------
        print("\n== Design vs. reality reconciliation ==")
        fixture_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "rag_pipeline_partial.json"
        )
        with open(fixture_path) as fp:
            fixture = json.load(fp)

        config = Config(mode="pre-production", workload_type="rag-pipeline",
                        regions=[fixture["region"]],
                        production_estimate={"peak_rpm": 200})
        scanner = MockScanner(fixture)
        pillar_results = [m.assess(scanner, fixture, config) for m in PILLAR_MODULES.values()]

        rows = diagram_review.reconcile(dr.capability_statuses, pillar_results)
        by_cap = {r["capability"]: r for r in rows}
        verdicts = {r["verdict"] for r in rows}

        from bedrock_readiness.core.models import RECONCILIATION_VERDICTS

        c.ok(bool(rows), "produces reconciliation rows")
        c.ok(verdicts <= set(RECONCILIATION_VERDICTS),
             "only documented verdicts are emitted", str(verdicts))
        c.ok(all(r["design_status"] in ("PRESENT", "ABSENT", "UNCLEAR") for r in rows),
             "design_status values are valid")
        c.ok(all(r["runtime_status"] in ("PRESENT", "ABSENT", "UNKNOWN", "NOT_ASSESSED")
                 for r in rows),
             "runtime_status values are valid")

        # Verify the verdict matrix directly -- independent of any fixture, so
        # this cannot silently pass because a fixture happened to change.
        matrix = {
            ("PRESENT", "PRESENT"): "ALIGNED",
            ("PRESENT", "ABSENT"): "DESIGN_NOT_IMPLEMENTED",
            ("ABSENT", "PRESENT"): "UNDOCUMENTED_IN_DESIGN",
            ("ABSENT", "ABSENT"): "MISSING_IN_BOTH",
            ("UNCLEAR", "PRESENT"): "UNDOCUMENTED_IN_DESIGN",
            ("UNCLEAR", "ABSENT"): "ABSENT_IN_ACCOUNT",
            ("PRESENT", "NOT_ASSESSED"): "INCONCLUSIVE",
            ("ABSENT", "UNKNOWN"): "INCONCLUSIVE",
            ("UNCLEAR", "NOT_ASSESSED"): "INCONCLUSIVE",
        }
        wrong = {
            pair: (expected, diagram_review._verdict_for(*pair))
            for pair, expected in matrix.items()
            if diagram_review._verdict_for(*pair) != expected
        }
        c.ok(not wrong, "verdict matrix is correct for all 9 status pairs", str(wrong))

        # An account gap proven by the scan must not be labelled INCONCLUSIVE
        # just because the diagram was ambiguous -- that would bury a real,
        # often high-severity finding behind a label meaning "nothing to see".
        c.ok(diagram_review._verdict_for("UNCLEAR", "ABSENT") == "ABSENT_IN_ACCOUNT",
             "a scan-proven gap is not downgraded to INCONCLUSIVE")

        # Each row's verdict must match the matrix for its own status pair.
        inconsistent = [
            (r["capability"], r["verdict"])
            for r in rows
            if r["verdict"] != diagram_review._verdict_for(
                r["design_status"], r["runtime_status"])
        ]
        c.ok(not inconsistent,
             "every row's verdict matches its status pair", str(inconsistent))

        # The stub declares cross_region_inference PRESENT; this fixture has no
        # cross-region setup, so at least one drift row must exist.
        drift = [r for r in rows if r["verdict"] == "DESIGN_NOT_IMPLEMENTED"]
        c.ok(bool(drift),
             "flags drift where the diagram promises what the account lacks",
             f"verdicts present: {sorted(verdicts)}")
        c.ok(all(r["design_status"] == "PRESENT" and r["runtime_status"] == "ABSENT"
                 for r in drift),
             "drift rows are exactly design=PRESENT + account=ABSENT")
        c.ok(all(r["runtime_checks"] for r in drift),
             "drift rows cite the runtime check IDs behind the verdict",
             str([(r["capability"], r["runtime_checks"]) for r in drift]))

        c.ok(all(r["pillar"] for r in rows), "every row names a pillar")
        c.ok(rows[0]["verdict"] == "DESIGN_NOT_IMPLEMENTED",
             "the most actionable rows sort to the top",
             f"first row is {rows[0]['verdict']}")
        c.ok(rows[-1]["verdict"] == "INCONCLUSIVE",
             "unknowable rows sort to the bottom",
             f"last row is {rows[-1]['verdict']}")

        no_sec = " ".join(f"{r['label']} {r['capability']}" for r in rows).lower()
        c.ok(not any(t in no_sec for t in
                     ("encrypt", "iam", "guardrail", "retention", "privatelink")),
             "no reconciled capability implies a security verdict")

        # --- Full API + report rendering --------------------------------------
        print("\n== Report rendering ==")
        design_pillars = dr.as_pillar_results()
        c.ok(bool(design_pillars), "groups design findings into pillar containers")
        c.ok(all(p.pillar_name.endswith("(design)") for p in design_pillars),
             "design pillar containers are labelled as design")

        from bedrock_readiness.core.models import AssessmentResult
        design_only = AssessmentResult(
            account_id="offline-stub", region="us-east-1", mode="design-review",
            workload_type="rag-pipeline", pillar_results=[], overall_score=0,
            score_applicable=False,
            severity_summary=summarize_by_severity(design_pillars),
            design_summary=dr.design_summary, design_findings=dr.findings,
            out_of_scope_note_count=dr.out_of_scope_note_count,
            dropped_finding_count=dr.dropped_finding_count,
            diagram_source=dr.diagram_source, diagram_model_id=dr.model_id,
        )
        html_design = generate_html_report(design_only)
        c.ok("Design review -- not scored" in html_design,
             "design-only HTML shows no score badge")
        c.ok("all checks passed" not in html_design,
             "design-only HTML does not claim all checks passed")
        c.ok("Design Review (from the diagram)" in html_design,
             "design-only HTML renders the design section")
        c.ok("<script" not in html_design.lower(),
             "model text cannot inject script tags into the report")

        combined = AssessmentResult(
            account_id=fixture["account_id"], region=fixture["region"],
            mode="pre-production", workload_type="rag-pipeline",
            pillar_results=pillar_results, overall_score=55,
            severity_summary=summarize_by_severity(pillar_results),
            design_summary=dr.design_summary, design_findings=dr.findings,
            reconciliation=rows, diagram_source=dr.diagram_source,
            diagram_model_id=dr.model_id,
            out_of_scope_note_count=dr.out_of_scope_note_count,
            dropped_finding_count=dr.dropped_finding_count,
        )
        html_combined = generate_html_report(combined)
        c.ok("Design vs. Reality" in html_combined,
             "combined HTML renders the reconciliation table")
        c.ok("Pillar Breakdown" in html_combined,
             "combined HTML still renders the runtime pillar breakdown")
        c.ok("not for production use" in html_combined.lower(),
             "report carries the non-production disclaimer")
        normalized = re.sub(r"\s+", " ", html_combined)
        c.ok("never a deployable policy, guardrail configuration, or template"
             in normalized,
             "report states the no-deployable-remediation note once, as a note banner")
        c.ok(normalized.count("never a deployable") <= 1,
             "the no-deployable-remediation point is not repeated defensively through the body",
             f"appears {normalized.count('never a deployable')} times")
        c.ok("Model Fitness" in html_combined and "Observability" in html_combined,
             "header states what the assessment does cover")

        # Sidebar navigation
        c.ok('class="toc"' in html_combined, "renders the sidebar nav")
        for anchor, label in [("#priority", "Priority Actions"),
                              ("#reconciliation", "Design vs. Reality"),
                              ("#design-review", "Design Review"),
                              ("#summary", "Pillar Breakdown"),
                              ("#pillars", "Detailed Findings")]:
            c.ok(f'href="{anchor}"' in html_combined and label in html_combined,
                 f"nav links to {label}")
        c.ok('id="pillar-observability"' in html_combined,
             "pillar sections carry anchors for nav sub-links")
        c.ok('href="#pillar-quota_capacity"' in html_combined,
             "nav includes per-pillar sub-links")
        c.ok('href="#reconciliation"' not in html_design,
             "design-only nav omits sections it does not render")

        if generate_html:
            out_dir = os.path.dirname(os.path.abspath(__file__))
            for name, html in [("output_diagram_only.html", html_design),
                               ("output_diagram_combined.html", html_combined)]:
                path = os.path.join(out_dir, name)
                with open(path, "w") as fp:
                    fp.write(html)
                print(f"  wrote {path}")

    print("\n" + "=" * 70)
    return c.summary()


if __name__ == "__main__":
    print("=" * 70)
    print("  Diagram review -- offline pipeline and scope-filter test")
    print("  No AWS credentials, no model invocation (stubbed invoke_fn)")
    print("=" * 70)
    sys.exit(main())
