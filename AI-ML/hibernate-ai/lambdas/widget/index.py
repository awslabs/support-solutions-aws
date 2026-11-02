# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CloudWatch Custom Widget - Hibernate AI with Bedrock recommendations."""
import json
import os
import time
import html
import boto3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from advisor import analyze_projects, process_natural_language
from rules import evaluate_resource, get_rules, add_rule, remove_rule, render_rules_html
from multi_account import get_cross_account_session, discover_all_accounts, list_org_accounts, get_cross_account_cfn_template, save_account_config, deploy_stackset_to_ous, aggregate_all_accounts
from cost_transparency import render_cost_transparency_html, calculate_tool_costs, log_savings
from onboarding import render_onboarding_html
import config as hib_config
import safety
import approvals


# ── Extended-service discovery (read-only) ───────────────────────────────────
# HibernateAI v1 is advisory-only. The destructive save/restore implementations for
# the 11 extended services used to live in a separate extended-services module, which
# has been removed from this read-only branch. The read-only DISCOVERY helpers below are
# the only part that advisory mode needs — they are called by discover_projects() to
# surface idle extended-service resources on the dashboard. They issue describe/list
# calls ONLY and perform no hibernate/terminate/delete action.
#
# These helpers use the extended-service tag conventions (grouping tag "project_name",
# exemption tag "hibernate_exempt=true"), which differ from index.py's own get_tag/
# _is_protected — so they carry their own local helpers to preserve exact behavior.
def _ext_get_tag(tags, key):
    """Extract a tag value from a list-of-{Key,Value} tags (extended-service shape)."""
    if not tags:
        return None
    for t in tags:
        if t.get("Key") == key:
            return t.get("Value")
    return None


def _ext_is_protected(tags):
    """Extended-service exemption: resource opts out of hibernation via hibernate_exempt."""
    return _ext_get_tag(tags, "hibernate_exempt") == "true"


def _find_redshift(tag_filter):
    """Discover Redshift clusters by project_name tag (read-only)."""
    client = boto3.client("redshift")
    out = []
    for page in client.get_paginator("describe_clusters").paginate():
        for cl in page.get("Clusters", []):
            tags = cl.get("Tags", [])
            if _ext_is_protected(tags):
                continue
            proj = _ext_get_tag(tags, "project_name")
            if tag_filter and proj != tag_filter:
                continue
            if tag_filter is None and proj is not None:
                continue
            out.append(cl)
    return out


def _find_opensearch(tag_filter):
    """Discover OpenSearch domains by project_name tag (read-only)."""
    client = boto3.client("opensearch")
    out = []
    for d in client.list_domain_names().get("DomainNames", []):
        name = d["DomainName"]
        arn = f"arn:aws:es:{boto3.session.Session().region_name}:{boto3.client('sts').get_caller_identity()['Account']}:domain/{name}"
        tags = client.list_tags(ARN=arn).get("TagList", [])
        if _ext_is_protected(tags):
            continue
        proj = _ext_get_tag(tags, "project_name")
        if tag_filter and proj != tag_filter:
            continue
        if tag_filter is None and proj is not None:
            continue
        desc = client.describe_domain(DomainName=name)["DomainStatus"]
        out.append({"DomainName": name, "Tags": tags, "Config": desc})
    return out


def _find_elasticache(tag_filter):
    """Discover ElastiCache clusters by project_name tag (read-only)."""
    client = boto3.client("elasticache")
    out = []
    for page in client.get_paginator("describe_cache_clusters").paginate(ShowCacheNodeInfo=True):
        for cl in page.get("CacheClusters", []):
            tags = client.list_tags_for_resource(ResourceName=cl["ARN"]).get("TagList", [])
            if _ext_is_protected(tags):
                continue
            proj = _ext_get_tag(tags, "project_name")
            if tag_filter and proj != tag_filter:
                continue
            if tag_filter is None and proj is not None:
                continue
            cl["_tags"] = tags
            out.append(cl)
    return out


def _find_sagemaker(tag_filter):
    """Discover SageMaker endpoints by project_name tag (read-only)."""
    client = boto3.client("sagemaker")
    out = []
    for page in client.get_paginator("list_endpoints").paginate(StatusEquals="InService"):
        for ep in page.get("Endpoints", []):
            tags = client.list_tags(ResourceArn=ep["EndpointArn"]).get("Tags", [])
            if _ext_is_protected(tags):
                continue
            proj = _ext_get_tag(tags, "project_name")
            if tag_filter and proj != tag_filter:
                continue
            if tag_filter is None and proj is not None:
                continue
            desc = client.describe_endpoint(EndpointName=ep["EndpointName"])
            out.append({"EndpointName": ep["EndpointName"], "Tags": tags, "Config": desc})
    return out


def _find_emr(tag_filter):
    """Discover EMR clusters by project_name tag (read-only)."""
    client = boto3.client("emr")
    out = []
    for page in client.get_paginator("list_clusters").paginate(ClusterStates=["WAITING", "RUNNING"]):
        for cl in page.get("Clusters", []):
            desc = client.describe_cluster(ClusterId=cl["Id"])["Cluster"]
            tags = desc.get("Tags", [])
            if _ext_is_protected(tags):
                continue
            proj = _ext_get_tag(tags, "project_name")
            if tag_filter and proj != tag_filter:
                continue
            if tag_filter is None and proj is not None:
                continue
            out.append(desc)
    return out


def _find_eks_nodegroups(tag_filter):
    """Discover EKS node groups with running nodes by project_name tag (read-only)."""
    client = boto3.client("eks")
    out = []
    for cluster_name in client.list_clusters().get("clusters", []):
        for ng_name in client.list_nodegroups(clusterName=cluster_name).get("nodegroups", []):
            ng = client.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng_name)["nodegroup"]
            tags = ng.get("tags", {})  # EKS uses dict-style tags
            if tags.get("hibernate_exempt") == "true":
                continue
            proj = tags.get("project_name")
            if tag_filter and proj != tag_filter:
                continue
            if tag_filter is None and proj is not None:
                continue
            if ng.get("scalingConfig", {}).get("desiredSize", 0) > 0:
                out.append(ng)
    return out


def _find_nat_gateways(tag_filter):
    """Discover NAT Gateways by project_name tag (read-only)."""
    ec2 = boto3.client("ec2")
    filters = [{"Name": "state", "Values": ["available"]}]
    if tag_filter:
        filters.append({"Name": "tag:project_name", "Values": [tag_filter]})
    out = []
    for page in ec2.get_paginator("describe_nat_gateways").paginate(Filter=filters):
        for ngw in page.get("NatGateways", []):
            tags = ngw.get("Tags", [])
            if _ext_is_protected(tags):
                continue
            if tag_filter is None and _ext_get_tag(tags, "project_name") is not None:
                continue
            out.append(ngw)
    return out


def _find_elbs(tag_filter):
    """Discover ELBs by project_name tag (read-only)."""
    client = boto3.client("elbv2")
    out = []
    for page in client.get_paginator("describe_load_balancers").paginate():
        for lb in page.get("LoadBalancers", []):
            tags = []
            for desc in client.describe_tags(ResourceArns=[lb["LoadBalancerArn"]]).get("TagDescriptions", []):
                tags = desc.get("Tags", [])
            if _ext_is_protected(tags):
                continue
            proj = _ext_get_tag(tags, "project_name")
            if tag_filter and proj != tag_filter:
                continue
            if tag_filter is None and proj is not None:
                continue
            lb["_tags"] = tags
            out.append(lb)
    return out


def _find_fsx(tag_filter):
    """Discover FSx file systems by project_name tag (read-only)."""
    client = boto3.client("fsx")
    out = []
    for page in client.get_paginator("describe_file_systems").paginate():
        for fs in page.get("FileSystems", []):
            tags = fs.get("Tags", [])
            if _ext_is_protected(tags):
                continue
            proj = _ext_get_tag(tags, "project_name")
            if tag_filter and proj != tag_filter:
                continue
            if tag_filter is None and proj is not None:
                continue
            out.append(fs)
    return out


def _find_workspaces(tag_filter):
    """Discover running WorkSpaces by project_name tag (read-only)."""
    client = boto3.client("workspaces")
    out = []
    for page in client.get_paginator("describe_workspaces").paginate():
        for ws in page.get("Workspaces", []):
            tags = client.describe_tags(ResourceId=ws["WorkspaceId"]).get("TagList", [])
            if _ext_is_protected(tags):
                continue
            proj = _ext_get_tag(tags, "project_name")
            if tag_filter and proj != tag_filter:
                continue
            if tag_filter is None and proj is not None:
                continue
            if ws.get("State") in ("AVAILABLE", "IMPAIRED"):
                ws["_tags"] = tags
                out.append(ws)
    return out


def _find_appstream_fleets(tag_filter):
    """Discover running AppStream fleets by project_name tag (read-only)."""
    client = boto3.client("appstream")
    out = []
    try:
        for fleet in client.describe_fleets().get("Fleets", []):
            tags = client.list_tags_for_resource(ResourceArn=fleet["Arn"]).get("Tags", {})
            if tags.get("hibernate_exempt") == "true":  # AppStream uses dict-style tags
                continue
            proj = tags.get("project_name")
            if tag_filter and proj != tag_filter:
                continue
            if tag_filter is None and proj is not None:
                continue
            if fleet.get("State") == "RUNNING":
                fleet["_tags"] = tags
                out.append(fleet)
    except Exception:
        pass
    return out


def esc(v):
    """HTML-escape any externally-sourced value (AWS tag/resource names, project
    names, NLP input, LLM output) before interpolating into an HTML string.
    Prevents stored/reflected XSS in the CloudWatch custom-widget HTML (CSR findings).
    Quotes are escaped too so values are safe inside HTML attributes."""
    return html.escape("" if v is None else str(v), quote=True)


def _enrollment_error_message(exc):
    """If a destructive call failed because the resource isn't enrolled for Hibernate AI
    (IAM denies Terminate/Delete/Stop unless the resource carries HibernateAI=true), return
    a clear, actionable message. Otherwise return the original error text (truncated).

    Path A safety model: the Lambda/cross-account role can only act destructively on
    resources tagged aws:ResourceTag/HibernateAI=true. An un-enrolled resource therefore
    surfaces as AccessDenied/UnauthorizedOperation — which we translate here rather than
    showing a raw IAM error."""
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code", "")
    msg = str(exc)
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation") or \
       "not authorized" in msg.lower() or "UnauthorizedOperation" in msg:
        return ("Not enrolled for Hibernate AI — tag the resource with HibernateAI=true "
                "to allow hibernation (safety control).")
    return msg[:100]


HOURLY_COST = {
    "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416,
    "t3.large": 0.0832, "t4g.micro": 0.0084, "t4g.small": 0.0168,
    "t4g.medium": 0.0336, "m5.large": 0.096, "m6i.large": 0.096,
    "db.t3.micro": 0.017, "db.t3.small": 0.034, "db.t3.medium": 0.068,
    "db.t4g.micro": 0.016, "db.t4g.small": 0.032, "db.t4g.medium": 0.065,
    "db.r5.large": 0.24, "db.r6g.large": 0.218,
}
EBS_GB_HOUR = 0.10 / 730
SELF_ARN = ""

# Production protection — skip resources with these tags
PROTECTED_TAGS = {
    "Environment": ["production", "prod", "prd"],
    "CostSaverProtect": ["true", "yes"],
}


def _is_protected(tags):
    """Return True if resource has production/protected tags."""
    for t in (tags or []):
        key, val = t.get("Key", ""), t.get("Value", "").lower()
        if key in PROTECTED_TAGS and val in PROTECTED_TAGS[key]:
            return True
    return False


def handler(event, context):
    global SELF_ARN
    SELF_ARN = context.invoked_function_arn if context else ""

    # SD: never log the full event — it carries NLP free-text input, approval tokens,
    # feedback verdicts, and project names. Log only non-sensitive metadata. Even in
    # DEBUG we log the set of KEYS present (not their values) plus the action, so deep
    # debugging can see the event shape without exposing user-submitted data to CloudWatch.
    _SENSITIVE_KEYS = {"nlp", "text", "input", "query", "approve_token", "token", "verdict",
                       "feedback", "project", "display"}
    if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
        _safe_keys = sorted(k for k in (event.keys() if isinstance(event, dict) else []))
        print("EVENT action:", event.get("action"),
              "| keys:", [k for k in _safe_keys if k not in _SENSITIVE_KEYS],
              "| redacted:", [k for k in _safe_keys if k in _SENSITIVE_KEYS])
    else:
        print("EVENT action:", event.get("action"))

    # cwdb-action puts params at top level of event, not in widgetContext.params
    action = event.get("action")

    # #5 Role separation: gate privileged (destructive) actions on caller identity.
    if action in approvals.PRIVILEGED_ACTIONS:
        allowed, why = approvals.authorize(action, event)
        if not allowed:
            return json.dumps({"success": False, "error": f"Not authorized: {why}"})
    # ── READ-ONLY v1: destructive actions are disabled. HibernateAI v1 is advisory-only —
    # it analyzes usage and recommends savings, but performs NO hibernate/restore/terminate/
    # schedule actions. (Destructive capability ships separately in a customer-opt-in, security-
    # reviewed add-on.) These routes return a clear message instead of acting.
    _READ_ONLY_DISABLED = {"save", "restore", "set_schedule", "remove_schedule",
                           "prefreeze", "snooze", "approve"}
    if action in _READ_ONLY_DISABLED:
        return json.dumps({
            "success": False,
            "read_only": True,
            "error": ("HibernateAI is running in read-only (advisory) mode. This version "
                      "analyzes idle resources and estimates savings but does not perform "
                      "hibernate, restore, terminate, or scheduling actions. Destructive "
                      "actions are available via a separate, customer-authorized add-on."),
        })

    if action == "analyze":
        return handle_analyze()
    elif action == "nlp":
        return handle_nlp(event.get("input", ""))
    elif action == "set_config":
        return handle_set_config(event)
    elif action == "detect_tags":
        return handle_detect_tags()
    elif action == "feedback":
        return handle_feedback(event)
    elif action == "deploy_stackset":
        return handle_deploy_stackset(event)
    elif action == "add_rule":
        return handle_add_rule(event)
    elif action == "remove_rule":
        return handle_remove_rule(event)
    elif action == "list_accounts":
        return handle_list_accounts()
    elif action == "set_accounts":
        return handle_set_accounts(event)
    return render_dashboard()


def get_tag(tags, key):
    return next((t["Value"] for t in (tags or []) if t["Key"] == key), None)


def discover_projects():
    ec2 = boto3.client("ec2")
    rds = boto3.client("rds")
    groups = {}
    _cfg = hib_config.load_config()  # #1/#2: resolve grouping key via config (fallback: project_name)

    def grp(name):
        if name not in groups:
            groups[name] = {"project_name": name, "display": "Untagged Resources" if name == "__untagged__" else name,
                "ec2": [], "ebs": [], "aurora": [], "rds_standalone": [],
                "redshift": [], "opensearch": [], "elasticache": [], "sagemaker": [],
                "emr": [], "eks_nodegroups": [], "nat_gateways": [], "elbs": [],
                "fsx": [], "workspaces": [], "appstream": [], "hourly_cost": 0.0}
        return groups[name]

    _stopped_vol_proj = {}  # volume_id -> project, for EBS attached to STOPPED instances
    for page in ec2.get_paginator("describe_instances").paginate(Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}]):
        for res in page.get("Reservations", []):
            for inst in res.get("Instances", []):
                if _is_protected(inst.get("Tags")):
                    continue
                proj = hib_config.resolve_group_key(inst.get("Tags"), _cfg)
                g = grp(proj)
                itype = inst["InstanceType"]
                g["ec2"].append({"id": inst["InstanceId"], "name": get_tag(inst.get("Tags"), "Name") or inst["InstanceId"], "type": itype, "state": inst["State"]["Name"]})
                if inst["State"]["Name"] == "running":
                    g["hourly_cost"] += HOURLY_COST.get(itype, 0.05)
                else:
                    # Stopped instances incur NO compute cost, but their attached EBS volumes
                    # keep billing. Track the volume IDs so we can add their storage cost below
                    # (this is the true cost of a stopped instance — and the real savings from
                    # hibernating it, since freeze snapshots+deletes those volumes).
                    for bdm in inst.get("BlockDeviceMappings", []):
                        vid = bdm.get("Ebs", {}).get("VolumeId")
                        if vid:
                            _stopped_vol_proj[vid] = proj

    # Add EBS storage cost for volumes attached to STOPPED instances (they keep billing).
    if _stopped_vol_proj:
        vids = list(_stopped_vol_proj.keys())
        for i in range(0, len(vids), 200):  # DescribeVolumes VolumeIds batch limit
            batch = vids[i:i+200]
            try:
                for vol in ec2.describe_volumes(VolumeIds=batch).get("Volumes", []):
                    proj = _stopped_vol_proj.get(vol["VolumeId"], "__untagged__")
                    grp(proj)["hourly_cost"] += vol["Size"] * EBS_GB_HOUR
            except Exception as e:
                print(f"stopped-EBS cost lookup warning: {e}")

    for page in ec2.get_paginator("describe_volumes").paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
        for vol in page.get("Volumes", []):
            if _is_protected(vol.get("Tags")):
                continue
            proj = hib_config.resolve_group_key(vol.get("Tags"), _cfg)
            g = grp(proj)
            g["ebs"].append({"id": vol["VolumeId"], "name": get_tag(vol.get("Tags"), "Name") or vol["VolumeId"], "size": vol["Size"], "type": vol["VolumeType"]})
            g["hourly_cost"] += vol["Size"] * EBS_GB_HOUR

    cluster_ids = set()
    for page in rds.get_paginator("describe_db_clusters").paginate():
        for cl in page.get("DBClusters", []):
            tags = rds.list_tags_for_resource(ResourceName=cl["DBClusterArn"]).get("TagList", [])
            proj = hib_config.resolve_group_key(tags, _cfg)
            g = grp(proj)
            for m in cl.get("DBClusterMembers", []):
                cluster_ids.add(m["DBInstanceIdentifier"])
            g["aurora"].append({"id": cl["DBClusterIdentifier"], "engine": cl["Engine"], "members": len(cl.get("DBClusterMembers", [])), "status": cl["Status"]})

    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page.get("DBInstances", []):
            tags = rds.list_tags_for_resource(ResourceName=db["DBInstanceArn"]).get("TagList", [])
            proj = hib_config.resolve_group_key(tags, _cfg)
            cost = HOURLY_COST.get(db["DBInstanceClass"], 0.10)
            if db["DBInstanceIdentifier"] in cluster_ids:
                grp(proj)["hourly_cost"] += cost
                continue
            g = grp(proj)
            g["rds_standalone"].append({"id": db["DBInstanceIdentifier"], "engine": db["Engine"], "cls": db["DBInstanceClass"], "status": db["DBInstanceStatus"]})
            g["hourly_cost"] += cost

    # Extended services discovery
    try:
        for cl in _find_redshift(None):
            proj = hib_config.resolve_group_key(cl.get("Tags", []), _cfg)
            grp(proj)["redshift"].append({"id": cl["ClusterIdentifier"], "node_type": cl.get("NodeType", ""), "nodes": cl.get("NumberOfNodes", 1), "status": cl.get("ClusterStatus", "")})
            grp(proj)["hourly_cost"] += 0.25 * cl.get("NumberOfNodes", 1)
    except Exception:
        pass
    try:
        for d in _find_opensearch(None):
            proj = hib_config.resolve_group_key(d.get("Tags", []), _cfg)
            grp(proj)["opensearch"].append({"id": d["DomainName"], "instance_type": d.get("Config", {}).get("ClusterConfig", {}).get("InstanceType", "")})
            grp(proj)["hourly_cost"] += 0.10
    except Exception:
        pass
    try:
        for cl in _find_elasticache(None):
            proj = hib_config.resolve_group_key(cl.get("_tags", []), _cfg)
            grp(proj)["elasticache"].append({"id": cl["CacheClusterId"], "engine": cl.get("Engine", "redis"), "node_type": cl.get("CacheNodeType", "")})
            grp(proj)["hourly_cost"] += 0.05
    except Exception:
        pass
    try:
        for ep in _find_sagemaker(None):
            proj = hib_config.resolve_group_key(ep.get("Tags", []), _cfg)
            grp(proj)["sagemaker"].append({"id": ep["EndpointName"]})
            grp(proj)["hourly_cost"] += 0.20
    except Exception:
        pass
    try:
        for cl in _find_emr(None):
            proj = hib_config.resolve_group_key(cl.get("Tags", []), _cfg)
            grp(proj)["emr"].append({"id": cl["Id"], "name": cl.get("Name", ""), "status": cl.get("Status", {}).get("State", "")})
            grp(proj)["hourly_cost"] += 0.15
    except Exception:
        pass
    try:
        for ng in _find_eks_nodegroups(None):
            proj = hib_config.resolve_group_key(ng.get("tags", {}), _cfg)
            grp(proj)["eks_nodegroups"].append({"id": ng["nodegroupName"], "cluster": ng["clusterName"], "desired": ng.get("scalingConfig", {}).get("desiredSize", 0)})
            grp(proj)["hourly_cost"] += 0.10 * ng.get("scalingConfig", {}).get("desiredSize", 0)
    except Exception:
        pass
    try:
        for ngw in _find_nat_gateways(None):
            proj = hib_config.resolve_group_key(ngw.get("Tags", []), _cfg)
            grp(proj)["nat_gateways"].append({"id": ngw["NatGatewayId"], "subnet": ngw.get("SubnetId", "")})
            grp(proj)["hourly_cost"] += 0.045
    except Exception:
        pass
    try:
        for lb in _find_elbs(None):
            proj = hib_config.resolve_group_key(lb.get("_tags", []), _cfg)
            grp(proj)["elbs"].append({"id": lb["LoadBalancerName"], "type": lb.get("Type", "application")})
            grp(proj)["hourly_cost"] += 0.0225
    except Exception:
        pass
    try:
        for fs in _find_fsx(None):
            proj = hib_config.resolve_group_key(fs.get("Tags", []), _cfg)
            grp(proj)["fsx"].append({"id": fs["FileSystemId"], "type": fs.get("FileSystemType", ""), "capacity": fs.get("StorageCapacity", 0)})
            grp(proj)["hourly_cost"] += 0.10
    except Exception:
        pass
    try:
        for ws in _find_workspaces(None):
            proj = hib_config.resolve_group_key(ws.get("_tags", []), _cfg)
            grp(proj)["workspaces"].append({"id": ws["WorkspaceId"], "user": ws.get("UserName", ""), "state": ws.get("State", "")})
            grp(proj)["hourly_cost"] += 0.05
    except Exception:
        pass
    try:
        for fleet in _find_appstream_fleets(None):
            proj = hib_config.resolve_group_key(fleet.get("_tags", {}), _cfg)
            grp(proj)["appstream"].append({"id": fleet["Name"], "instance_type": fleet.get("InstanceType", "")})
            grp(proj)["hourly_cost"] += 0.10
    except Exception:
        pass

    return dict(sorted(groups.items(), key=lambda x: (x[0] == "__untagged__", x[0])))


# ── DynamoDB State ───────────────────────────────────────────
def _get_table():
    return boto3.resource("dynamodb").Table(os.environ.get("STATE_TABLE", "HibernateAIState"))

def _save_state(project_name, state):
    state["saved_at"] = datetime.utcnow().isoformat()
    # DynamoDB's boto3 resource rejects Python float; round-trip with Decimal so any
    # float in a captured resource config (sizes, costs, placement) serializes cleanly.
    from decimal import Decimal
    item = json.loads(json.dumps({"project_name": project_name, "state": state}, default=str),
                      parse_float=Decimal)
    _get_table().put_item(Item=item)

def _load_state(project_name):
    r = _get_table().get_item(Key={"project_name": project_name})
    return r.get("Item", {}).get("state")

def _list_saved_projects():
    # Exclude reserved control records (__config__, __rules__, __snooze__<token>,
    # __approval__<token>, __retry__<project>). They live in the same table under the
    # "__key__" convention and must NOT render as phantom "0 resources" project cards.
    # __untagged__ is also "__"-prefixed but is handled separately in discovery, so it is
    # correctly excluded here too.
    table = _get_table()
    names, kwargs = [], {"ProjectionExpression": "project_name"}
    while True:  # paginate: a single scan returns at most 1MB; don't silently drop items
        r = table.scan(**kwargs)
        names.extend(item["project_name"] for item in r.get("Items", [])
                     if not item["project_name"].startswith("__"))
        lek = r.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return names


def _get_schedule(project_name):
    """Check if EventBridge rules exist for this project."""
    events_client = boto3.client("events")
    safe_name = project_name.replace(" ", "-").replace("__", "u")[:30]
    print(f"SCHEDULE_LOOKUP: project={project_name} safe_name={safe_name} rule=HibernateAI-Restore-{safe_name}")
    try:
        restore = events_client.describe_rule(Name=f"HibernateAI-Restore-{safe_name}")
        save = events_client.describe_rule(Name=f"HibernateAI-Save-{safe_name}")
        def parse_cron(expr):
            parts = expr.replace("cron(", "").replace(")", "").split()
            return f"{parts[1].zfill(2)}:{parts[0].zfill(2)}"
        result = {
            "restore_at": parse_cron(restore["ScheduleExpression"]),
            "save_at": parse_cron(save["ScheduleExpression"]),
            "state": restore.get("State", "ENABLED"),
        }
        print(f"SCHEDULE_FOUND: {result}")
        return result
    except Exception as e:
        print(f"SCHEDULE_NOT_FOUND: {e}")
        return None


# ── Rendering ────────────────────────────────────────────────
def render_dashboard():
    projects = discover_projects()
    total_cost = sum(p["hourly_cost"] for p in projects.values())
    total_res = sum(len(p["ec2"]) + len(p["ebs"]) + len(p["aurora"]) + len(p["rds_standalone"]) for p in projects.values())

    # Find saved projects in DynamoDB that have no active resources
    saved_projects = _list_saved_projects()
    for sp in saved_projects:
        if sp not in projects:
            projects[sp] = {"project_name": sp, "display": "Untagged Resources" if sp == "__untagged__" else sp,
                "ec2": [], "ebs": [], "aurora": [], "rds_standalone": [], "hourly_cost": 0.0}

    cards = ""
    for name, p in projects.items():
        count = len(p["ec2"]) + len(p["ebs"]) + len(p["aurora"]) + len(p["rds_standalone"])
        has_saved = _load_state(name) is not None
        if count == 0 and not has_saved:
            continue
        cards += render_card(name, p, has_saved=has_saved)

    # NLP input and analyze button
    nlp_params = json.dumps({"action": "nlp", "input": "__INPUT__"})
    analyze_params = json.dumps({"action": "analyze"})

    return f"""
<style>
  .ai-wrap {{ font-family: -apple-system, sans-serif; color: #d5d5d5; }}
  .ai-stats {{ display: flex; gap: 32px; margin-bottom: 12px; padding: 12px; background: #1a1a2e; border-radius: 8px; }}
  .ai-stat-val {{ font-size: 22px; font-weight: 700; color: #4f8ff7; }}
  .ai-stat-lbl {{ font-size: 11px; color: #888; }}
  .ai-controls {{ display: flex; gap: 8px; margin-bottom: 12px; padding: 12px; background: #1a1a2e; border-radius: 8px; align-items: center; }}
  .ai-input {{ flex: 1; background: #0f0f23; color: #d5d5d5; border: 1px solid #2a2d3a; border-radius: 6px; padding: 8px 12px; font-size: 13px; font-family: inherit; }}
  .ai-grid {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .ai-card {{ background: #16213e; border: 1px solid #2a2d3a; border-radius: 8px; width: 360px; overflow: hidden; }}
  .ai-card:hover {{ border-color: #4f8ff7; }}
  .ai-hdr {{ padding: 12px; display: flex; justify-content: space-between; }}
  .ai-title {{ font-size: 14px; font-weight: 600; }}
  .ai-sub {{ font-size: 11px; color: #888; margin-top: 2px; }}
  .ai-cost {{ font-size: 16px; font-weight: 700; color: #fb923c; }}
  .ai-cost-lbl {{ font-size: 10px; color: #888; }}
  .ai-badges {{ padding: 0 12px 8px; display: flex; gap: 6px; flex-wrap: wrap; }}
  .ai-badge {{ font-size: 10px; padding: 2px 6px; border-radius: 3px; background: #0f0f23; }}
  .ai-b-ec2 {{ color: #34d399; border: 1px solid #34d399; }}
  .ai-b-ebs {{ color: #a78bfa; border: 1px solid #a78bfa; }}
  .ai-b-aur {{ color: #4f8ff7; border: 1px solid #4f8ff7; }}
  .ai-b-rds {{ color: #fb923c; border: 1px solid #fb923c; }}
  .ai-res {{ padding: 0 12px 8px; }}
  .ai-r {{ display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #2a2d3a; font-size: 12px; }}
  .ai-r:last-child {{ border: none; }}
  .ai-r-meta {{ color: #888; font-size: 10px; }}
  .ai-state {{ font-size: 10px; padding: 1px 5px; border-radius: 3px; }}
  .ai-running {{ background: rgba(52,211,153,0.15); color: #34d399; }}
  .ai-stopped {{ background: rgba(248,113,113,0.15); color: #f87171; }}
  .ai-available {{ background: rgba(79,143,247,0.15); color: #4f8ff7; }}
  .ai-creating {{ background: rgba(251,146,60,0.15); color: #fb923c; }}
  .ai-acts {{ padding: 8px 12px; border-top: 1px solid #2a2d3a; display: flex; gap: 8px; justify-content: flex-end; }}
  .ai-untagged {{ border-style: dashed; border-color: #fb923c; }}
  a.btn {{ display: inline-block; padding: 4px 12px; border-radius: 4px; font-size: 11px; font-weight: 600; text-decoration: none; cursor: pointer; }}
  a.btn.hibernate {{ background: #f87171; color: #fff; }}
  a.btn.restore {{ background: #34d399; color: #000; }}
  a.btn.analyze {{ background: #a78bfa; color: #fff; }}
  a.btn.nlp {{ background: #4f8ff7; color: #fff; }}
</style>
<div class="ai-wrap">
  <div class="ai-stats">
    <div><div class="ai-stat-val">{len(projects)}</div><div class="ai-stat-lbl">Projects</div></div>
    <div><div class="ai-stat-val">{total_res}</div><div class="ai-stat-lbl">Resources</div></div>
    <div><div class="ai-stat-val">${total_cost:.2f}</div><div class="ai-stat-lbl">Hourly Cost</div></div>
    <div><div class="ai-stat-val">${total_cost * 24:.2f}</div><div class="ai-stat-lbl">Daily Cost</div></div>
    <div><div class="ai-stat-val">${total_cost * 730:.0f}</div><div class="ai-stat-lbl">Monthly Est.</div></div>
  </div>
  <div style="padding:6px 12px;margin-bottom:8px;background:#1a1a2e;border-radius:6px;border-left:3px solid #a78bfa">
    <span style="font-size:12px;color:#a78bfa;font-weight:600">&#128164; Hibernation</span>
    <span style="font-size:11px;color:#888"> = Snapshot resources &amp; delete them when not needed. Restore on demand from saved state.</span>
  </div>
  {render_cost_transparency_html(projects)}
  {render_onboarding_html(projects, SELF_ARN)}
  {render_rules_html(SELF_ARN)}
  <div class="ai-controls">
    <span style="font-size:18px">&#129302;</span>
    <a class="btn analyze">&#128161; AI Analyze All</a>
    <cwdb-action action="call" endpoint="{SELF_ARN}" display="widget">
      {analyze_params}
    </cwdb-action>
  </div>
  <div class="ai-grid">{cards}</div>
</div>"""


def _project_savings(p):
    """Monthly cost scenarios for a project card / tab:
      running_24x7 : left on 24/7 (hourly * 730)
      scheduled    : Mon-Fri 9AM-6PM (9h x ~21.7 weekdays ≈ 195 on-hrs/mo);
                     off-hours pay only snapshot storage
      hibernated   : fully off 24/7 -> snapshot storage only
    """
    hourly = p.get("hourly_cost", 0.0)
    running_24x7 = hourly * 730
    snap = 0.0
    for v in p.get("ebs", []):
        snap += (v.get("size") or 8) * 0.05
    for _ in p.get("aurora", []):
        snap += 1 * 0.095
    for _ in p.get("rds_standalone", []):
        snap += 20 * 0.095
    on_hours = 9 * (52 * 5 / 12)      # ≈195 hrs/mo
    on_frac = on_hours / 730.0
    scheduled = running_24x7 * on_frac + snap * (1 - on_frac)
    return {
        "running_24x7": round(running_24x7, 2),
        "scheduled": round(scheduled, 2),
        "hibernated": round(snap, 2),
        "save_scheduled": round(max(running_24x7 - scheduled, 0), 2),
        "save_hibernated": round(max(running_24x7 - snap, 0), 2),
    }


def render_card(name, p, has_saved=False):
    is_untagged = name == "__untagged__"
    count = len(p["ec2"]) + len(p["ebs"]) + len(p["aurora"]) + len(p["rds_standalone"])
    icon = "&#128230;" if is_untagged else "&#128193;"
    extra = " ai-untagged" if is_untagged else ""

    # Saved but no active resources
    if count == 0 and has_saved:
        restore_params = json.dumps({"action": "restore", "project": name})
        state = _load_state(name) or {}
        s_ec2 = state.get("ec2", [])
        s_ebs = state.get("ebs", [])
        s_aurora = state.get("aurora", [])
        s_rds = state.get("rds", [])
        s_total = len(s_ec2) + len(s_ebs) + len(s_aurora) + len(s_rds)

        badges = ""
        if s_ec2: badges += f'<span class="ai-badge ai-b-ec2">EC2: {len(s_ec2)}</span>'
        if s_ebs: badges += f'<span class="ai-badge ai-b-ebs">EBS: {len(s_ebs)}</span>'
        if s_aurora: badges += f'<span class="ai-badge ai-b-aur">Aurora: {len(s_aurora)}</span>'
        if s_rds: badges += f'<span class="ai-badge ai-b-rds">RDS: {len(s_rds)}</span>'

        resources = ""
        for r in s_ec2:
            resources += f'<div class="ai-r"><div>{esc(r.get("name","?"))}<div class="ai-r-meta">EC2 - {esc(r.get("instance_type",""))}</div></div><span class="ai-state" style="background:rgba(167,139,250,0.15);color:#a78bfa">saved</span></div>'
        for r in s_aurora:
            resources += f'<div class="ai-r"><div>{esc(r.get("cluster_id","?"))}<div class="ai-r-meta">Aurora - {esc(r.get("engine",""))} ({len(r.get("members",[]))} inst)</div></div><span class="ai-state" style="background:rgba(167,139,250,0.15);color:#a78bfa">saved</span></div>'
        for r in s_rds:
            resources += f'<div class="ai-r"><div>{esc(r.get("db_id","?"))}<div class="ai-r-meta">RDS - {esc(r.get("engine",""))} {esc(r.get("instance_class",""))}</div></div><span class="ai-state" style="background:rgba(167,139,250,0.15);color:#a78bfa">saved</span></div>'
        for r in s_ebs:
            resources += f'<div class="ai-r"><div>{esc(r.get("name","?"))}<div class="ai-r-meta">EBS - {esc(r.get("size",""))}GB {esc(r.get("volume_type",""))}</div></div><span class="ai-state" style="background:rgba(167,139,250,0.15);color:#a78bfa">saved</span></div>'

        return f"""
<div class="ai-card" style="border-color:#a78bfa;border-style:dashed">
  <div class="ai-hdr">
    <div><div class="ai-title">&#128190; {esc(p["display"])}</div><div class="ai-sub">{s_total} resources saved</div></div>
    <div style="text-align:right"><div class="ai-cost" style="color:#34d399">$0.00</div><div class="ai-cost-lbl">not running</div></div>
  </div>
  <div class="ai-badges">{badges}</div>
  <div class="ai-res">{resources}</div>
  <div class="ai-acts">
    <a class="btn restore">&#128640; Restore</a>
    <cwdb-action action="call" endpoint="{SELF_ARN}" confirmation="Restore '{esc(p['display'])}' from saved state?" display="widget">{esc(restore_params)}</cwdb-action>
  </div>
</div>"""

    badges = ""
    if p["ec2"]: badges += f'<span class="ai-badge ai-b-ec2">EC2: {len(p["ec2"])}</span>'
    if p["ebs"]: badges += f'<span class="ai-badge ai-b-ebs">EBS: {len(p["ebs"])}</span>'
    if p["aurora"]: badges += f'<span class="ai-badge ai-b-aur">Aurora: {len(p["aurora"])}</span>'
    if p["rds_standalone"]: badges += f'<span class="ai-badge ai-b-rds">RDS: {len(p["rds_standalone"])}</span>'

    resources = ""
    for r in p["ec2"][:4]:
        st = "ai-running" if r["state"] == "running" else "ai-stopped"
        resources += f'<div class="ai-r"><div>{esc(r["name"])}<div class="ai-r-meta">EC2 - {esc(r["type"])}</div></div><span class="ai-state {st}">{esc(r["state"])}</span></div>'
    for r in p["ebs"][:2]:
        resources += f'<div class="ai-r"><div>{esc(r["name"])}<div class="ai-r-meta">EBS - {esc(r["size"])}GB {esc(r["type"])}</div></div><span class="ai-state ai-available">available</span></div>'
    for r in p["aurora"][:2]:
        st = "ai-available" if r["status"] == "available" else "ai-creating"
        resources += f'<div class="ai-r"><div>{esc(r["id"])}<div class="ai-r-meta">Aurora - {esc(r["engine"])} ({r["members"]} inst)</div></div><span class="ai-state {st}">{esc(r["status"])}</span></div>'
    for r in p["rds_standalone"][:2]:
        resources += f'<div class="ai-r"><div>{esc(r["id"])}<div class="ai-r-meta">RDS - {esc(r["engine"])} {esc(r["cls"])}</div></div><span class="ai-state ai-available">{esc(r["status"])}</span></div>'

    save_params = json.dumps({"action": "save", "project": name})
    restore_params = json.dumps({"action": "restore", "project": name})
    _sv = _project_savings(p)

    # Check for active schedule
    sched = _get_schedule(p["display"])
    sched_html = ""
    if sched:
        rm_params = json.dumps({"action": "remove_schedule", "project": p["display"]})
        sched_html = f"""
  <div style="padding:6px 12px;border-top:1px solid #2a2d3a;background:rgba(79,143,247,0.05)">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div style="font-size:11px;color:#4f8ff7">&#128337; Scheduled: restore {sched["restore_at"]} | hibernate {sched["save_at"]} (Mon-Fri)</div>
      <span><a class="btn" style="background:#2a2d3a;color:#f87171;padding:2px 8px;border-radius:3px;font-size:10px;text-decoration:none">&#10005; Remove</a>
      <cwdb-action action="call" endpoint="{SELF_ARN}" confirmation="Remove schedule for '{esc(p['display'])}'?" display="widget">{esc(rm_params)}</cwdb-action></span>
    </div>
  </div>"""
    else:
        # Default schedule offer: Mon-Fri 9AM-6PM ET (restore 09:00, hibernate 18:00)
        set_params = json.dumps({"action": "set_schedule", "project": p["display"],
            "restore_at": "09:00", "save_at": "18:00", "timezone": "US/Eastern",
            "days": ["mon", "tue", "wed", "thu", "fri"]})
        sched_html = f"""
  <div style="padding:6px 12px;border-top:1px solid #2a2d3a">
    <span><a class="btn" style="background:#4f8ff7;color:#fff;padding:2px 10px;border-radius:3px;font-size:10px;text-decoration:none">&#9201; Set Schedule (Mon-Fri 9-6 ET)</a>
    <cwdb-action action="call" endpoint="{SELF_ARN}" confirmation="Auto hibernate/restore '{esc(p['display'])}' Mon-Fri, restore 9AM / hibernate 6PM US/Eastern? (edit the payload to change time/timezone)" display="widget">{esc(set_params)}</cwdb-action></span>
  </div>"""

    # Savings projection line (only meaningful when there's running cost)
    savings_html = ""
    if _sv["running_24x7"] > 0.01:
        savings_html = f"""
  <div style="padding:6px 12px;border-top:1px solid #2a2d3a;font-size:11px;color:#bbb">
    &#128176; <b style="color:#34d399">Save ~${_sv['save_scheduled']:.0f}/mo</b> on a Mon-Fri 9-6 schedule
    &nbsp;·&nbsp; up to <b style="color:#34d399">~${_sv['save_hibernated']:.0f}/mo</b> if fully hibernated
    <span style="color:#666">(24/7 now: ${_sv['running_24x7']:.0f}/mo)</span>
  </div>"""

    return f"""
<div class="ai-card{extra}">
  <div class="ai-hdr">
    <div><div class="ai-title">{icon} {esc(p["display"])}</div><div class="ai-sub">{count} resources</div></div>
    <div style="text-align:right"><div class="ai-cost">${p["hourly_cost"]:.3f}</div><div class="ai-cost-lbl">per hour</div></div>
  </div>
  <div class="ai-badges">{badges}</div>
  <div class="ai-res">{resources}</div>{savings_html}{sched_html}
  <div class="ai-acts">
    <a class="btn hibernate">&#128164; Hibernate</a>
    <cwdb-action action="call" endpoint="{SELF_ARN}" confirmation="Hibernate all resources in '{esc(p['display'])}'? (snapshot + delete)" display="widget">{esc(save_params)}</cwdb-action>
    <a class="btn restore">&#128640; Restore</a>
    <cwdb-action action="call" endpoint="{SELF_ARN}" confirmation="Restore '{esc(p['display'])}' from saved state?" display="widget">{esc(restore_params)}</cwdb-action>
  </div>
</div>"""


# ── Action Handlers ──────────────────────────────────────────
def handle_add_rule(event):
    """Add a custom hibernate rule."""
    rule = add_rule({"name": event.get("name", "Custom rule"), "service": event.get("service", "*"), "condition": event.get("condition", ""), "action": event.get("rule_action", "skip")})
    return f'<div style="padding:16px;color:#34d399"><h3>✅ Rule Added: {esc(rule["name"])}</h3><p style="color:#888;font-size:12px">Condition: {esc(rule["condition"])}</p></div>'


def handle_remove_rule(event):
    """Remove a custom rule."""
    remove_rule(event.get("rule_id", ""))
    return '<div style="padding:16px;color:#fb923c"><h3>Rule Removed</h3></div>'


def handle_list_accounts(event=None):
    """List org accounts for multi-account setup."""
    accounts = list_org_accounts()
    rows = "".join(f'<div style="font-size:12px;padding:4px 0;border-bottom:1px solid #2a2d3a">{esc(a["id"])} — {esc(a["name"])}</div>' for a in accounts[:20])
    return f'<div style="padding:16px;color:#d5d5d5"><h3 style="color:#4f8ff7">🏢 Organization Accounts ({len(accounts)})</h3><div style="background:#16213e;border-radius:6px;padding:8px;margin-top:8px">{rows}</div><p style="color:#888;font-size:11px;margin-top:8px">Deploy cross-account role via StackSets to enable multi-account hibernation.</p></div>'


def handle_set_accounts(event):
    """Save enabled accounts for multi-account mode."""
    accounts = event.get("accounts", [])
    save_account_config(accounts)
    return f'<div style="padding:16px;color:#34d399"><h3>✅ {len(accounts)} Accounts Configured</h3></div>'


def handle_analyze():
    """Run AI analysis on all projects."""
    projects = discover_projects()
    result = analyze_projects(projects)

    if "error" in result:
        return f'<div style="padding:16px;color:#f87171"><h3>Hibernate AI Analysis Error</h3><p>{esc(result["error"])}</p></div>'

    # #8 Deterministic guardrail: override any safe_to_save that the metrics contradict.
    import feedback as _fb
    result = _fb.validate_result(result, result.get("_project_data", []))

    recs = result.get("recommendations", [])
    summary = result.get("summary", "")

    html = '<div style="padding:16px;font-family:sans-serif;color:#d5d5d5">'
    html += f'<h3 style="color:#a78bfa">&#129302; Hibernate AI Analysis</h3>'
    html += f'<p style="color:#888;margin-bottom:12px">{esc(summary)}</p>'

    for rec in recs:
        risk = rec.get("risk", "review_needed")
        color = {"safe_to_save": "#34d399", "review_needed": "#fb923c", "keep_running": "#f87171"}.get(risk, "#888")
        icon = {"safe_to_save": "&#9989;", "review_needed": "&#9888;&#65039;", "keep_running": "&#128308;"}.get(risk, "")

        html += f'<div style="background:#16213e;border:1px solid #2a2d3a;border-left:3px solid {color};border-radius:6px;padding:12px;margin-bottom:8px">'
        html += f'<div style="font-weight:600">{icon} {esc(rec.get("project","?"))}</div>'
        if rec.get("guardrail_override"):
            html += '<div style="font-size:10px;color:#fbbf24;margin:2px 0">🛡️ Adjusted by deterministic guardrail (metrics contradicted the model)</div>'
        html += f'<div style="font-size:12px;color:{color};margin:4px 0">{esc(risk.replace("_"," ").title())} - {esc(rec.get("reason",""))}</div>'
        if rec.get("monthly_savings"):
            html += f'<div style="font-size:12px;color:#34d399">&#128176; Potential savings: ${rec["monthly_savings"]:.0f}/mo</div>'
        for s in rec.get("suggestions", []):
            html += f'<div style="font-size:11px;color:#888;margin-left:12px">&#8226; {esc(s)}</div>'
        for a in rec.get("anomalies", []):
            html += f'<div style="font-size:11px;color:#fb923c;margin-left:12px">&#9888; {esc(a)}</div>'

        # Schedule suggestion
        sched = rec.get("suggested_schedule")
        if sched and isinstance(sched, dict) and sched.get("restore_at"):
            proj = rec.get("project", "")
            html += f'<div style="margin-top:8px;padding:8px;background:#0f0f23;border-radius:4px;border:1px solid #4f8ff7">'
            html += f'<div style="font-size:12px;color:#4f8ff7;font-weight:600">&#128337; Suggested Schedule</div>'
            html += f'<div style="font-size:11px;color:#d5d5d5;margin:4px 0">Restore at <b>{esc(sched["restore_at"])}</b> | Hibernate at <b>{esc(sched["save_at"])}</b> ({esc(sched.get("timezone","US/Eastern"))})</div>'
            html += f'<div style="font-size:10px;color:#888">Days: {esc(", ".join(sched.get("days",["mon-fri"])))}</div>'
            html += f'<div style="font-size:10px;color:#888;margin-top:2px">{esc(sched.get("reasoning",""))}</div>'
            sched_params = json.dumps({"action": "set_schedule", "project": proj, "restore_at": sched["restore_at"], "save_at": sched["save_at"], "timezone": sched.get("timezone", "US/Eastern"), "days": sched.get("days", ["mon","tue","wed","thu","fri"])})
            html += f'<div style="margin-top:6px"><a class="btn" style="background:#4f8ff7;color:#fff;padding:4px 12px;border-radius:4px;font-size:11px;font-weight:600;text-decoration:none">&#9201; Set This Schedule</a>'
            html += f'<cwdb-action action="call" endpoint="{SELF_ARN}" confirmation="Set auto hibernate/restore schedule for {esc(proj)}?" display="widget">{esc(sched_params)}</cwdb-action></div>'
            html += '</div>'

        # #8 Feedback capture: thumbs up/down per recommendation.
        _proj = rec.get("project", "")
        _up = json.dumps({"action": "feedback", "project": _proj, "verdict": "up", "recommendation": risk})
        _down = json.dumps({"action": "feedback", "project": _proj, "verdict": "down", "recommendation": risk})
        html += ('<div style="margin-top:8px;font-size:11px;color:#888">Was this helpful? '
                 f'<a class="btn" style="padding:2px 8px;border-radius:3px;background:#1a2e1a;color:#34d399;text-decoration:none">👍</a>'
                 f'<cwdb-action action="call" endpoint="{SELF_ARN}" display="widget">{esc(_up)}</cwdb-action> '
                 f'<a class="btn" style="padding:2px 8px;border-radius:3px;background:#2e1a1a;color:#f87171;text-decoration:none">👎</a>'
                 f'<cwdb-action action="call" endpoint="{SELF_ARN}" display="widget">{esc(_down)}</cwdb-action></div>')

        html += '</div>'

    html += '</div>'
    return html


def handle_nlp(user_input):
    """Process natural language command."""
    if not user_input:
        return '<div style="padding:16px;color:#888">No input provided.</div>'

    projects = discover_projects()
    result = process_natural_language(user_input, projects)

    if "error" in result:
        return f'<div style="padding:16px;color:#f87171"><h3>Error</h3><p>{esc(result["error"])}</p></div>'

    action = result.get("action", "info")
    project = result.get("project")
    response_text = result.get("response", "")

    html = f'<div style="padding:16px;font-family:sans-serif;color:#d5d5d5">'
    html += f'<p style="color:#4f8ff7">&#129302; {esc(response_text)}</p>'

    if action == "save" and project:
        html += (f'<p style="margin-top:8px;color:#9aa">Hibernating <b>{esc(project)}</b> is recommended, '
                 f'but HibernateAI is in <b>read-only mode</b> — review the analysis and take action through '
                 f'your own change process. (Destructive actions require the separate customer-authorized add-on.)</p>')
    elif action == "restore" and project:
        html += (f'<p style="margin-top:8px;color:#9aa">Restoring <b>{esc(project)}</b> is recommended, '
                 f'but HibernateAI is in <b>read-only mode</b> and does not perform restore actions.</p>')
    elif action == "analyze":
        html += f'<p style="margin-top:8px"><a class="btn analyze">Run Analysis</a>'
        html += f'<cwdb-action action="call" endpoint="{SELF_ARN}" display="widget">{esc(json.dumps({"action":"analyze"}))}</cwdb-action></p>'

    html += '</div>'
    return html



def handle_set_config(event):
    """Persist a partial config update (grouping/environment tag keys, grouping_dimension,
    deny-list, thresholds, etc.). Merges over the existing config; #1/#2."""
    patch = event.get("config", {})
    if not isinstance(patch, dict):
        return json.dumps({"success": False, "error": "config must be an object"})
    current = hib_config.load_config()
    current.update(patch)
    hib_config.save_config(current)
    return json.dumps({"success": True, "config": {k: v for k, v in current.items() if k != "updated"}}, default=str)


def handle_detect_tags():
    """Scan the account's tags and propose a grouping/environment mapping (#1)."""
    from onboarding import propose_tag_mapping
    return json.dumps({"success": True, "proposal": propose_tag_mapping()}, default=str)


def handle_deploy_stackset(event):
    """Roll the cross-account role to selected OUs via StackSets (#6). Privileged."""
    ou_ids = event.get("ou_ids") or []
    if not ou_ids:
        return json.dumps({"success": False, "error": "ou_ids required"})
    regions = event.get("regions")
    try:
        result = deploy_stackset_to_ous(ou_ids, regions=regions)
        return json.dumps({"success": result.get("status") != "error", "result": result}, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def handle_feedback(event):
    """Persist thumbs up/down feedback on a recommendation (#8 closed loop)."""
    import feedback as _fb
    project = event.get("project", "")
    verdict = event.get("verdict", "up")
    fid = _fb.record_feedback(
        project, verdict,
        recommendation=event.get("recommendation"),
        user=__import__("approvals").caller_identity(event),
        reason=event.get("reason"),
    )
    icon = "👍" if verdict == "up" else "👎"
    return (f'<div style="padding:16px;font-family:sans-serif;color:#d5d5d5">'
            f'<p style="color:#34d399">{icon} Thanks — feedback recorded for <b>{esc(project)}</b>. '
            f'It will improve future recommendations.</p></div>')
