# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Defense-in-depth safety gate for HibernateAI's destructive (hibernate) path.

Review points closed:
  #3  Production protection is too weak  -> deny-list, cost-threshold gate, OU awareness,
                                            fail-safe (unknown => protected), dry-run.
  #9  Configuration drift               -> detect IaC-managed resources and prefer a
                                            reversible stop/start (or opt-in) over terminate.

Everything here is intentionally free of AWS side effects except the thin
``account_is_prod_ou`` / ``list_account_ous`` helpers, so the core decision logic is
unit-testable without moto. The one rule that governs the whole module:

    **When classification is uncertain, the resource is PROTECTED (not terminated).**
"""
from config import load_config

# Tag keys that mark a resource as managed by Infrastructure-as-Code.
IAC_TAG_KEYS = (
    "aws:cloudformation:stack-id",
    "aws:cloudformation:stack-name",
    "aws:cloudformation:logical-id",
)
# Terraform doesn't tag by default, but these are common conventions.
IAC_TAG_KEYS_SOFT = ("terraform", "Terraform", "managed-by", "ManagedBy")


def _tags_to_dict(tags):
    """Normalize the several tag shapes used across services into a flat {key: value}."""
    out = {}
    if isinstance(tags, dict):
        return {str(k): str(v) for k, v in tags.items()}
    for t in (tags or []):
        key = t.get("Key", t.get("key"))
        val = t.get("Value", t.get("value"))
        if key is not None:
            out[str(key)] = "" if val is None else str(val)
    return out


def detect_iac(tags):
    """Return (is_iac, marker) for a resource's tags.

    Hard CloudFormation/StackSet markers win; soft Terraform/managed-by markers are
    reported too so the save path can flag (not necessarily block) them.
    """
    td = _tags_to_dict(tags)
    for k in IAC_TAG_KEYS:
        if k in td and td[k]:
            return True, k
    for k in IAC_TAG_KEYS_SOFT:
        if k in td and td[k]:
            return True, k
    return False, None


def matches_deny_list(*, tags=None, resource_arn=None, account_id=None, ou_ids=None, config=None):
    """Return (denied, reason) if the resource matches any deny-list entry.

    tag_combos match when ALL key/value pairs in the combo are present on the resource
    (case-insensitive on value).
    """
    cfg = config if config is not None else load_config()
    deny = cfg.get("deny_list", {}) or {}
    td = _tags_to_dict(tags)

    if account_id and account_id in (deny.get("account_ids") or []):
        return True, f"account {account_id} is on the deny-list"

    for ou in (ou_ids or []):
        if ou in (deny.get("ou_ids") or []):
            return True, f"OU {ou} is on the deny-list"

    if resource_arn and resource_arn in (deny.get("resource_arns") or []):
        return True, f"resource {resource_arn} is on the deny-list"

    for combo in (deny.get("tag_combos") or []):
        if not combo:
            continue
        ok = True
        for k, v in combo.items():
            if td.get(k, "").lower() != str(v).lower():
                ok = False
                break
        if ok:
            return True, f"tag combo {combo} is on the deny-list"

    return False, None


def classify_environment(tags, *, environment_tag_keys=None, config=None):
    """Return one of 'prod' | 'nonprod' | 'unknown' from tags.

    FAIL-SAFE: absent/empty environment tag => 'unknown' (caller treats as protected).
    """
    cfg = config if config is not None else load_config()
    keys = environment_tag_keys or cfg.get("environment_tag_keys") or ["environment"]
    td = {k.lower(): v for k, v in _tags_to_dict(tags).items()}
    for key in keys:
        val = td.get(key.lower())
        if val:
            v = val.lower()
            if v in ("production", "prod", "prd"):
                return "prod"
            if v in ("dev", "development", "test", "qa", "stage", "staging", "sandbox", "nonprod", "non-prod"):
                return "nonprod"
            return "unknown"  # tagged, but with a value we don't recognize -> be safe
    return "unknown"


def is_protected_failsafe(tags, *, config=None):
    """The fail-safe classification gate for #3.

    Returns (protected, reason). A resource is protected when its environment cannot be
    positively classified as non-prod. This is the deny-by-default rule the reviewer
    asked for, layered ON TOP of the existing allow-by-default _is_protected()/rules.
    """
    env = classify_environment(tags, config=config)
    if env == "prod":
        return True, "environment classified as prod"
    if env == "unknown":
        return True, "environment could not be classified (fail-safe: treated as protected)"
    return False, None


def iac_action(tags, *, config=None):
    """Decide how the destructive path should treat a resource, given IaC ownership.

    Returns (action, note) where action is one of:
      'terminate'  - normal snapshot+terminate is allowed
      'stop_start' - reversible; do NOT snapshot+terminate (avoids IaC drift)
      'skip'       - do not act (opt_in policy, resource not opted in)
    """
    is_iac, marker = detect_iac(tags)
    if not is_iac:
        return "terminate", None
    cfg = config if config is not None else load_config()
    policy = cfg.get("iac_managed_policy", "stop_start")
    if policy == "terminate":
        return "terminate", f"IaC-managed ({marker}) but policy=terminate"
    if policy == "opt_in":
        td = _tags_to_dict(tags)
        if td.get("HibernateAIAllowTerminate", "").lower() in ("true", "yes"):
            return "terminate", f"IaC-managed ({marker}) with explicit opt-in"
        return "skip", f"IaC-managed ({marker}); no HibernateAIAllowTerminate opt-in"
    # default stop_start
    return "stop_start", f"IaC-managed ({marker}); using reversible stop/start to avoid drift"


def exceeds_cost_threshold(hourly_cost, *, config=None):
    """Return (exceeds, monthly_cost, threshold). threshold<=0 disables the gate."""
    cfg = config if config is not None else load_config()
    threshold = float(cfg.get("cost_threshold_monthly_usd", 0.0) or 0.0)
    monthly = float(hourly_cost) * 730.0
    if threshold <= 0:
        return False, monthly, threshold
    return monthly > threshold, monthly, threshold


# ── Thin AWS helpers (Organizations OU lookup) ──────────────────────────────
def list_account_ous(account_id, org_client=None):
    """Best-effort: return the list of parent OU ids for an account. Empty on error."""
    import boto3

    org = org_client or boto3.client("organizations")
    ous = []
    try:
        parents = org.list_parents(ChildId=account_id).get("Parents", [])
        for p in parents:
            if p.get("Type") == "ORGANIZATIONAL_UNIT":
                ous.append(p["Id"])
    except Exception as e:
        print(f"OU lookup warning for {account_id}: {e}")
    return ous


def account_is_prod_ou(account_id, *, config=None, org_client=None):
    """True if the account sits in a configured prod OU (=> recommend-only)."""
    cfg = config if config is not None else load_config()
    prod_ous = set(cfg.get("prod_ou_ids") or [])
    if not prod_ous or not account_id:
        return False
    return bool(prod_ous.intersection(list_account_ous(account_id, org_client=org_client)))
