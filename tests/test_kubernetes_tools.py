"""Kubernetes tools with mocked API clients: safety policy, diagnostics and failure handling."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from agent.schemas import ToolStatus
from agent.tools import build_registry
from agent.tools.base import ToolExecutor
from agent.tools.kubernetes_tools import KubernetesTools


def _container_status(ready=True, restarts=0, waiting=None, last_reason=None):
    state = NS(waiting=NS(reason=waiting, message=f"{waiting} msg") if waiting else None, terminated=None,
               running=None if waiting else NS(started_at=datetime(2026, 11, 13, tzinfo=UTC)))
    last = NS(terminated=NS(reason=last_reason, exit_code=1, finished_at=None) if last_reason else None)
    return NS(name="inference-api", image="mlops-inference-api:1.0.0", ready=ready, restart_count=restarts,
              state=state, last_state=last)


def _pod(name, phase="Running", ready=True, restarts=0, waiting=None, last_reason=None):
    return NS(
        metadata=NS(name=name, labels={"app.kubernetes.io/version": "1.0.0"}),
        spec=NS(containers=[NS(name="inference-api")], node_name="node-1"),
        status=NS(phase=phase, pod_ip="10.0.0.1", start_time=None,
                  container_statuses=[_container_status(ready, restarts, waiting, last_reason)],
                  conditions=[NS(type="Ready", status=str(ready), reason=None if ready else "ContainersNotReady",
                                 message=None)]),
    )


def _event(reason, obj, message):
    return NS(type="Warning", reason=reason, involved_object=NS(kind="Pod", name=obj), message=message, count=3,
              last_timestamp=datetime(2026, 11, 13, 12, tzinfo=UTC), event_time=None,
              metadata=NS(creation_timestamp=None))


def _tools(settings, core=None, apps=None):
    return KubernetesTools(settings, core=core or MagicMock(), apps=apps or MagicMock(), discovery=MagicMock())


def _executor(settings, logger, k8s, confirm=None):
    registry = build_registry(settings, dvc=MagicMock(), mlflow=MagicMock(), airflow=MagicMock(), k8s=k8s)
    return ToolExecutor(registry, logger, 5.0, confirm)


def test_restart_in_unapproved_namespace_is_rejected_without_api_call(settings, logger):
    apps = MagicMock()
    k8s = _tools(settings, apps=apps)
    asked = []
    result = _executor(settings, logger, k8s, confirm=lambda s, a: asked.append(1) or True).execute(
        "k8s_restart_deployment", {"namespace": "kube-system", "deployment": "inference-api"}, "r1")
    assert result.status == ToolStatus.DENIED
    assert "not approved" in result.error
    assert asked == []
    apps.patch_namespaced_deployment.assert_not_called()


def test_restart_without_user_confirmation_is_rejected(settings, logger):
    apps = MagicMock()
    result = _executor(settings, logger, _tools(settings, apps=apps), confirm=lambda s, a: False).execute(
        "k8s_restart_deployment", {}, "r1")
    assert result.status == ToolStatus.DENIED
    assert "confirmation" in result.error
    apps.patch_namespaced_deployment.assert_not_called()


def test_scale_of_unmanaged_deployment_is_rejected(settings, logger):
    apps = MagicMock()
    result = _executor(settings, logger, _tools(settings, apps=apps), confirm=lambda s, a: True).execute(
        "k8s_scale_deployment", {"deployment": "coredns", "replicas": 0}, "r1")
    assert result.status == ToolStatus.DENIED
    apps.patch_namespaced_deployment_scale.assert_not_called()


def test_scale_outside_allowed_range_is_invalid(settings, logger):
    result = _executor(settings, logger, _tools(settings), confirm=lambda s, a: True).execute(
        "k8s_scale_deployment", {"replicas": 50}, "r1")
    assert result.status == ToolStatus.INVALID_REQUEST


def test_confirmed_restart_patches_only_the_approved_deployment(settings, logger):
    apps = MagicMock()
    result = _executor(settings, logger, _tools(settings, apps=apps), confirm=lambda s, a: True).execute(
        "k8s_restart_deployment", {}, "r1")
    assert result.status == ToolStatus.SUCCESS
    args, _ = apps.patch_namespaced_deployment.call_args
    assert args[0] == "inference-api" and args[1] == "mlops-agent"
    assert "kubectl.kubernetes.io/restartedAt" in args[2]["spec"]["template"]["metadata"]["annotations"]


def test_read_tools_are_scoped_to_approved_namespaces(settings, logger):
    core = MagicMock()
    result = _executor(settings, logger, _tools(settings, core=core)).execute(
        "k8s_list_pods", {"namespace": "kube-system"}, "r1")
    assert result.status == ToolStatus.DENIED
    core.list_namespaced_pod.assert_not_called()


def test_find_unhealthy_pods_reports_readiness_failures_and_events(settings):
    core = MagicMock()
    core.list_namespaced_pod.return_value = NS(items=[
        _pod("api-ok"),
        _pod("api-bad", ready=False, restarts=4, last_reason="Error"),
    ])
    core.list_namespaced_event.return_value = NS(items=[
        _event("Unhealthy", "api-bad", "Readiness probe failed: HTTP probe failed with statuscode: 503")])
    out = _tools(settings, core=core).find_unhealthy_pods(NS(namespace=None))
    assert out["healthy_pods"] == ["api-ok"]
    bad = out["unhealthy_pods"][0]
    assert bad["name"] == "api-bad"
    assert any("not ready" in p for p in bad["problems"])
    assert any("4 restart" in p for p in bad["problems"])
    assert bad["recent_warning_events"][0]["reason"] == "Unhealthy"
    assert out["all_healthy"] is False


def test_unreachable_cluster_is_classified_unavailable(settings, logger):
    core = MagicMock()
    core.list_namespaced_pod.side_effect = ConnectionRefusedError("[Errno 111] Connection refused")
    result = _executor(settings, logger, _tools(settings, core=core)).execute("k8s_list_pods", {}, "r1")
    assert result.status == ToolStatus.UNAVAILABLE


def test_rbac_forbidden_is_reported_as_denied(settings, logger):
    core = MagicMock()
    err = Exception("Forbidden")
    err.status, err.reason = 403, "Forbidden"
    core.list_namespaced_pod.side_effect = err
    result = _executor(settings, logger, _tools(settings, core=core)).execute("k8s_list_pods", {}, "r1")
    assert result.status == ToolStatus.DENIED
