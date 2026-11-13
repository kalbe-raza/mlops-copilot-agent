"""Static checks that the Kubernetes manifests meet the deployment requirements."""

from __future__ import annotations

from pathlib import Path

import yaml

K8S = Path(__file__).resolve().parents[1] / "kubernetes"


def _load(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((K8S / name).read_text()) if d]


def test_deployment_replicas_probes_and_resources():
    (dep,) = _load("deployment.yaml")
    assert dep["kind"] == "Deployment" and dep["spec"]["replicas"] >= 2
    (container,) = dep["spec"]["template"]["spec"]["containers"]
    assert container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health/live"
    for kind in ("requests", "limits"):
        assert {"cpu", "memory"} <= set(container["resources"][kind])
    assert container["envFrom"][0]["configMapRef"]["name"] == "inference-api-config"
    assert container["securityContext"]["allowPrivilegeEscalation"] is False


def test_service_is_cluster_ip_and_matches_pods():
    (svc,) = _load("service.yaml")
    (dep,) = _load("deployment.yaml")
    assert svc["spec"]["type"] == "ClusterIP"
    assert svc["spec"]["selector"].items() <= dep["spec"]["template"]["metadata"]["labels"].items()
    assert svc["spec"]["ports"][0]["name"] == "http"


def test_configmap_holds_only_non_sensitive_settings():
    (cm,) = _load("configmap.yaml")
    assert cm["kind"] == "ConfigMap"
    assert not any(word in key.lower() for key in cm["data"] for word in ("password", "secret", "token", "key"))


def test_agent_rbac_is_namespace_scoped_and_least_privilege():
    docs = {d["kind"]: d for d in _load("rbac.yaml")}
    assert "ClusterRole" not in docs and "ClusterRoleBinding" not in docs
    rules = docs["Role"]["rules"]
    verbs = {v for r in rules for v in r["verbs"]}
    assert not verbs & {"delete", "create", "*", "deletecollection"}
    for rule in rules:
        if "patch" in rule["verbs"]:
            assert rule["resourceNames"] == ["inference-api"]
