"""Render tests of the chart for dev and prod (task 4.6). They call `helm template` and `helm lint`, and are
skipped with a clear message when helm is not on PATH."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

import pytest
from chart_support import (
    CLIENT_ID,
    DIGEST,
    HELM_MISSING,
    POOL_ID,
    REPO,
    REPOSITORY,
    WAF_ARN,
    Doc,
    find_helm,
    helm_args,
    named,
    of_kind,
    pod_specs,
    render,
    run_helm,
)

pytestmark = pytest.mark.skipif(find_helm() is None, reason=HELM_MISSING)

ENVS = ["dev", "prod"]
HTTP = ["add", "sub", "mul", "div", "history"]
WORKER = "ingest"
ROUTES = {name: [f"/api/v1/{name}", f"/api/eks/v1/{name}"] for name in HTTP}
ANN = "alb.ingress.kubernetes.io/"
TAG_KEYS = ["team-owner", "project-name", "app-name", "repo-name", "env-type"]
POLICIES = {"default-deny-all", *(f"{name}-network" for name in [*HTTP, WORKER, "migrate"])}


@pytest.fixture(scope="module", params=ENVS)
def env(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def docs(env: str) -> list[Doc]:
    return render(env)


def anns(doc: Doc) -> dict[str, str]:
    return dict(doc["metadata"]["annotations"])


def containers(docs: list[Doc]) -> list[tuple[str, Doc]]:
    return [(name, c) for name, spec in pod_specs(docs) for c in spec["containers"]]


def test_helm_lint_passes(env: str) -> None:
    result = run_helm("lint", helm_args(env))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 chart(s) failed" in result.stdout


def test_every_object_is_in_the_calculator_namespace(docs: list[Doc]) -> None:
    for doc in docs:
        if doc["kind"] == "Namespace":
            assert doc["metadata"]["name"] == "calculator"
        else:
            assert doc["metadata"]["namespace"] == "calculator", doc["kind"]


def test_namespace_enforces_restricted_pod_security(docs: list[Doc]) -> None:
    labels = of_kind(docs, "Namespace")[0]["metadata"]["labels"]
    for mode in ("enforce", "audit", "warn"):
        assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"


def test_resource_quota_and_limit_range(docs: list[Doc]) -> None:
    hard = named(docs, "ResourceQuota", "calculator-quota")["spec"]["hard"]
    assert set(hard) == {"pods", "requests.cpu", "requests.memory", "limits.cpu", "limits.memory"}
    container = named(docs, "LimitRange", "calculator-limits")["spec"]["limits"][0]
    assert container["type"] == "Container"
    assert {"default", "defaultRequest", "min", "max"} <= set(container)


def test_service_accounts_carry_only_the_helm_ownership_annotations(docs: list[Doc]) -> None:
    accounts = of_kind(docs, "ServiceAccount")
    assert sorted(a["metadata"]["name"] for a in accounts) == ["calc", "history", "ingest", "migrate"]
    for account in accounts:
        # Pod Identity needs no annotation (never a role ARN here); the Helm ownership metadata lets the release
        # adopt accounts that were applied before it
        assert set(account["metadata"]["annotations"]) == {
            "meta.helm.sh/release-name",
            "meta.helm.sh/release-namespace",
        }
        assert account["automountServiceAccountToken"] is False


def test_workloads_use_the_right_account_and_argument(docs: list[Doc]) -> None:
    deployments = {d["metadata"]["name"]: d for d in of_kind(docs, "Deployment")}
    assert sorted(deployments) == sorted([*HTTP, WORKER])
    accounts = {"history": "history", WORKER: "ingest"}
    for name, deployment in deployments.items():
        spec = deployment["spec"]["template"]["spec"]
        assert spec["serviceAccountName"] == accounts.get(name, "calc")
        assert spec["containers"][0]["args"] == [name]


def test_security_context_of_every_pod(docs: list[Doc]) -> None:
    for name, spec in pod_specs(docs):
        pod = spec["securityContext"]
        assert pod["runAsNonRoot"] is True and pod["runAsUser"] > 0, name
        assert pod["seccompProfile"] == {"type": "RuntimeDefault"}, name
        assert spec["automountServiceAccountToken"] is False, name
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            assert key not in spec, name
        for container in spec["containers"]:
            sc = container["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, name
            assert sc["readOnlyRootFilesystem"] is True, name
            assert sc["runAsNonRoot"] is True, name
            assert sc["privileged"] is False, name
            assert sc["capabilities"] == {"drop": ["ALL"]}, name
            assert sc["seccompProfile"] == {"type": "RuntimeDefault"}, name
        volumes = {v["name"]: v for v in spec["volumes"]}
        assert set(volumes) == {"tmp"} and "emptyDir" in volumes["tmp"], name
        assert [m["mountPath"] for m in spec["containers"][0]["volumeMounts"]] == ["/tmp"], name


def test_images_are_digests_without_tags(docs: list[Doc]) -> None:
    images = [c["image"] for _, c in containers(docs)]
    assert images
    for image in images:
        assert image == f"{REPOSITORY}@{DIGEST}"
        assert re.fullmatch(r"[^@:]+(:[0-9]+)?/[^@:]+@sha256:[0-9a-f]{64}", image)


def test_probes_use_https_health(docs: list[Doc]) -> None:
    for deployment in of_kind(docs, "Deployment"):
        if deployment["metadata"]["name"] == WORKER:
            continue
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        for probe in ("readinessProbe", "livenessProbe"):
            http_get = container[probe]["httpGet"]
            assert http_get == {"path": "/health", "port": "https", "scheme": "HTTPS"}
        assert container["ports"] == [{"name": "https", "containerPort": 8443, "protocol": "TCP"}]


def test_requests_and_limits_on_every_container(docs: list[Doc]) -> None:
    for name, container in containers(docs):
        res = container["resources"]
        for section in ("requests", "limits"):
            assert res[section]["cpu"] and res[section]["memory"], name
    for deployment in of_kind(docs, "Deployment"):
        if deployment["metadata"]["name"] in {"add", "sub", "mul", "div"}:
            assert deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] == "250m"


def test_topology_spread_across_zones(docs: list[Doc]) -> None:
    for deployment in of_kind(docs, "Deployment"):
        constraint = deployment["spec"]["template"]["spec"]["topologySpreadConstraints"][0]
        assert constraint["topologyKey"] == "topology.kubernetes.io/zone"
        assert constraint["labelSelector"]["matchLabels"] == deployment["spec"]["selector"]["matchLabels"]


def test_environment_variables_are_plain_and_complete(docs: list[Doc]) -> None:
    expected = {
        "AWS_REGION",
        "ENVIRONMENT",
        "ALERTS_TOPIC_ARN",
        "APP_SECRET_ARN",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "LOG_LEVEL",
        "SSL_ROOT_CERT",
    }
    extra = {
        "history": {"ALLOWED_ORIGIN", "CURSOR_KEY_SECRET_ARN"},
        WORKER: {"INGEST_QUEUE_URL", "INGEST_BUCKET"},
    }
    for deployment in of_kind(docs, "Deployment"):
        env = {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        name = deployment["metadata"]["name"]
        assert set(env) == expected | extra.get(name, {"ALLOWED_ORIGIN"})
        assert all(set(e) == {"name", "value"} for e in env.values())
        assert env["DB_PORT"]["value"] == "5432"
        assert env["SSL_ROOT_CERT"]["value"] == "/app/db-trust-bundle.pem"


def test_every_workload_and_the_migrate_job_get_the_environment_and_log_level_info(env: str, docs: list[Doc]) -> None:
    pods = [*of_kind(docs, "Deployment"), *of_kind(docs, "Job")]
    assert len(pods) == len(HTTP) + 2  # the HTTP workloads, the ingest worker and the migrate job
    for workload in pods:
        values = {e["name"]: e["value"] for e in workload["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert values["ENVIRONMENT"] == env, workload["metadata"]["name"]
        assert values["LOG_LEVEL"] == "INFO", workload["metadata"]["name"]  # DEBUG only by an explicit owner override


def test_services_are_cluster_ip_on_8443(docs: list[Doc]) -> None:
    services = of_kind(docs, "Service")
    assert sorted(s["metadata"]["name"] for s in services) == sorted(HTTP)
    for service in services:
        assert service["spec"]["type"] == "ClusterIP"
        assert service["spec"]["ports"][0]["port"] == 8443


def test_pod_disruption_budget_only_above_one_replica(env: str, docs: list[Doc]) -> None:
    budgets = of_kind(docs, "PodDisruptionBudget")
    minimum = {d["metadata"]["name"]: d["spec"]["minReplicas"] for d in of_kind(docs, "HorizontalPodAutoscaler")}
    minimum[WORKER] = worker(docs)["spec"]["replicas"]
    assert sorted(b["metadata"]["name"] for b in budgets) == sorted(n for n, r in minimum.items() if r > 1)
    assert (len(budgets) == 5) == (env == "prod")  # the worker never has one


def test_budgets_follow_fixed_replicas_when_autoscaling_is_off(env: str) -> None:
    docs = render(env, {"autoscaling.enabled": False}, drop=())
    assert not of_kind(docs, "HorizontalPodAutoscaler")
    replicas = {d["metadata"]["name"]: d["spec"]["replicas"] for d in of_kind(docs, "Deployment")}
    assert all(replicas[name] == (2 if env == "prod" else 1) for name in HTTP)
    assert len(of_kind(docs, "PodDisruptionBudget")) == (5 if env == "prod" else 0)


def test_migrate_job_exists_once_and_is_one_shot(docs: list[Doc]) -> None:
    jobs = of_kind(docs, "Job")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["metadata"]["name"] == "migrate-1"  # the release revision of `helm template`
    spec = job["spec"]
    assert spec["backoffLimit"] == 0 and spec["ttlSecondsAfterFinished"] > 0
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never" and pod["serviceAccountName"] == "migrate"
    container = pod["containers"][0]
    assert container["args"] == ["migrate"]
    env = {e["name"] for e in container["env"]}
    assert {"MASTER_SECRET_ARN", "DB_INSTANCE_HOST", "DB_HOST", "APP_SECRET_ARN"} <= env
    assert "probes" not in pod and "readinessProbe" not in container


def test_migrate_job_name_follows_the_run_id(env: str) -> None:
    docs = render(env, {"migrate.runId": "run-42"})
    assert of_kind(docs, "Job")[0]["metadata"]["name"] == "migrate-run-42"
    assert of_kind(render(env, {"migrate.enabled": False}), "Job") == []


def policy_map(docs: list[Doc]) -> dict[str, Doc]:
    return {p["metadata"]["name"]: p["spec"] for p in of_kind(docs, "NetworkPolicy")}


def cidrs_of(rule: Doc, key: str) -> list[str]:
    return [peer["ipBlock"]["cidr"] for peer in rule[key]]


def vpc_cidr(env: str) -> str:
    parameters = json.loads((REPO / "parameters" / f"{env}.json").read_text(encoding="utf-8"))
    return str(parameters["Parameters"]["VpcCidr"])


def parameter_slices(env: str, indexes: tuple[int, ...]) -> list[str]:
    """Fn::Cidr [VpcCidr, 13, 8] of templates/network.yaml: the /24 slices of the VPC range."""
    subnets = list(ipaddress.ip_network(vpc_cidr(env)).subnets(new_prefix=24))
    return [str(subnets[i]) for i in indexes]


def test_default_deny_and_one_policy_per_workload(docs: list[Doc]) -> None:
    policies = policy_map(docs)
    assert set(policies) == POLICIES
    deny = policies["default-deny-all"]
    assert deny["podSelector"] == {} and deny["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny and "egress" not in deny
    for name in [*HTTP, WORKER, "migrate"]:  # each policy selects exactly one workload and filters both directions
        spec = policies[f"{name}-network"]
        assert spec["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == name
        assert spec["policyTypes"] == ["Ingress", "Egress"]
    # every pod template carries the labels its policy selects
    selected = {name: policies[f"{name}-network"]["podSelector"]["matchLabels"] for name in [*HTTP, WORKER]}
    for deployment in of_kind(docs, "Deployment"):
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        name = deployment["metadata"]["name"]
        assert selected[name].items() <= labels.items()
    job_labels = of_kind(docs, "Job")[0]["spec"]["template"]["metadata"]["labels"]
    assert policies["migrate-network"]["podSelector"]["matchLabels"].items() <= job_labels.items()


def test_release_without_the_job_still_carries_the_migrate_policy(env: str, docs: list[Doc]) -> None:
    """The release is installed with migrate.enabled=false; the policy must still be in it, or the next job (which
    runs before the upgrade, under default-deny-all) has no egress."""
    release_docs = render(env, {"migrate.enabled": False})
    assert not of_kind(release_docs, "Job")
    assert set(policy_map(release_docs)) == POLICIES
    assert policy_map(release_docs)["migrate-network"] == policy_map(docs)["migrate-network"]


def test_migrate_policy_carries_the_helm_ownership_annotations(env: str) -> None:
    for overrides in ({}, {"migrate.enabled": False}):
        policy = named(render(env, overrides), "NetworkPolicy", "migrate-network")
        assert policy["metadata"]["annotations"] == {
            "meta.helm.sh/release-name": "calculator",
            "meta.helm.sh/release-namespace": "calculator",
        }


@pytest.mark.parametrize("name", HTTP)
def test_http_workload_ingress_is_only_the_load_balancer_subnets_on_the_pod_port(
    env: str, docs: list[Doc], name: str
) -> None:
    ingress = policy_map(docs)[f"{name}-network"]["ingress"]
    assert len(ingress) == 1
    assert ingress[0]["ports"] == [{"protocol": "TCP", "port": 8443}]
    assert [peer.keys() for peer in ingress[0]["from"]] == [{"ipBlock"}] * 2  # no pod, namespace or other selector
    assert cidrs_of(ingress[0], "from") == parameter_slices(env, (1, 2))
    assert all("except" not in peer["ipBlock"] for peer in ingress[0]["from"])


@pytest.mark.parametrize("name", [WORKER, "migrate"])
def test_worker_and_job_accept_no_traffic(docs: list[Doc], name: str) -> None:
    spec = policy_map(docs)[f"{name}-network"]
    assert "Ingress" in spec["policyTypes"] and "ingress" not in spec


def test_no_policy_admits_the_pod_subnets_or_the_whole_vpc_to_the_api_pods(env: str, docs: list[Doc]) -> None:
    vpc = ipaddress.ip_network(vpc_cidr(env))
    private = [ipaddress.ip_network(c) for c in parameter_slices(env, (11, 12))]  # nodes and pods live here
    public = {ipaddress.ip_network(c) for c in parameter_slices(env, (1, 2))}
    reaching_api = 0
    for doc in of_kind(docs, "NetworkPolicy"):
        spec = doc["spec"]
        selector = spec["podSelector"].get("matchLabels", {})
        # policies add up: count every policy that selects all pods or any API pod, whatever its name
        if selector and selector.get("app.kubernetes.io/name") not in HTTP:
            continue
        for rule in spec.get("ingress", []):
            reaching_api += 1
            assert rule.get("ports"), f"{doc['metadata']['name']}: ingress with no port restriction"
            for peer in rule["from"]:
                assert set(peer) == {"ipBlock"}, f"{doc['metadata']['name']}: source other than an ipBlock"
                source = ipaddress.ip_network(peer["ipBlock"]["cidr"])
                assert source in public, f"{doc['metadata']['name']}: {source} is not a load balancer subnet"
                assert source != vpc and not any(source.overlaps(p) for p in private)
    assert reaching_api == len(HTTP)


COREDNS_PEER = [
    {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
    }
]


@pytest.mark.parametrize("name", [*HTTP, WORKER, "migrate"])
def test_each_workload_egress_is_dns_database_aws_and_pod_identity_only(env: str, docs: list[Doc], name: str) -> None:
    egress = policy_map(docs)[f"{name}-network"]["egress"]
    assert len(egress) == 4
    by_port = {tuple((p["protocol"], p["port"]) for p in rule["ports"]): rule for rule in egress}
    assert set(by_port) == {(("UDP", 53), ("TCP", 53)), (("TCP", 5432),), (("TCP", 443),), (("TCP", 80),)}
    dns = by_port[(("UDP", 53), ("TCP", 53))]
    assert dns["to"] == COREDNS_PEER  # CoreDNS pods only, not the whole namespace
    database = by_port[(("TCP", 5432),)]
    assert cidrs_of(database, "to") == parameter_slices(env, (11, 12))  # private subnets, not the whole VPC
    aws = by_port[(("TCP", 443),)]["to"]
    assert aws == [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [vpc_cidr(env)]}}]  # the AWS APIs, never another pod
    identity = by_port[(("TCP", 80),)]["to"]
    assert identity == [{"ipBlock": {"cidr": "169.254.170.23/32"}}]


def test_egress_rules_cannot_be_unknown_or_missing() -> None:
    unknown = run_helm("template", [*helm_args("dev"), "--set", "workloads.add.egress[0]=anything"])
    assert unknown.returncode != 0 and "unknown egress rule" in unknown.stderr
    empty = run_helm("template", [*helm_args("dev"), "--set", "workloads.add.egress=null"])
    assert empty.returncode != 0 and "add needs a non-empty egress list" in empty.stderr


@pytest.mark.parametrize("key", ["network.albCidrs", "network.databaseCidrs"])
def test_missing_subnet_cidrs_stop_the_render(key: str) -> None:
    result = run_helm("template", [*helm_args("prod"), "--set", f"{key}=null"])
    assert result.returncode != 0 and key in result.stderr


def test_no_secret_hostpath_or_privileged_anywhere(docs: list[Doc]) -> None:
    assert of_kind(docs, "Secret") == []
    text = json.dumps(docs)
    for forbidden in ("hostPath", "secretKeyRef", '"privileged": true', "hostNetwork", ":latest"):
        assert forbidden not in text


def ingresses(docs: list[Doc]) -> tuple[Doc, Doc]:
    return named(docs, "Ingress", "calculator-preflight"), named(docs, "Ingress", "calculator-api")


def paths(ingress: Doc) -> list[tuple[str, str]]:
    return [(p["path"], p["backend"]["service"]["name"]) for p in ingress["spec"]["rules"][0]["http"]["paths"]]


def test_two_ingresses_in_one_group_with_the_contract_routes(docs: list[Doc]) -> None:
    assert len(of_kind(docs, "Ingress")) == 2
    preflight, api = ingresses(docs)
    expected = sorted((path, name) for name, routes in ROUTES.items() for path in routes)
    assert sorted(paths(preflight)) == expected and sorted(paths(api)) == expected
    for ingress in (preflight, api):
        assert ingress["spec"]["ingressClassName"] == "alb"
        assert all(p["pathType"] == "Exact" for p in ingress["spec"]["rules"][0]["http"]["paths"])
        assert "defaultBackend" not in ingress["spec"]  # the controller builds the fixed 404 default itself
    group = {anns(i)[ANN + "group.name"] for i in (preflight, api)}
    assert len(group) == 1
    assert int(anns(preflight)[ANN + "group.order"]) < int(anns(api)[ANN + "group.order"])


def test_jwt_validation_on_every_non_options_path(docs: list[Doc]) -> None:
    preflight, api = ingresses(docs)
    jwt = json.loads(anns(api)[ANN + "jwt-validation"])
    issuer = f"https://cognito-idp.us-east-2.amazonaws.com/{POOL_ID}"
    assert jwt["issuer"] == issuer and jwt["jwksEndpoint"] == f"{issuer}/.well-known/jwks.json"
    claims = {c["name"]: c for c in jwt["additionalClaims"]}
    assert claims["aud"]["values"] == [CLIENT_ID] and claims["token_use"]["values"] == ["id"]
    assert all(c["format"] == "single-string" for c in claims.values())
    # every path of the authenticated Ingress is routed with no method condition: any method needs the token
    assert not [key for key in anns(api) if key.startswith(ANN + "conditions.")]
    assert len(paths(api)) == 10
    # the unauthenticated Ingress has no validation and only OPTIONS conditions, one per service it routes to
    assert ANN + "jwt-validation" not in anns(preflight)
    services = {name for _, name in paths(preflight)}
    conditions = {k.removeprefix(ANN + "conditions."): v for k, v in anns(preflight).items() if "conditions." in k}
    assert set(conditions) == services
    for value in conditions.values():
        assert json.loads(value) == [
            {"field": "http-request-method", "httpRequestMethodConfig": {"Values": ["OPTIONS"]}}
        ]


def test_load_balancer_annotations(docs: list[Doc]) -> None:
    preflight, api = ingresses(docs)
    exclusive = [
        "scheme",
        "target-type",
        "backend-protocol",
        "healthcheck-protocol",
        "healthcheck-path",
        "listen-ports",
        "ssl-redirect",
        "ssl-policy",
        "certificate-arn",
        "security-groups",
        "manage-backend-security-group-rules",
        "load-balancer-attributes",
        "tags",
    ]
    for key in exclusive:  # Exclusive annotations of an IngressGroup must have the same value everywhere
        assert anns(preflight)[ANN + key] == anns(api)[ANN + key], key
    ann = anns(api)
    assert ann[ANN + "scheme"] == "internet-facing" and ann[ANN + "target-type"] == "ip"
    assert ann[ANN + "backend-protocol"] == "HTTPS" and ann[ANN + "healthcheck-protocol"] == "HTTPS"
    assert ann[ANN + "healthcheck-path"] == "/health"
    assert ann[ANN + "ssl-policy"] == "ELBSecurityPolicy-TLS13-1-2-2021-06"
    assert ann[ANN + "ssl-redirect"] == "443"
    assert json.loads(ann[ANN + "listen-ports"]) == [{"HTTP": 80}, {"HTTPS": 443}]
    assert ann[ANN + "certificate-arn"].startswith("arn:aws:acm:")
    assert ann[ANN + "security-groups"] == "sg-0123456789abcdef0"
    attributes = dict(item.split("=", 1) for item in ann[ANN + "load-balancer-attributes"].split(","))
    assert attributes["routing.http.drop_invalid_header_fields.enabled"] == "true"
    assert attributes["access_logs.s3.enabled"] == "true" and attributes["access_logs.s3.bucket"] == "example-alb-logs"


def test_tags_annotation_has_the_five_mandatory_tags(env: str, docs: list[Doc]) -> None:
    for ingress in ingresses(docs):
        tags = dict(item.split("=", 1) for item in anns(ingress)[ANN + "tags"].split(","))
        assert list(tags) == TAG_KEYS
        assert tags["env-type"] == env and all(tags[key] for key in TAG_KEYS)


def test_waf_only_in_prod(env: str, docs: list[Doc]) -> None:
    for ingress in ingresses(docs):
        waf = anns(ingress).get(ANN + "wafv2-acl-arn")
        assert waf == (WAF_ARN if env == "prod" else None)


def test_prod_requires_a_waf_arn() -> None:
    result = run_helm("template", helm_args("prod", {"ingress.wafAclArn": ""}))
    assert result.returncode != 0 and "wafAclArn" in result.stderr


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"image.digest": ""}, "image.digest is required"),
        ({"image.digest": "latest"}, "sha256"),
        ({"image.repository": "registry.example.invalid/calc/app:v1"}, "must not contain a tag"),
        ({"image.repository": "registry.example.invalid/calc/app@sha256:" + "b" * 64}, "must not contain a tag"),
        ({"migrate.runId": "Bad_Id"}, "migrate.runId"),
    ],
)
def test_bad_deployment_values_stop_the_render(overrides: dict[str, str | bool], message: str) -> None:
    result = run_helm("template", helm_args("dev", overrides))
    assert result.returncode != 0 and message in result.stderr


@pytest.mark.parametrize(
    "missing", ["ingress.certificateArn", "ingress.securityGroupId", "auth.appClientId", "app.dbHost"]
)
def test_missing_deployment_values_stop_the_render(missing: str) -> None:
    result = run_helm("template", helm_args("dev", drop=(missing,)))
    assert result.returncode != 0 and missing in result.stderr


def test_release_namespace_must_be_calculator() -> None:
    args = helm_args("dev")
    args[args.index("calculator", args.index("--namespace"))] = "default"
    result = run_helm("template", args)
    assert result.returncode != 0 and "must be" in result.stderr


def test_dev_and_prod_differ_only_by_environment_values() -> None:
    dev, prod = render("dev"), render("prod")

    def shape(docs: list[Doc]) -> Any:
        return sorted((d["kind"], d["metadata"]["name"]) for d in docs if d["kind"] != "PodDisruptionBudget")

    assert shape(dev) == shape(prod)


def test_access_logs_are_omitted_when_no_bucket_is_given(env: str) -> None:
    """The log bucket arrives with the storage stack (task 5.1): until then the load balancer has no access logs."""
    for ingress in ingresses(render(env, drop=("ingress.accessLogs.bucket",))):
        attributes = dict(item.split("=", 1) for item in anns(ingress)[ANN + "load-balancer-attributes"].split(","))
        assert attributes == {"routing.http.drop_invalid_header_fields.enabled": "true"}


def test_only_the_history_workload_gets_the_cursor_secret_and_it_is_required(env: str, docs: list[Doc]) -> None:
    for deployment in of_kind(docs, "Deployment"):
        names = {e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert ("CURSOR_KEY_SECRET_ARN" in names) == (deployment["metadata"]["name"] == "history")
    for job in of_kind(docs, "Job"):
        assert "CURSOR_KEY_SECRET_ARN" not in json.dumps(job)
    result = run_helm("template", helm_args(env, drop=("cursor.secretArn",)))
    assert result.returncode != 0 and "cursor.secretArn is required" in result.stderr


def worker(docs: list[Doc]) -> Doc:
    return named(docs, "Deployment", WORKER)


def test_worker_has_one_replica_in_every_environment_and_no_service_budget_or_route(env: str, docs: list[Doc]) -> None:
    assert worker(docs)["spec"]["replicas"] == 1  # prod runs two replicas of the HTTP pods, not of the worker
    assert WORKER not in {s["metadata"]["name"] for s in of_kind(docs, "Service")}
    assert WORKER not in {b["metadata"]["name"] for b in of_kind(docs, "PodDisruptionBudget")}
    for ingress in ingresses(docs):
        assert not any(f"/{WORKER}" in path for path, _ in paths(ingress))
        assert not any(backend == WORKER for _, backend in paths(ingress))
        assert f"{ANN}conditions.{WORKER}" not in anns(ingress)


def test_worker_pod_is_restricted_has_no_port_or_probe_and_uses_its_pod_identity_account(docs: list[Doc]) -> None:
    pod = worker(docs)["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "ingest" and pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["args"] == [WORKER]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    for key in ("ports", "readinessProbe", "livenessProbe"):
        assert key not in container, key
    labels = worker(docs)["spec"]["template"]["metadata"]["labels"]
    # not "api": the policy that lets the load balancer in selects that component, so nothing can reach the worker
    assert labels["app.kubernetes.io/component"] == "worker"


def test_worker_reads_queue_and_bucket_from_the_deployment_values(env: str, docs: list[Doc]) -> None:
    values = {e["name"]: e["value"] for e in worker(docs)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert values["INGEST_QUEUE_URL"].startswith("https://sqs.us-east-2.amazonaws.com/")
    assert values["INGEST_BUCKET"] == "example-ingest-bucket" and values["ENVIRONMENT"] == env
    assert values["DB_HOST"] == "proxy.example.invalid" and "MASTER_SECRET_ARN" not in values


@pytest.mark.parametrize("missing", ["ingest.queueUrl", "ingest.bucketName"])
def test_the_worker_needs_queue_and_bucket(missing: str) -> None:
    result = run_helm("template", helm_args("dev", drop=(missing,)))
    assert result.returncode != 0 and missing in result.stderr


def test_load_balancer_logs_go_to_the_alb_prefix(docs: list[Doc]) -> None:
    for ingress in ingresses(docs):
        attributes = dict(item.split("=", 1) for item in anns(ingress)[ANN + "load-balancer-attributes"].split(","))
        assert attributes["access_logs.s3.prefix"] == "alb"  # the only prefix the bucket policy accepts


def test_one_autoscaler_per_http_workload_with_the_cpu_target_and_window(env: str, docs: list[Doc]) -> None:
    autoscalers = of_kind(docs, "HorizontalPodAutoscaler")
    assert sorted(a["metadata"]["name"] for a in autoscalers) == sorted(HTTP)  # never the worker
    for hpa in autoscalers:
        spec = hpa["spec"]
        assert spec["scaleTargetRef"] == {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": hpa["metadata"]["name"],
        }
        assert spec["minReplicas"] == (2 if env == "prod" else 1) and spec["maxReplicas"] == 4
        assert spec["metrics"] == [
            {
                "type": "Resource",
                "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 50}},
            }
        ]
        assert spec["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 300


def test_scaled_deployments_have_no_replicas_field_and_the_worker_keeps_one(docs: list[Doc]) -> None:
    for deployment in of_kind(docs, "Deployment"):
        name = deployment["metadata"]["name"]
        if name in HTTP:
            assert "replicas" not in deployment["spec"], name
        else:
            assert deployment["spec"]["replicas"] == 1 and name == WORKER


def _amount(text: str) -> float:
    """CPU in cores or memory in MiB, from a Kubernetes quantity (m, Mi, Gi or a plain number)."""
    for suffix, factor in (("Gi", 1024.0), ("Mi", 1.0), ("m", 0.001)):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text)


def test_quota_fits_every_workload_at_maximum_replicas_plus_one_surge_pod(docs: list[Doc]) -> None:
    hard = named(docs, "ResourceQuota", "calculator-quota")["spec"]["hard"]
    maximum = {a["metadata"]["name"]: a["spec"]["maxReplicas"] for a in of_kind(docs, "HorizontalPodAutoscaler")}
    totals = dict.fromkeys(hard, 0.0)

    def add(pods: int, resources: Doc) -> None:
        totals["pods"] += pods
        for section in ("requests", "limits"):
            for kind in ("cpu", "memory"):
                totals[f"{section}.{kind}"] += pods * _amount(resources[section][kind])

    for deployment in of_kind(docs, "Deployment"):
        name = deployment["metadata"]["name"]
        pods = maximum[name] + 1 if name in maximum else deployment["spec"]["replicas"]  # surge: maxSurge 1
        add(pods, deployment["spec"]["template"]["spec"]["containers"][0]["resources"])
    add(1, of_kind(docs, "Job")[0]["spec"]["template"]["spec"]["containers"][0]["resources"])  # migrate pod running
    for key, used in totals.items():
        assert used <= _amount(hard[key]), f"{key}: {used} is above the quota {hard[key]}"
