"""
Standalone test for the FULL _gather_aws_context() pipeline via the AWS API MCP Server.
ASYNC / PARALLEL VERSION, with a persistent MCP session across calls.

Key changes vs. the original sync version:
  1. The MCP client + `call_aws` tool handle is created ONCE per PROCESS and reused
     for every command AND every call to _gather_aws_context(), not just within a
     single run. The old version spun up a brand-new uvx subprocess per AWS call
     (~8s/call flat + a 100s+ cold start); a naive async fix would still spin up a
     fresh subprocess on every *run* of the pipeline. This version pays that
     startup cost exactly once for the life of the process, via a persistent
     background event-loop thread that owns the MCP session.
  2. All 24 AWS CLI calls are independent of each other (none of them need the
     *result* of another call to build their own command string), so they are fired
     concurrently via asyncio.gather() with no artificial concurrency cap.
  3. The cross-referencing logic (VPC -> subnets -> route tables -> public/private,
     etc.) is unchanged, it just now runs AFTER all results are back, reading from a
     dict keyed by command string instead of calling out one at a time.

Paste this whole file into one Jupyter cell, or run directly:
    python test_gather_aws_context_async.py

If you call _gather_aws_context() multiple times in the same process (e.g. once
per pipeline run inside Chandra), every call after the first skips subprocess
startup + MCP handshake entirely and only pays for the actual AWS round trips.
"""

import sys
import os
import json
import asyncio
import logging
import threading
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_test")


def check_cancelled():
    pass


# ============================================================
# Persistent MCP session
#
# Owns exactly one background thread running exactly one event loop for the
# life of the process. The MCP client (and its uvx subprocess) is created
# lazily on first use on THAT loop, and never torn down. Every subsequent
# _gather_aws_context() call — from any calling thread — submits its
# coroutine to this same loop via run_coroutine_threadsafe, so the
# already-connected aws_tool handle just gets reused.
# ============================================================

class _PersistentMCPSession:
    def __init__(self):
        self._loop = None
        self._thread = None
        self._start_lock = threading.Lock()
        self._client = None
        self._aws_tool = None
        self._client_lock = None  # created lazily, bound to the loop

    def _ensure_loop_started(self):
        if self._loop is not None:
            return
        with self._start_lock:
            if self._loop is not None:
                return
            if sys.platform == "win32":
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()

            def _run_forever():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            t = threading.Thread(target=_run_forever, daemon=True, name="mcp-session-loop")
            t.start()
            self._loop = loop
            self._thread = t

    async def _get_aws_tool(self):
        # First coroutine to run on the loop creates the client; every later
        # coroutine on the same loop just reads the cached handle.
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()

        async with self._client_lock:
            if self._aws_tool is not None:
                return self._aws_tool

            from langchain_mcp_adapters.client import MultiServerMCPClient

            region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION") or "us-east-1"
            server_config = {
                "aws_api": {
                    "command": "uvx",
                    "args": ["awslabs.aws-api-mcp-server@latest"],
                    "env": {
                        "AWS_REGION": os.getenv("AWS_REGION", region),
                        "AWS_DEFAULT_REGION": os.getenv("AWS_DEFAULT_REGION", region),
                        **({"AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID")} if os.getenv("AWS_ACCESS_KEY_ID") else {}),
                        **({"AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY")} if os.getenv("AWS_SECRET_ACCESS_KEY") else {}),
                        **({"AWS_SESSION_TOKEN": os.getenv("AWS_SESSION_TOKEN")} if os.getenv("AWS_SESSION_TOKEN") else {}),
                        **({"AWS_PROFILE": os.getenv("AWS_PROFILE")} if os.getenv("AWS_PROFILE") else {}),
                        "UV_LINK_MODE": "copy",
                    },
                    "transport": "stdio",
                },
            }

            print("[MCP SESSION] Cold start: launching aws_api MCP server (first call in this process only)...")
            t0 = time.perf_counter()
            self._client = MultiServerMCPClient(server_config)
            tools = await self._client.get_tools(server_name="aws_api")
            self._aws_tool = next((t for t in tools if t.name == "call_aws"), None)
            print(f"[MCP SESSION] Ready in {time.perf_counter() - t0:.2f}s — reused for all future calls in this process.")

            if self._aws_tool is None:
                raise RuntimeError("'call_aws' tool not found on aws_api MCP server")
            return self._aws_tool

    def run_coro(self, coro_fn, *args, **kwargs):
        """Submit a coroutine to the persistent loop from any (sync) calling
        thread and block until it completes."""
        self._ensure_loop_started()
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args, **kwargs), self._loop)
        return fut.result()

    async def get_tool_async(self):
        """For callers that are already running inside the persistent loop
        (e.g. code that awaits _gather_aws_context_async() directly)."""
        return await self._get_aws_tool()


_session = _PersistentMCPSession()


# Collects (command, seconds) for a summary table at the end
_TIMING_LOG = []


def _parse_call_aws_response(res, command: str, debug: bool = False):
    """Same parsing logic as the original, just pulled out so it can be reused
    from the async call site. Returns a dict on success, or None on a parse miss."""
    if debug:
        print(f"[DEBUG] {command}\n  RAW: {res!r}\n")

    inner_text = res if isinstance(res, str) else json.dumps(res)
    try:
        parsed = json.loads(inner_text)
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "text" in parsed[0]:
            inner_text = parsed[0]["text"]
        elif isinstance(parsed, dict) and "text" in parsed:
            inner_text = parsed["text"]
    except Exception:
        pass

    try:
        parsed_text = json.loads(inner_text)
        if isinstance(parsed_text, list) and parsed_text and "response" in parsed_text[0]:
            as_json = parsed_text[0]["response"].get("as_json")
            if as_json:
                return json.loads(as_json)
    except Exception:
        pass

    print(f"[MISS] Could not parse response for: {command}")
    print(f"       inner_text was: {inner_text[:300]!r}")
    return None


async def _run_mcp_aws_command_async(aws_tool, command: str, debug: bool = False):
    """Runs a single `call_aws` invocation using an ALREADY-CONNECTED tool handle.
    No client creation here, no concurrency cap — just fire and await."""
    t_start = time.perf_counter()
    start_stamp = time.strftime("%H:%M:%S")
    print(f"[TIMER START] {start_stamp}  ->  {command}")

    try:
        res = await aws_tool.ainvoke({"cli_command": command})
        parsed = _parse_call_aws_response(res, command, debug=debug)
        result = parsed if parsed is not None else {}
    except Exception as exc:
        logger.warning(f"MCP AWS CLI execution failed for '{command}': {exc}")
        result = {}

    elapsed = time.perf_counter() - t_start
    end_stamp = time.strftime("%H:%M:%S")
    print(f"[TIMER END]   {end_stamp}  ->  {command}  ({elapsed:.2f}s)")
    _TIMING_LOG.append((command, elapsed))

    return command, result


# ============================================================
# _gather_aws_context (async, parallel fetch phase)
# ============================================================

async def _gather_aws_context_async() -> str:
    check_cancelled()

    region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION") or "us-east-1"

    try:
        # ---- Reuse the persistent session's tool handle. Only pays for
        # subprocess startup + MCP handshake on the very first call made in
        # this process; every call after that is instant. ----
        aws_tool = await _session.get_tool_async()

        # SSM public-parameter paths to pull the FULL AMI catalog from, instead
        # of one hardcoded parameter name. get-parameters-by-path returns every
        # variant under the path in one call (AL2 vs AL2023, x86_64 vs arm64,
        # full vs minimal, gp2 vs gp3), so Terraform can pick the AMI that
        # actually matches the target instance architecture/generation instead
        # of always defaulting to one specific x86_64 image.
        # Add more paths here if you want other OS families in the catalog too,
        # e.g. "/aws/service/ami-windows-latest" or a Canonical Ubuntu path.
        AMI_SSM_PATHS = [
            "/aws/service/ami-amazon-linux-latest",
        ]
        ami_commands = [
            f"aws ssm get-parameters-by-path --path {path} --recursive --region {region}"
            for path in AMI_SSM_PATHS
        ]

        # ---- Build the full list of independent commands up front ----
        commands = [
            "aws sts get-caller-identity",
            f"aws ec2 describe-vpcs --region {region}",
            f"aws ec2 describe-subnets --region {region}",
            f"aws ec2 describe-internet-gateways --region {region}",
            f"aws ec2 describe-route-tables --region {region}",
            f"aws ec2 describe-security-groups --region {region}",
            f"aws ec2 describe-vpc-endpoints --region {region}",
            f"aws ec2 describe-availability-zones --region {region}",
            f"aws ec2 describe-key-pairs --region {region}",
            f"aws route53 list-hosted-zones --region {region}",
            "aws iam list-roles",
            "aws iam list-instance-profiles",
            "aws s3api list-buckets",
            *ami_commands,
            f"aws rds describe-db-instances --region {region}",
            f"aws rds describe-db-subnet-groups --region {region}",
            f"aws dynamodb list-tables --region {region}",
            f"aws elbv2 describe-load-balancers --region {region}",
            f"aws ec2 describe-addresses --region {region}",
            f"aws ec2 describe-nat-gateways --filter Name=state,Values=available --region {region}",
            f"aws acm list-certificates --region {region}",
            "aws cloudfront list-distributions",
            f"aws kms list-aliases --region {region}",
            f"aws lambda list-functions --region {region}",
        ]

        acm_use1_cmd = None
        if region != "us-east-1":
            acm_use1_cmd = "aws acm list-certificates --region us-east-1"
            commands.append(acm_use1_cmd)

        # ---- Round 1: fire ALL independent commands concurrently, no cap ----
        tasks = [_run_mcp_aws_command_async(aws_tool, cmd) for cmd in commands]
        pairs = await asyncio.gather(*tasks)
        results = dict(pairs)  # command -> parsed dict (or {} on failure)

        # ---- Round 2: role policies for roles attached to instance profiles ----
        # `list-instance-profiles` already embeds each profile's attached Role
        # object (name/ARN), so that link needs no extra call. What it does NOT
        # include is what permissions that role actually grants — for that we
        # need one more call per role. We only fetch this for roles that are
        # actually attached to an instance profile (i.e. usable by EC2), not
        # every role in the account, to keep this bounded and relevant.
        instance_profiles_result = results.get("aws iam list-instance-profiles")
        profile_role_map = {}  # instance_profile_name -> role_name or None
        if instance_profiles_result:
            for p in instance_profiles_result.get("InstanceProfiles") or []:
                prof_name = p.get("InstanceProfileName")
                roles_on_profile = p.get("Roles") or []
                profile_role_map[prof_name] = roles_on_profile[0].get("RoleName") if roles_on_profile else None

        roles_needing_policies = sorted({r for r in profile_role_map.values() if r})

        policy_commands = []
        for role_name in roles_needing_policies:
            policy_commands.append(f"aws iam list-attached-role-policies --role-name {role_name}")
            policy_commands.append(f"aws iam list-role-policies --role-name {role_name}")

        if policy_commands:
            policy_tasks = [_run_mcp_aws_command_async(aws_tool, cmd) for cmd in policy_commands]
            policy_pairs = await asyncio.gather(*policy_tasks)
            results.update(dict(policy_pairs))

    except Exception as exc:
        logger.warning("aws_context.failed (fetch phase): %s", exc)
        return ""

    # ============================================================
    # Everything below is pure post-processing on already-fetched
    # data — no more network calls, so it stays synchronous and
    # is byte-for-byte the same logic as the original version.
    # ============================================================
    try:
        lines = ["AWS ACCOUNT GROUNDING (live, fetched at pipeline start — treat as ground truth):"]

        identity = results.get("aws sts get-caller-identity")
        if identity:
            lines.append(f"  Account ID : {identity.get('Account')}")
            lines.append(f"  Caller ARN : {identity.get('Arn')}")
        else:
            lines.append("  Account ID : (unavailable — could not call sts:GetCallerIdentity via MCP)")

        lines.append(f"  Region     : {region}")

        def _name_tag(tags):
            return next((t["Value"] for t in (tags or []) if t.get("Key") == "Name"), None)

        all_vpcs = results.get(f"aws ec2 describe-vpcs --region {region}")
        vpc_list = all_vpcs.get("Vpcs") or [] if all_vpcs else []

        all_subnets = results.get(f"aws ec2 describe-subnets --region {region}")
        subnet_list = all_subnets.get("Subnets") or [] if all_subnets else []
        subnets_by_vpc = {}
        for s in subnet_list:
            subnets_by_vpc.setdefault(s["VpcId"], []).append(s)

        all_igws = results.get(f"aws ec2 describe-internet-gateways --region {region}")
        igw_list = all_igws.get("InternetGateways") or [] if all_igws else []
        igw_by_vpc = {}
        for igw in igw_list:
            for att in igw.get("Attachments") or []:
                if att.get("State") == "available":
                    igw_by_vpc[att["VpcId"]] = igw["InternetGatewayId"]

        all_route_tables = results.get(f"aws ec2 describe-route-tables --region {region}")
        rt_list = all_route_tables.get("RouteTables") or [] if all_route_tables else []
        main_rt_by_vpc = {}
        subnet_rt_map = {}
        for rt in rt_list:
            vpc_id = rt.get("VpcId")
            for assoc in rt.get("Associations") or []:
                if assoc.get("Main"):
                    main_rt_by_vpc[vpc_id] = rt
                if assoc.get("SubnetId"):
                    subnet_rt_map[assoc["SubnetId"]] = rt

        def _is_public_subnet(subnet_id, vpc_id):
            rt = subnet_rt_map.get(subnet_id) or main_rt_by_vpc.get(vpc_id)
            if not rt:
                return False
            for route in rt.get("Routes") or []:
                gw = route.get("GatewayId") or ""
                dest = route.get("DestinationCidrBlock") or route.get("DestinationIpv6CidrBlock") or ""
                if gw.startswith("igw-") and dest in ("0.0.0.0/0", "::/0"):
                    return True
            return False

        all_sgs = results.get(f"aws ec2 describe-security-groups --region {region}")
        sg_list = all_sgs.get("SecurityGroups") or [] if all_sgs else []
        sgs_by_vpc = {}
        for sg in sg_list:
            sgs_by_vpc.setdefault(sg["VpcId"], []).append(sg)

        all_endpoints = results.get(f"aws ec2 describe-vpc-endpoints --region {region}")
        endpoint_list = all_endpoints.get("VpcEndpoints") or [] if all_endpoints else []
        endpoints_by_vpc = {}
        for ep in endpoint_list:
            endpoints_by_vpc.setdefault(ep["VpcId"], []).append(ep)

        default_vpc = None
        if not vpc_list:
            lines.append("  VPCs: (none found in this account/region)")
        else:
            lines.append(f"  VPCs found in {region}: {len(vpc_list)}")
            for vpc in vpc_list:
                vpc_id = vpc["VpcId"]
                is_default = vpc.get("IsDefault", False)
                if is_default:
                    default_vpc = vpc_id
                name = _name_tag(vpc.get("Tags"))
                label = f"{vpc_id}{' (' + name + ')' if name else ''}{' [DEFAULT]' if is_default else ''}"
                lines.append(f"\n  --- VPC: {label} ---")
                lines.append(f"    Primary CIDR: {vpc.get('CidrBlock')}")

                secondary_cidrs = [
                    c["CidrBlock"] for c in (vpc.get("CidrBlockAssociationSet") or [])
                    if c.get("CidrBlock") != vpc.get("CidrBlock") and c.get("CidrBlockState", {}).get("State") == "associated"
                ]
                if secondary_cidrs:
                    lines.append(f"    Secondary CIDR blocks: {', '.join(secondary_cidrs)}")

                ipv6_cidrs = [
                    c["Ipv6CidrBlock"] for c in (vpc.get("Ipv6CidrBlockAssociationSet") or [])
                    if c.get("Ipv6CidrBlockState", {}).get("State") == "associated"
                ]
                if ipv6_cidrs:
                    lines.append(f"    IPv6 CIDR blocks: {', '.join(ipv6_cidrs)}")

                igw_id = igw_by_vpc.get(vpc_id)
                lines.append(f"    Internet Gateway: {igw_id if igw_id else '(none attached — no route to the internet is possible without one)'}")

                vpc_subnets = subnets_by_vpc.get(vpc_id, [])
                if vpc_subnets:
                    for s in vpc_subnets:
                        sid = s["SubnetId"]
                        s_name = _name_tag(s.get("Tags"))
                        public = _is_public_subnet(sid, vpc_id)
                        lines.append(
                            f"    Subnet {sid}{' (' + s_name + ')' if s_name else ''}: "
                            f"CIDR={s.get('CidrBlock')}, AZ={s.get('AvailabilityZone')}, "
                            f"AvailableIPs={s.get('AvailableIpAddressCount')}, "
                            f"{'PUBLIC (has IGW route)' if public else 'PRIVATE (no IGW route)'}"
                        )
                else:
                    lines.append("    Subnets: (none found in this VPC)")

                vpc_sgs = sgs_by_vpc.get(vpc_id, [])
                if vpc_sgs:
                    sg_info = [f"{sg['GroupName']} ({sg['GroupId']})" for sg in vpc_sgs]
                    lines.append(f"    Security Groups: {', '.join(sg_info)}")
                else:
                    lines.append("    Security Groups: (none found)")

                vpc_endpoints = endpoints_by_vpc.get(vpc_id, [])
                if vpc_endpoints:
                    ep_info = [
                        f"{ep.get('ServiceName')} ({ep.get('VpcEndpointType')}, {ep.get('State')})"
                        for ep in vpc_endpoints
                    ]
                    lines.append(f"    VPC Endpoints: {', '.join(ep_info)}")
                else:
                    lines.append("    VPC Endpoints: (none — private subnets with no NAT Gateway will NOT be able to reach S3/DynamoDB/other AWS services)")

            lines.append("")
            if default_vpc:
                lines.append(f"  Default VPC for this account/region: {default_vpc} (use this VPC unless the request specifies otherwise or names a different VPC above)")
            else:
                lines.append("  Default VPC for this account/region: (none — you MUST pick one of the VPCs listed above, or ask which VPC to target if ambiguous)")

        azs = results.get(f"aws ec2 describe-availability-zones --region {region}")
        az_names = [az["ZoneName"] for az in (azs.get("AvailabilityZones") or []) if az["State"] == "available"] if azs else []
        if az_names:
            lines.append(f"  Available AZs: {', '.join(az_names)}")

        key_pairs = results.get(f"aws ec2 describe-key-pairs --region {region}")
        kp_names = [kp["KeyName"] for kp in (key_pairs.get("KeyPairs") or [])] if key_pairs else []
        if kp_names:
            lines.append(f"  Existing Key Pairs: {', '.join(kp_names)}")
        else:
            lines.append("  Existing Key Pairs: (none found, you must generate one if needed)")

        zones = results.get(f"aws route53 list-hosted-zones --region {region}")
        zone_info = [f"{z['Name']} (ID: {z['Id']})" for z in (zones.get("HostedZones") or []) if not z.get("Config", {}).get("PrivateZone")] if zones else []
        if zone_info:
            lines.append(f"  Public Route53 Zones: {', '.join(zone_info)}")
        else:
            lines.append("  Public Route53 Zones: (none found)")

        # ---- PATCHED: full IAM role list now includes ARN / RoleId / Path /
        # CreateDate for each role, not just the bare name — no extra API
        # calls needed since list-roles already returns these fields. ----
        iam_roles = results.get("aws iam list-roles")
        role_objs = [
            r for r in (iam_roles.get("Roles") or [])
            if not r.get("Path", "/").startswith("/aws-service-role/")
        ] if iam_roles else []
        if role_objs:
            lines.append(f"  Existing IAM Roles in account ({len(role_objs)}, name-collision + ARN reference — see Instance Profiles below for role->permissions detail):")
            for r in role_objs:
                lines.append(
                    f"    {r.get('RoleName')}  ARN={r.get('Arn')}  "
                    f"(ID={r.get('RoleId')}, Path={r.get('Path')}, Created={r.get('CreateDate')})"
                )
        else:
            lines.append("  Existing IAM Roles: (none found or unavailable — use name_prefix regardless)")

        # ---- PATCHED: instance profiles now surface Profile ARN/ID/CreateDate
        # and the attached role's ARN/ID/CreateDate, all already embedded in
        # the list-instance-profiles response — no extra API calls needed. ----
        instance_profiles_list = instance_profiles_result.get("InstanceProfiles") or [] if instance_profiles_result else []
        if instance_profiles_list:
            lines.append(f"  Existing IAM Instance Profiles ({len(instance_profiles_list)}) — role and permissions each one actually grants:")
            for p in instance_profiles_list:
                prof_name = p.get("InstanceProfileName")
                prof_arn = p.get("Arn")
                prof_id = p.get("InstanceProfileId")
                prof_created = p.get("CreateDate")
                role_name = profile_role_map.get(prof_name)

                lines.append(f"    {prof_name}")
                lines.append(f"      Profile ARN: {prof_arn}  (ID: {prof_id}, Created: {prof_created})")

                if not role_name:
                    lines.append("      Role: (none attached — cannot be used as-is)")
                    continue

                # Already embedded on the instance profile's Roles list — no
                # extra call needed to get the role's ARN/ID/CreateDate.
                role_obj = next(
                    (r for r in (p.get("Roles") or []) if r.get("RoleName") == role_name),
                    {},
                )
                role_arn = role_obj.get("Arn")
                role_id = role_obj.get("RoleId")
                role_created = role_obj.get("CreateDate")

                attached = results.get(f"aws iam list-attached-role-policies --role-name {role_name}") or {}
                managed_policies = [
                    ap.get("PolicyName") for ap in (attached.get("AttachedPolicies") or [])
                ]

                inline = results.get(f"aws iam list-role-policies --role-name {role_name}") or {}
                inline_policies = inline.get("PolicyNames") or []

                lines.append(f"      Role: {role_name}")
                lines.append(f"      Role ARN: {role_arn}  (ID: {role_id}, Created: {role_created})")
                lines.append(f"      Managed Policies: {', '.join(managed_policies) if managed_policies else '(none)'}")
                lines.append(f"      Inline Policies: {', '.join(inline_policies) if inline_policies else '(none)'}")
        else:
            lines.append("  Existing IAM Instance Profiles: (none found)")

        s3_buckets = results.get("aws s3api list-buckets")
        bucket_names = [b["Name"] for b in (s3_buckets.get("Buckets") or [])] if s3_buckets else []
        if bucket_names:
            lines.append(f"  Existing S3 Buckets in account ({len(bucket_names)}): {', '.join(bucket_names)}")
        else:
            lines.append("  Existing S3 Buckets in account: (none found)")

        # ---- AMI catalog: parse every variant instead of one hardcoded
        # parameter, so Terraform can match AMI to the actual target
        # architecture (x86_64 vs arm64/Graviton) and generation (AL2 vs AL2023,
        # full vs minimal, gp2 vs gp3) instead of always defaulting to one image.
        ami_catalog = {}  # friendly_name -> ami_id, e.g. "al2023-ami-kernel-6.1-x86_64" -> "ami-..."
        for path, cmd in zip(AMI_SSM_PATHS, ami_commands):
            ami_result = results.get(cmd)
            if not ami_result:
                continue
            for p in ami_result.get("Parameters") or []:
                full_name = p.get("Name") or ""
                value = p.get("Value")
                if not full_name or not value:
                    continue
                friendly = full_name[len(path):].lstrip("/")
                ami_catalog[friendly] = value

        if ami_catalog:
            lines.append(f"  Amazon Linux AMI Catalog (latest, {len(ami_catalog)} variants found):")
            for name in sorted(ami_catalog):
                lines.append(f"    {name}: {ami_catalog[name]}")
            # Surface one sensible default (AL2023, full, x86_64) so there's
            # still an obvious pick when the request doesn't specify OS/arch.
            default_ami_id = next(
                (v for k, v in sorted(ami_catalog.items())
                 if k.startswith("al2023-ami-kernel-") and k.endswith("-x86_64") and "minimal" not in k),
                None,
            )
            if default_ami_id:
                lines.append(f"  Recommended default if OS/arch unspecified (AL2023, x86_64): {default_ami_id}")
        else:
            lines.append("  Amazon Linux AMI Catalog: (unavailable — use a data \"aws_ami\" lookup instead)")

        rds_instances_result = results.get(f"aws rds describe-db-instances --region {region}")
        rds_instance_list = rds_instances_result.get("DBInstances") or [] if rds_instances_result else []

        rds_subnet_groups_result = results.get(f"aws rds describe-db-subnet-groups --region {region}")
        rds_subnet_group_list = rds_subnet_groups_result.get("DBSubnetGroups") or [] if rds_subnet_groups_result else []

        # describe-db-instances already embeds the full DBSubnetGroup (VPC +
        # subnet list) per instance, and describe-db-subnet-groups embeds VPC +
        # AZ per subnet — no extra calls needed, just surfacing what's already
        # in the response instead of flattening it away into a bare name list.
        if rds_instance_list:
            lines.append(f"  Existing RDS Instances ({len(rds_instance_list)}) — VPC/subnets each one actually runs in:")
            for db in rds_instance_list:
                db_id = db.get("DBInstanceIdentifier")
                engine = db.get("Engine")
                status = db.get("DBInstanceStatus")
                db_sg = db.get("DBSubnetGroup") or {}
                db_vpc = db_sg.get("VpcId")
                db_sg_name = db_sg.get("DBSubnetGroupName")
                db_subnet_ids = [s.get("SubnetIdentifier") for s in (db_sg.get("Subnets") or []) if s.get("SubnetIdentifier")]
                lines.append(
                    f"    {db_id} (engine={engine}, status={status}): VPC={db_vpc}, "
                    f"DBSubnetGroup={db_sg_name}, Subnets=[{', '.join(db_subnet_ids) if db_subnet_ids else '(none)'}]"
                )
        else:
            lines.append("  Existing RDS Instances: (none found)")

        if rds_subnet_group_list:
            lines.append(f"  Existing RDS Subnet Groups ({len(rds_subnet_group_list)}) — VPC + AZ spread each one covers (check this against golden rule #10, 'spans the needed AZs'):")
            for g in rds_subnet_group_list:
                g_name = g.get("DBSubnetGroupName")
                g_vpc = g.get("VpcId")
                subnet_az_pairs = [
                    f"{s.get('SubnetIdentifier')}({(s.get('SubnetAvailabilityZone') or {}).get('Name', '?')})"
                    for s in (g.get("Subnets") or [])
                ]
                lines.append(f"    {g_name}: VPC={g_vpc}, Subnets=[{', '.join(subnet_az_pairs) if subnet_az_pairs else '(none)'}]")
        else:
            lines.append("  Existing RDS Subnet Groups: (none found — you must create one for any RDS instance)")

        dynamo_tables = results.get(f"aws dynamodb list-tables --region {region}")
        table_names = dynamo_tables.get("TableNames") or [] if dynamo_tables else []
        if table_names:
            lines.append(f"  Existing DynamoDB Tables: {', '.join(table_names)}")
        else:
            lines.append("  Existing DynamoDB Tables: (none found)")

        albs = results.get(f"aws elbv2 describe-load-balancers --region {region}")
        alb_info = [f"{lb['LoadBalancerName']} ({lb['Type']}, {lb['DNSName']})" for lb in (albs.get("LoadBalancers") or [])] if albs else []
        if alb_info:
            lines.append(f"  Existing Load Balancers (ALB/NLB): {', '.join(alb_info)}")
        else:
            lines.append("  Existing Load Balancers (ALB/NLB): (none found)")

        eips = results.get(f"aws ec2 describe-addresses --region {region}")
        unassociated_eips = [e["PublicIp"] for e in (eips.get("Addresses") or []) if not e.get("AssociationId")] if eips else []
        if unassociated_eips:
            lines.append(f"  Unassociated Elastic IPs available for reuse: {', '.join(unassociated_eips)}")
        else:
            lines.append("  Unassociated Elastic IPs: (none — a new EIP will consume account quota)")

        nats = results.get(f"aws ec2 describe-nat-gateways --filter Name=state,Values=available --region {region}")
        nat_ids = [n["NatGatewayId"] for n in (nats.get("NatGateways") or [])] if nats else []
        if nat_ids:
            lines.append(f"  Existing NAT Gateways: {', '.join(nat_ids)}")
        else:
            lines.append("  Existing NAT Gateways: (none found — private subnets have no internet egress unless one is created)")

        acm_certs = results.get(f"aws acm list-certificates --region {region}")
        cert_info = [f"{c['DomainName']} ({c['CertificateArn']})" for c in (acm_certs.get("CertificateSummaryList") or [])] if acm_certs else []
        if cert_info:
            lines.append(f"  Existing ACM Certificates ({region}): {', '.join(cert_info)}")
        else:
            lines.append(f"  Existing ACM Certificates ({region}): (none found — HTTPS listeners will need a new cert, which requires DNS validation)")

        if acm_use1_cmd:
            acm_certs_use1 = results.get(acm_use1_cmd)
            cert_info_use1 = [f"{c['DomainName']} ({c['CertificateArn']})" for c in (acm_certs_use1.get("CertificateSummaryList") or [])] if acm_certs_use1 else []
            if cert_info_use1:
                lines.append(f"  Existing ACM Certificates (us-east-1, for CloudFront use only): {', '.join(cert_info_use1)}")
            else:
                lines.append("  Existing ACM Certificates (us-east-1, for CloudFront use only): (none found)")

        cf_dists = results.get("aws cloudfront list-distributions")
        dist_info = [
            f"{d['Id']} ({d.get('DomainName')})"
            for d in ((cf_dists.get("DistributionList") or {}).get("Items") or [])
        ] if cf_dists else []
        if dist_info:
            lines.append(f"  Existing CloudFront Distributions: {', '.join(dist_info)}")
        else:
            lines.append("  Existing CloudFront Distributions: (none found)")

        kms_aliases = results.get(f"aws kms list-aliases --region {region}")
        alias_names = [
            a["AliasName"] for a in (kms_aliases.get("Aliases") or [])
            if not a["AliasName"].startswith("alias/aws/")
        ] if kms_aliases else []
        if alias_names:
            lines.append(f"  Existing customer-managed KMS Key Aliases: {', '.join(alias_names)}")
        else:
            lines.append("  Existing customer-managed KMS Key Aliases: (none found — will use AWS-managed keys by default)")

        lambdas = results.get(f"aws lambda list-functions --region {region}")
        fn_names = [f["FunctionName"] for f in (lambdas.get("Functions") or [])] if lambdas else []
        if fn_names:
            lines.append(f"  Existing Lambda Functions: {', '.join(fn_names)}")
        else:
            lines.append("  Existing Lambda Functions: (none found)")

        lines.append(
            "\nIf an ID is provided in the context above (VPC, Subnets, Security Groups, Key Pairs, Route53 Zones, AMI, RDS Subnet Groups, Elastic IPs, NAT Gateways, ACM Certs, KMS Aliases), hardcode it directly in your Terraform code to avoid data source filter errors. "
            "ONLY use Terraform data sources (e.g. data \"aws_vpc\") if the required resource is marked as '(none found)' or is missing from the context.\n"
            "\nTERRAFORM GOLDEN RULES:\n"
            "1. S3 Buckets: Names must be globally unique. Always use random_id or random_pet to append a suffix to bucket names.\n"
            "2. IAM Roles/Policies: Always use name_prefix instead of name to avoid conflicts with existing roles.\n"
            "3. EC2/RDS Security Groups: Prefer using existing security groups if they match your needs.\n"
            "4. Circular Dependencies: Never make a Security Group depend on an EC2 instance's IP if the EC2 instance also depends on that Security Group.\n"
            "5. Hardcoding: Hardcode environment IDs only if provided above. NEVER hardcode ARNs or Regions.\n"
            "6. Stateful Resources: Always set lifecycle { prevent_destroy = true } for RDS/DynamoDB/S3 unless instructed otherwise.\n"
            "7. Provider Version: hashicorp/aws ~> 5.0.\n"
            "8. Local Files: use the Terraform local_file resource instead of shell commands.\n"
            "9. AMIs: pick the catalog entry matching the target OS/arch (AL2 vs AL2023, x86_64 vs arm64/Graviton, full vs minimal) and hardcode that AMI ID. Only use a data \"aws_ami\" lookup if the AMI Catalog above is empty or the OS you need isn't in it.\n"
            "10. RDS: reuse an existing DB Subnet Group if listed and spans the needed AZs.\n"
            "11. Elastic IPs: reuse an unassociated EIP if listed.\n"
            "12. HTTPS/ACM: provision with DNS validation or default to HTTP-only and flag it.\n"
            "13. Subnet CIDRs: must be non-overlapping sub-blocks of the VPC CIDR; use cidrsubnet().\n"
            "14. CloudFront + ACM: cert MUST be in us-east-1 regardless of deployment region.\n"
            "15. VPC selection: prefer [DEFAULT] VPC when ambiguous, else ask.\n"
            "16. Public vs private subnets: trust the computed PUBLIC/PRIVATE label above.\n"
            "17. Subnet IP exhaustion: check AvailableIPs before placing IP-hungry resources.\n"
            "18. Private subnet AWS service access: verify NAT Gateway or VPC Endpoint exists before placing resources there."
        )
        ctx_str = "\n".join(lines)
        logger.info("Dynamic Grounding: _gather_aws_context output generated (%d chars)", len(ctx_str))
        return ctx_str
    except Exception as exc:
        logger.warning("aws_context.failed (processing phase): %s", exc)
        return ""


def _gather_aws_context() -> str:
    """Sync entry point — same signature as before, just delegates to the async
    version on the persistent session's event loop. First call in the process
    pays MCP startup cost; every call after that doesn't."""
    return _session.run_coro(_gather_aws_context_async)


# ============== RUN ==============

if __name__ == "__main__" or True:
    print("Running _gather_aws_context() — this fires 24 MCP calls concurrently, no cap...\n")

    overall_start = time.perf_counter()
    overall_start_stamp = time.strftime("%H:%M:%S")
    print(f"[OVERALL START] {overall_start_stamp}\n")

    output = _gather_aws_context()

    overall_end = time.perf_counter()
    overall_end_stamp = time.strftime("%H:%M:%S")
    overall_elapsed = overall_end - overall_start

    print(f"\n[OVERALL END]   {overall_end_stamp}")
    print(f"[OVERALL TOTAL] {overall_elapsed:.2f}s ({overall_elapsed/60:.2f} min)\n")

    print("=" * 70)
    print("PER-COMMAND TIMING SUMMARY (slowest first)")
    print("=" * 70)
    for cmd, secs in sorted(_TIMING_LOG, key=lambda x: -x[1]):
        print(f"  {secs:6.2f}s   {cmd}")
    print(f"\n  Total calls: {len(_TIMING_LOG)}")
    print(f"  Sum of individual call times: {sum(s for _, s in _TIMING_LOG):.2f}s")
    print(f"  Wall-clock total: {overall_elapsed:.2f}s")

    print("\n" + "=" * 70)
    print("FULL AWS CONTEXT OUTPUT")
    print("=" * 70)
    print(output)
    print(f"\nTotal length: {len(output)} characters")
