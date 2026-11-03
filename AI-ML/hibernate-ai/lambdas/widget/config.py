# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Typed configuration accessor for HibernateAI.

All runtime configuration lives in the DynamoDB state table under a single reserved
partition key ``__config__`` (following the existing ``__rules__`` / ``__snooze__`` /
``__retry__`` convention). This module is the ONE place that reads/writes it, so
callers never scatter ``get_item`` calls.

Design rules (shared conventions):
  * Backward compatible: a MISSING config key falls back to today's behavior. An empty
    or absent ``__config__`` item MUST leave the tool behaving exactly as before this
    change (e.g. grouping by ``project_name``, no deny-list, no threshold gate).
  * Fail-safe: if the config read itself fails, we return safe defaults rather than
    raising into a destructive code path.

The config document is a flat dict, e.g.::

    {
      "deny_list": {
        "account_ids":   ["111122223333"],
        "ou_ids":        ["ou-abcd-1234"],
        "resource_arns": ["arn:aws:rds:...:db:prod-db"],
        "tag_combos":    [{"Environment": "prod"}, {"tier": "critical"}]
      },
      "cost_threshold_monthly_usd": 500.0,     # 0/absent => gate disabled
      "prod_ou_ids": ["ou-abcd-prod01"],       # accounts here are recommend-only
      "approvers": ["arn:aws:sso:::group/prod-approvers"],
      "grouping_tag_keys": ["project_name"],
      "environment_tag_keys": ["environment"],
      "iac_managed_policy": "stop_start"       # stop_start | opt_in | terminate
    }
"""
import os
import boto3

CONFIG_KEY = "__config__"

# Defaults chosen so that an empty config == today's behavior.
_DEFAULTS = {
    "deny_list": {
        "account_ids": [],
        "ou_ids": [],
        "resource_arns": [],
        "tag_combos": [],
    },
    "cost_threshold_monthly_usd": 0.0,  # 0 => threshold gate disabled
    "prod_ou_ids": [],
    "approvers": [],
    "grouping_tag_keys": ["project_name"],
    "environment_tag_keys": ["environment"],
    # How to treat IaC-managed resources on the destructive path:
    #   "stop_start" (default, reversible) | "opt_in" (skip unless explicitly allowed)
    #   | "terminate" (legacy behavior; not recommended)
    "iac_managed_policy": "stop_start",
    # Enabled member accounts for multi-account discovery. Empty => single
    # account / Org fallback.
    "accounts": [],
}


def _table():
    return boto3.resource("dynamodb").Table(os.environ.get("STATE_TABLE", "HibernateAIState"))


def _merge_defaults(doc):
    """Return a config dict with every default key present (deep for deny_list)."""
    merged = dict(_DEFAULTS)
    merged["deny_list"] = dict(_DEFAULTS["deny_list"])
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == "deny_list" and isinstance(v, dict):
                merged["deny_list"] = {**_DEFAULTS["deny_list"], **v}
            else:
                merged[k] = v
    return merged


def load_config():
    """Load the full config document, merged over defaults. Fail-safe: on any error
    returns pure defaults (today's behavior) rather than raising."""
    try:
        r = _table().get_item(Key={"project_name": CONFIG_KEY})
        doc = r.get("Item", {}).get("state", {})
    except Exception as e:  # never let a config read break a destructive path
        print(f"config load warning (using defaults): {e}")
        doc = {}
    return _merge_defaults(doc)


def get(key, default=None):
    """Get a single config value, falling back to the module default then ``default``."""
    cfg = load_config()
    if key in cfg:
        return cfg[key]
    return default


def save_config(doc):
    """Persist the full config document (overwrites). Callers should load, mutate, save."""
    import json
    from datetime import datetime
    from decimal import Decimal

    clean = _merge_defaults(doc)
    clean["updated"] = datetime.utcnow().isoformat()

    # A prior load returns numbers as Decimal. json.dumps(default=str) would stringify
    # them ("300" instead of 300), corrupting types across save/load cycles — so convert
    # Decimal to int/float in the encoder, then re-parse floats back to Decimal for
    # DynamoDB (which rejects float).
    def _enc(o):
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return str(o)

    item = json.loads(json.dumps(clean, default=_enc), parse_float=Decimal)
    _table().put_item(Item={"project_name": CONFIG_KEY, "state": item})
    return clean


# ── Tag resolution (flexible tags + dynamic grouping) ──
def _tag_value(tags, key):
    """Case-insensitive tag lookup across the several tag shapes used in the codebase."""
    if isinstance(tags, dict):
        # dict form (e.g. {"project_name": "x"}) or {key: value}
        for k, v in tags.items():
            if str(k).lower() == key.lower():
                return v
        return None
    for t in (tags or []):
        k = t.get("Key", t.get("key"))
        if k is not None and str(k).lower() == key.lower():
            return t.get("Value", t.get("value"))
    return None


def resolve_group_key(tags, config=None):
    """Resolve a resource's grouping value using configured, ordered fallback keys.

    #2 dynamic grouping: if ``grouping_dimension`` is set it takes priority; otherwise we
    walk ``grouping_tag_keys`` (default ["project_name"]) and use the first present tag.
    Falls back to ``__untagged__`` when none match. With empty config this is identical
    to today's ``get_tag(tags, "project_name") or "__untagged__"``.
    """
    cfg = config if config is not None else load_config()
    keys = []
    dim = cfg.get("grouping_dimension")
    if dim:
        keys.append(dim)
    keys.extend(cfg.get("grouping_tag_keys") or ["project_name"])
    seen = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        val = _tag_value(tags, key)
        if val:
            return str(val)
    return "__untagged__"


def resolve_environment(tags, config=None):
    """Return the environment tag value using ``environment_tag_keys`` fallback order,
    or None if none present."""
    cfg = config if config is not None else load_config()
    for key in (cfg.get("environment_tag_keys") or ["environment"]):
        val = _tag_value(tags, key)
        if val:
            return str(val)
    return None


def effective_grouping_key(config=None):
    """The single tag key the save-path AWS-side filter should match on.

    Discovery can fall back across several keys, but the ``tag:<key>`` server-side filter
    used by _find_* takes one key: the configured ``grouping_dimension`` if set, else the
    first of ``grouping_tag_keys``, else ``project_name`` (today's behavior)."""
    cfg = config if config is not None else load_config()
    dim = cfg.get("grouping_dimension")
    if dim:
        return dim
    keys = cfg.get("grouping_tag_keys") or ["project_name"]
    return keys[0] if keys else "project_name"
