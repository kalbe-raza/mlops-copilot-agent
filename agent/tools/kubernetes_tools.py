"""Kubernetes operations tools for the inference application (official ``kubernetes`` Python client).

Safety model
------------
* Every tool is scoped to an approved namespace list (``K8S_APPROVED_NAMESPACES``); other
  namespaces are rejected before any API call is made.
* State-changing tools (restart, scale) are marked ``mutating`` so the executor demands explicit
  user confirmation; they are additionally limited to the configured application Deployment.
* The cluster-side RBAC Role (kubernetes/rbac.yaml) mirrors these limits for defence in depth.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from agent.config import Settings
from agent.schemas import (
    DeploymentInput,
    EventsInput,
    NamespaceInput,
    PodLogsInput,
    PodsInput,
    ScaleInput,
)
from agent.tools.base import ToolError, ToolPermissionError, ToolSpec, ToolUnavailableError

REQUEST_TIMEOUT = 10


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


class KubernetesTools:
    module = "kubernetes"

    def __init__(self, settings: Settings, core: Any = None, apps: Any = None, discovery: Any = None) -> None:
        self.settings = settings
        self._core, self._apps, self._discovery = core, apps, discovery

    # ------------------------------------------------------------------ client plumbing
    def _load(self) -> None:
        from kubernetes import client, config

        try:
            config.load_kube_config(config_file=self.settings.kubeconfig, context=self.settings.k8s_context)
        except Exception:  # noqa: BLE001 - fall back to in-cluster service account
            try:
                config.load_incluster_config()
            except Exception as exc:  # noqa: BLE001
                raise ToolUnavailableError(f"no usable kubeconfig or in-cluster config: {exc}") from exc
        self._core = self._core or client.CoreV1Api()
        self._apps = self._apps or client.AppsV1Api()
        self._discovery = self._discovery or client.DiscoveryV1Api()

    @property
    def core(self) -> Any:
        if self._core is None:
            self._load()
        return self._core

    @property
    def apps(self) -> Any:
        if self._apps is None:
            self._load()
        return self._apps

    @property
    def discovery(self) -> Any:
        if self._discovery is None:
            self._load()
        return self._discovery

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, _request_timeout=REQUEST_TIMEOUT, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify client errors
            status = getattr(exc, "status", None)
            if status == 404:
                raise ToolError(f"Kubernetes resource not found: {getattr(exc, 'reason', exc)}") from exc
            if status in (401, 403):
                raise ToolPermissionError(f"Kubernetes RBAC denied the request: {getattr(exc, 'reason', exc)}") from exc
            if status is None or status >= 500:
                raise ToolUnavailableError(f"Kubernetes API unreachable: {str(exc)[:300]}") from exc
            raise ToolError(f"Kubernetes API error {status}: {getattr(exc, 'reason', exc)}") from exc

    def _ns(self, namespace: str | None) -> str:
        ns = namespace or self.settings.k8s_namespace
        if ns not in self.settings.k8s_approved_namespaces:
            raise ToolPermissionError(
                f"namespace '{ns}' is not approved; allowed namespaces: {self.settings.k8s_approved_namespaces}")
        return ns

    # ------------------------------------------------------------------ summaries
    @staticmethod
    def _pod_summary(pod: Any) -> dict[str, Any]:
        statuses = pod.status.container_statuses or []
        containers = []
        for cs in statuses:
            state: dict[str, Any] = {}
            if cs.state and cs.state.waiting:
                state = {"waiting": cs.state.waiting.reason, "message": cs.state.waiting.message}
            elif cs.state and cs.state.terminated:
                state = {"terminated": cs.state.terminated.reason, "exit_code": cs.state.terminated.exit_code}
            elif cs.state and cs.state.running:
                state = {"running_since": _iso(cs.state.running.started_at)}
            last = None
            if cs.last_state and cs.last_state.terminated:
                last = {"reason": cs.last_state.terminated.reason, "exit_code": cs.last_state.terminated.exit_code,
                        "finished_at": _iso(cs.last_state.terminated.finished_at)}
            containers.append({"name": cs.name, "image": cs.image, "ready": cs.ready,
                               "restart_count": cs.restart_count, "state": state, "last_termination": last})
        ready_cond = next((c for c in (pod.status.conditions or []) if c.type == "Ready"), None)
        return {
            "name": pod.metadata.name,
            "phase": pod.status.phase,
            "ready": f"{sum(1 for c in statuses if c.ready)}/{len(pod.spec.containers)}",
            "restarts": sum(c.restart_count for c in statuses),
            "node": pod.spec.node_name,
            "pod_ip": pod.status.pod_ip,
            "started": _iso(pod.status.start_time),
            "version_label": (pod.metadata.labels or {}).get("app.kubernetes.io/version"),
            "ready_condition": {"status": ready_cond.status, "reason": ready_cond.reason,
                                "message": ready_cond.message} if ready_cond else None,
            "containers": containers,
        }

    def _events(self, ns: str, warnings_only: bool, limit: int, involved: str | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        selectors = []
        if warnings_only:
            selectors.append("type=Warning")
        if involved:
            selectors.append(f"involvedObject.name={involved}")
        if selectors:
            kwargs["field_selector"] = ",".join(selectors)
        items = self._call(self.core.list_namespaced_event, ns, **kwargs).items

        def when(e: Any) -> datetime:
            ts = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
            return ts if isinstance(ts, datetime) else datetime.min.replace(tzinfo=UTC)

        out = []
        for e in sorted(items, key=when, reverse=True)[:limit]:
            obj = f"{e.involved_object.kind}/{e.involved_object.name}"
            out.append({"type": e.type, "reason": e.reason, "object": obj,
                        "message": (e.message or "")[:400], "count": e.count, "last_seen": _iso(when(e))})
        return out

    # ------------------------------------------------------------------ read-only tools
    def list_deployments(self, args: NamespaceInput) -> dict[str, Any]:
        ns = self._ns(args.namespace)
        out = []
        for d in self._call(self.apps.list_namespaced_deployment, ns).items:
            st = d.status
            out.append({
                "name": d.metadata.name,
                "desired_replicas": d.spec.replicas,
                "ready_replicas": st.ready_replicas or 0,
                "available_replicas": st.available_replicas or 0,
                "updated_replicas": st.updated_replicas or 0,
                "unavailable_replicas": st.unavailable_replicas or 0,
                "images": [c.image for c in d.spec.template.spec.containers],
                "version_label": (d.metadata.labels or {}).get("app.kubernetes.io/version"),
                "revision": (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
                "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                               for c in (st.conditions or [])],
            })
        return {"namespace": ns, "deployments": out, "count": len(out)}

    def list_pods(self, args: PodsInput) -> dict[str, Any]:
        ns = self._ns(args.namespace)
        selector = args.label_selector if args.label_selector is not None else self.settings.k8s_app_label
        pods = self._call(self.core.list_namespaced_pod, ns, label_selector=selector).items
        summaries = [self._pod_summary(p) for p in pods]
        return {"namespace": ns, "label_selector": selector, "pods": summaries, "count": len(summaries)}

    def get_pod_logs(self, args: PodLogsInput) -> dict[str, Any]:
        ns = self._ns(args.namespace)
        text = self._call(self.core.read_namespaced_pod_log, args.pod_name, ns, tail_lines=args.tail_lines,
                          previous=args.previous, timestamps=False)
        lines = (text or "").splitlines()
        errors = [ln for ln in lines if any(m in ln.lower() for m in ("error", "exception", "traceback", "failed"))]
        return {"namespace": ns, "pod": args.pod_name, "previous": args.previous, "lines": lines,
                "error_lines": errors[-10:]}

    def list_services(self, args: NamespaceInput) -> dict[str, Any]:
        ns = self._ns(args.namespace)
        out = []
        for s in self._call(self.core.list_namespaced_service, ns).items:
            slices = self._call(self.discovery.list_namespaced_endpoint_slice, ns,
                                label_selector=f"kubernetes.io/service-name={s.metadata.name}").items
            ready, not_ready = 0, 0
            for sl in slices:
                for ep in sl.endpoints or []:
                    if ep.conditions and ep.conditions.ready:
                        ready += 1
                    else:
                        not_ready += 1
            out.append({"name": s.metadata.name, "type": s.spec.type, "cluster_ip": s.spec.cluster_ip,
                        "ports": [{"name": p.name, "port": p.port, "target_port": str(p.target_port),
                                   "protocol": p.protocol} for p in (s.spec.ports or [])],
                        "selector": s.spec.selector, "ready_endpoints": ready, "not_ready_endpoints": not_ready,
                        "serving_traffic": ready > 0})
        return {"namespace": ns, "services": out, "count": len(out)}

    def find_unhealthy_pods(self, args: NamespaceInput) -> dict[str, Any]:
        ns = self._ns(args.namespace)
        pods = self._call(self.core.list_namespaced_pod, ns).items
        unhealthy, healthy = [], []
        for p in pods:
            s = self._pod_summary(p)
            ready_n, total = (int(x) for x in s["ready"].split("/"))
            problems = []
            if s["phase"] not in ("Running", "Succeeded"):
                problems.append(f"phase {s['phase']}")
            if s["phase"] == "Running" and ready_n < total:
                problems.append(f"not ready ({s['ready']})")
            for c in s["containers"]:
                if c["state"].get("waiting"):
                    problems.append(f"container {c['name']} waiting: {c['state']['waiting']}")
                if c["last_termination"]:
                    lt = c["last_termination"]
                    problems.append(f"container {c['name']} previously terminated: {lt['reason']} "
                                    f"(exit {lt['exit_code']})")
            if s["restarts"] > 0:
                problems.append(f"{s['restarts']} restart(s)")
            if problems:
                s["problems"] = problems
                s["recent_warning_events"] = self._events(ns, True, 5, involved=s["name"])
                unhealthy.append(s)
            else:
                healthy.append(s["name"])
        return {"namespace": ns, "total_pods": len(pods), "healthy_pods": healthy, "unhealthy_pods": unhealthy,
                "all_healthy": not unhealthy and bool(pods)}

    def get_events(self, args: EventsInput) -> dict[str, Any]:
        ns = self._ns(args.namespace)
        events = self._events(ns, args.warnings_only, args.limit)
        return {"namespace": ns, "warnings_only": args.warnings_only, "events": events, "count": len(events)}

    def get_deployed_version(self, args: DeploymentInput) -> dict[str, Any]:
        ns = self._ns(args.namespace)
        name = args.deployment or self.settings.k8s_deployment
        d = self._call(self.apps.read_namespaced_deployment, name, ns)
        container = d.spec.template.spec.containers[0]
        cfg_refs = [ef.config_map_ref.name for ef in (container.env_from or []) if ef.config_map_ref]
        config_data: dict[str, str] = {}
        for cm_name in cfg_refs:
            cm = self._call(self.core.read_namespaced_config_map, cm_name, ns)
            config_data.update(cm.data or {})
        live: dict[str, Any] | str
        try:
            raw = self._call(self.core.connect_get_namespaced_service_proxy_with_path,
                             f"{self.settings.k8s_service}:http", ns, "version", _preload_content=False)
            body = raw.data if hasattr(raw, "data") else raw  # raw bytes: the client would repr() JSON otherwise
            live = json.loads(body.decode() if isinstance(body, bytes) else body) if isinstance(body, (str, bytes)) \
                else body
        except (ToolError, ValueError) as exc:
            live = f"live /version endpoint not reachable through the API server proxy: {exc}"
        return {
            "namespace": ns, "deployment": name, "image": container.image,
            "image_tag": container.image.rsplit(":", 1)[-1] if ":" in container.image else "latest",
            "app_version_label": (d.metadata.labels or {}).get("app.kubernetes.io/version"),
            "annotations": {k: v for k, v in (d.spec.template.metadata.annotations or {}).items()
                            if k.startswith("mlops.")},
            "revision": (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
            "configmap": config_data, "live_version_endpoint": live,
            "replicas": {"desired": d.spec.replicas, "ready": d.status.ready_replicas or 0},
        }

    # ------------------------------------------------------------------ mutating tools
    def _authorize_mutation(self, args: DeploymentInput | ScaleInput) -> str | None:
        ns = args.namespace or self.settings.k8s_namespace
        if ns not in self.settings.k8s_approved_namespaces:
            return (f"modification rejected: namespace '{ns}' is not approved "
                    f"(allowed: {self.settings.k8s_approved_namespaces})")
        name = args.deployment or self.settings.k8s_deployment
        if name != self.settings.k8s_deployment:
            return f"modification rejected: deployment '{name}' is not managed by this agent"
        return None

    def restart_deployment(self, args: DeploymentInput) -> dict[str, Any]:
        ns = self._ns(args.namespace)
        name = args.deployment or self.settings.k8s_deployment
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        self._call(self.apps.patch_namespaced_deployment, name, ns, body)
        return {"namespace": ns, "deployment": name, "action": "rollout restart", "restarted_at": now}

    def scale_deployment(self, args: ScaleInput) -> dict[str, Any]:
        ns = self._ns(args.namespace)
        name = args.deployment or self.settings.k8s_deployment
        self._call(self.apps.patch_namespaced_deployment_scale, name, ns, {"spec": {"replicas": args.replicas}})
        return {"namespace": ns, "deployment": name, "action": "scale", "replicas": args.replicas}

    # ------------------------------------------------------------------ registry
    def specs(self) -> list[ToolSpec]:
        m = self.module
        return [
            ToolSpec("k8s_list_deployments", "List Deployments with desired/ready/available replicas, images, "
                     "version label and rollout conditions.", NamespaceInput, self.list_deployments, m),
            ToolSpec("k8s_list_pods", "List application Pods with phase, readiness, restarts and container "
                     "states.", PodsInput, self.list_pods, m),
            ToolSpec("k8s_get_pod_logs", "Recent logs of a Pod (optionally of the previous crashed container) "
                     "with error lines highlighted.", PodLogsInput, self.get_pod_logs, m),
            ToolSpec("k8s_list_services", "Inspect Services: type, ClusterIP, ports, selector and how many ready "
                     "endpoints currently back each Service.", NamespaceInput, self.list_services, m),
            ToolSpec("k8s_find_unhealthy_pods", "Identify unhealthy Pods (not ready, waiting, crash loops, "
                     "restarts) with their recent Warning events.", NamespaceInput, self.find_unhealthy_pods, m),
            ToolSpec("k8s_get_events", "Recent namespace events (Warnings by default): probe failures, back-offs, "
                     "scheduling and image pull problems.", EventsInput, self.get_events, m),
            ToolSpec("k8s_get_deployed_version", "Currently deployed application version: image tag, version "
                     "label, model annotations, ConfigMap values and the live /version endpoint.",
                     DeploymentInput, self.get_deployed_version, m),
            ToolSpec("k8s_restart_deployment", "Rollout-restart the inference Deployment (state-changing: requires "
                     "explicit user confirmation, approved namespace only).", DeploymentInput,
                     self.restart_deployment, m, mutating=True, authorize=self._authorize_mutation),
            ToolSpec("k8s_scale_deployment", "Scale the inference Deployment to N replicas (state-changing: "
                     "requires explicit user confirmation, approved namespace only).", ScaleInput,
                     self.scale_deployment, m, mutating=True, authorize=self._authorize_mutation),
        ]
