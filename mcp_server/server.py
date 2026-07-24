"""
Custom MCP server for Kubernetes triage.

Exposes read-only tools for:
- Listing pods with deterministic check findings already applied
- Fetching warning events for a pod
- Fetching logs, defaulting to the previous (dead) container
- Describing a deployment

Every tool is read-only. There is no tool here that mutates cluster state,
and that is deliberate: an LLM with delete permissions is a liability, not
a feature.

Runs as a subprocess launched by the agent (python -m mcp_server.server).
The Kubernetes client is synchronous, which is correct here — this process
does nothing else, and stdio framing is what the SDK expects.
"""

import json
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP

from mcp_server.summarize import summarize_pod

mcp = FastMCP("k8s-triage")

# Logs are the single biggest context risk in this project. A container
# crashlooping for an hour can emit tens of thousands of lines and none of
# it fits in a context window.
DEFAULT_TAIL_LINES = 50
MAX_TAIL_LINES = 200


def _load_config() -> None:
    """Load kubeconfig, falling back to in-cluster if it ever runs as a pod."""
    try:
        config.load_kube_config()
    except config.ConfigException:
        config.load_incluster_config()


def _to_dict(obj) -> dict:
    """Convert a client model to the same JSON shape kubectl returns.

    The Python client exposes snake_case attributes (pod.status.container_statuses)
    but the checks and the test fixtures are written against the wire format
    (containerStatuses, restartCount, lastState). sanitize_for_serialization
    produces the wire format, so what the checks see in production is byte for
    byte what they see in the tests.
    """
    return client.ApiClient().sanitize_for_serialization(obj)


def _error(message: str, category: str = "validation", retryable: bool = False) -> str:
    return json.dumps({
        "status": "error",
        "error_category": category,
        "is_retryable": retryable,
        "message": message,
    })


@mcp.tool()
def list_pods(namespace: str = "default") -> str:
    """List pods in a namespace with deterministic check findings applied.

    USE THIS TOOL WHEN:
    - Starting a triage — always call this first to see what is broken
    - You need to decide which pods are worth investigating further

    DO NOT USE THIS TOOL WHEN:
    - You already have the pod list for this namespace in this session
    - You need the reason a specific pod is broken — use get_pod_events or get_logs

    ACCEPTS: namespace — the Kubernetes namespace to inspect
    RETURNS: Every pod with phase, container states, and any check findings.
             Findings are symptoms, not causes. A pod flagged CRASHLOOP tells
             you it keeps dying; it does not tell you why. That requires events
             and logs.
    """
    try:
        _load_config()
        v1 = client.CoreV1Api()
        pod_list = v1.list_namespaced_pod(namespace=namespace)
    except ApiException as e:
        return _error(f"Kubernetes API error listing pods: {e.reason}", "api", True)
    except Exception as e:
        return _error(f"Could not reach the cluster: {e}", "connection", True)

    pods = [summarize_pod(_to_dict(pod_obj)) for pod_obj in pod_list.items]
    broken = [p["name"] for p in pods if p["findings"]]

    return json.dumps({
        "status": "success",
        "namespace": namespace,
        "pod_count": len(pods),
        "pods_with_findings": broken,
        "pods": pods,
    })


@mcp.tool()
def get_pod_events(namespace: str, pod_name: str) -> str:
    """Get warning events for a specific pod, newest first.

    USE THIS TOOL WHEN:
    - list_pods flagged a pod and you need to know why
    - A pod is Pending, ImagePullBackOff, or failing to start — the cause is
      almost always in the events rather than the logs

    DO NOT USE THIS TOOL WHEN:
    - The container started and then crashed on its own — the stack trace is
      in the logs, use get_logs
    - You have not run list_pods yet

    ACCEPTS: namespace, pod_name
    RETURNS: Warning events only, newest first. Normal events are filtered out
             deliberately: "Successfully pulled image" is most of what the
             cluster emits and none of it explains a failure.
    """
    try:
        _load_config()
        v1 = client.CoreV1Api()
        events = v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )
    except ApiException as e:
        return _error(f"Kubernetes API error fetching events: {e.reason}", "api", True)
    except Exception as e:
        return _error(f"Could not reach the cluster: {e}", "connection", True)

    warnings = []
    for event_obj in events.items:
        event = _to_dict(event_obj)
        if event.get("type") == "Normal":
            continue
        warnings.append({
            "reason": event.get("reason"),
            "message": event.get("message"),
            "count": event.get("count"),
            "last_timestamp": event.get("lastTimestamp") or event.get("eventTime"),
        })

    # Newest first. Events with no timestamp sort last rather than crashing
    # the comparison.
    warnings.sort(key=lambda e: e["last_timestamp"] or "", reverse=True)

    if not warnings:
        return json.dumps({
            "status": "success",
            "pod": pod_name,
            "event_count": 0,
            "events": [],
            "note": (
                "No warning events. Events expire after about an hour by default, "
                "so an old failure may have no events left. Check the logs."
            ),
        })

    return json.dumps({
        "status": "success",
        "pod": pod_name,
        "event_count": len(warnings),
        "events": warnings,
    })


@mcp.tool()
def get_logs(
    namespace: str,
    pod_name: str,
    container: str = None,
    previous: bool = True,
    tail_lines: int = DEFAULT_TAIL_LINES,
) -> str:
    """Get recent log lines from a pod, defaulting to the dead container.

    USE THIS TOOL WHEN:
    - A container started and then died — its own output explains why
    - Events showed nothing useful

    DO NOT USE THIS TOOL WHEN:
    - The pod is Pending — no container ever ran, so there are no logs.
      Use get_pod_events instead
    - The pod is ImagePullBackOff — the image never arrived, there is nothing
      to log

    ACCEPTS: namespace, pod_name, container (required if the pod has more than
             one), previous (default True), tail_lines (default 50, max 200)
    RETURNS: The last N lines. Truncated on purpose.

    previous defaults to True because on a crashlooping pod the live container
    is a few milliseconds old and has printed nothing yet. The container that
    died holds the error. If there is no previous container this falls back to
    the current one and says so.
    """
    tail_lines = min(max(tail_lines, 1), MAX_TAIL_LINES)

    try:
        _load_config()
        v1 = client.CoreV1Api()
    except Exception as e:
        return _error(f"Could not reach the cluster: {e}", "connection", True)

    def _read(use_previous: bool) -> str:
        return v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
            previous=use_previous,
            tail_lines=tail_lines,
        )

    used_previous = previous
    try:
        logs = _read(previous)
    except ApiException as e:
        # 400 here usually means "no previous terminated container", which is
        # not an error worth surfacing — it just means the pod has not
        # restarted yet.
        if previous and e.status == 400:
            try:
                logs = _read(False)
                used_previous = False
            except ApiException as inner:
                return _error(
                    f"Could not read logs for {pod_name}: {inner.reason}", "api", True
                )
        else:
            return _error(f"Could not read logs for {pod_name}: {e.reason}", "api", True)

    if not logs.strip():
        return json.dumps({
            "status": "success",
            "pod": pod_name,
            "container": container,
            "from_previous_container": used_previous,
            "lines": 0,
            "logs": "",
            "note": "Container produced no output. The failure is likely before the process started — check events.",
        })

    return json.dumps({
        "status": "success",
        "pod": pod_name,
        "container": container,
        "from_previous_container": used_previous,
        "truncated_to_last_n_lines": tail_lines,
        "logs": logs,
    })


@mcp.tool()
def describe_deployment(namespace: str, deployment_name: str) -> str:
    """Get a deployment's spec and rollout status.

    USE THIS TOOL WHEN:
    - A pod's problem looks like it comes from how it was declared — a bad
      ConfigMap reference, a wrong image tag, limits that are too tight
    - You need to know what a pod is supposed to look like, not what it is

    DO NOT USE THIS TOOL WHEN:
    - The pod is standalone rather than owned by a deployment
    - You only need the pod's runtime state — use list_pods

    ACCEPTS: namespace, deployment_name
    RETURNS: Replica counts, rollout conditions, and each container's image,
             env sources, and resources. The env sources matter: a
             configMapKeyRef pointing at a key that does not exist is a common
             cause of a crashloop that the pod status cannot explain.
    """
    try:
        _load_config()
        apps = client.AppsV1Api()
        deployment = _to_dict(apps.read_namespaced_deployment(
            name=deployment_name, namespace=namespace
        ))
    except ApiException as e:
        if e.status == 404:
            return _error(
                f"No deployment named {deployment_name} in {namespace}. "
                "The pod may be standalone or owned by a different controller.",
                "validation",
                False,
            )
        return _error(f"Kubernetes API error: {e.reason}", "api", True)
    except Exception as e:
        return _error(f"Could not reach the cluster: {e}", "connection", True)

    spec = deployment.get("spec", {})
    status = deployment.get("status", {})
    pod_spec = spec.get("template", {}).get("spec", {})

    containers = []
    for c in pod_spec.get("containers", []):
        containers.append({
            "name": c.get("name"),
            "image": c.get("image"),
            "command": c.get("command"),
            "env": c.get("env"),
            "env_from": c.get("envFrom"),
            "resources": c.get("resources"),
            "volume_mounts": c.get("volumeMounts"),
        })

    conditions = [
        {
            "type": cond.get("type"),
            "status": cond.get("status"),
            "reason": cond.get("reason"),
            "message": cond.get("message"),
        }
        for cond in status.get("conditions", [])
    ]

    return json.dumps({
        "status": "success",
        "deployment": deployment_name,
        "namespace": namespace,
        "replicas_desired": spec.get("replicas"),
        "replicas_ready": status.get("readyReplicas", 0),
        "replicas_available": status.get("availableReplicas", 0),
        "conditions": conditions,
        "containers": containers,
        "volumes": pod_spec.get("volumes"),
    })


if __name__ == "__main__":
    mcp.run()

    