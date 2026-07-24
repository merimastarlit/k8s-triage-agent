"""Assemble a pod summary from the deterministic checks.

Deliberately separate from server.py and free of Kubernetes imports. This is
the layer that turns a raw pod dict into the structure the agent reads, and it
is the layer most likely to contain a quiet mistake — joins between spec and
status, name handling, which message goes with which finding. Keeping it here
means it can be tested against the captured fixtures with no cluster and no
API client.

The bug that motivated the split: this logic lived inline in list_pods, where
a shadowed variable made every pod report its container's name instead of its
own. Every finding in the report was labelled "app". Nothing could have caught
it except a live run, because nothing could import it without a cluster.
"""

from mcp_server.checks.config import missing_resource_limits
from mcp_server.checks.pods import check_container, config_error
from mcp_server.checks.scheduling import is_unschedulable


def summarize_pod(pod: dict) -> dict:
    """Turn one pod dict into a triage summary with findings attached.

    Pure: a dict goes in, a dict comes out. No API calls, no model.
    """
    pod_name = pod.get("metadata", {}).get("name")
    findings = []

    # Pod-level first. A Pending pod has no containerStatuses at all, so
    # anything that touches containers would skip it entirely.
    unschedulable, scheduler_message = is_unschedulable(pod)
    if unschedulable:
        findings.append({
            "code": "UNSCHEDULABLE",
            "scheduler_message": scheduler_message,
        })

    # Fires on healthy pods too. That is the point: a triage run against a
    # cluster where nothing is on fire should still return something.
    offenders = missing_resource_limits(pod)
    if offenders:
        findings.append({
            "code": "MISSING_RESOURCE_LIMITS",
            "containers": offenders,
        })

    # Iterate spec.containers, not containerStatuses.
    #
    # containerStatuses only holds containers that were actually created, so a
    # Pending pod has none. Iterating it hid the pod's declared resources,
    # which for an unschedulable pod is the entire diagnosis: the first live
    # run reported that a pod had no memory requests when it was requesting
    # 64Gi.
    #
    # spec.containers is what should exist; status is what does. Join by name.
    statuses = {
        cs.get("name"): cs
        for cs in (pod.get("status", {}).get("containerStatuses") or [])
    }

    containers = []
    for spec_container in pod.get("spec", {}).get("containers", []):
        container_name = spec_container.get("name")
        cs = statuses.get(container_name, {})
        codes = check_container(cs) if cs else []

        containers.append({
            "name": container_name,
            # Distinguishes "never started" from "started and died". A
            # CONFIG_ERROR container was never created and has no logs.
            "created": bool(cs),
            "ready": cs.get("ready"),
            "restart_count": cs.get("restartCount"),
            # Declared requests and limits. Without these the agent can see
            # that a container was OOMKilled but not what it was capped at,
            # so the best fix it can offer is "go look up the limit".
            "resources": spec_container.get("resources", {}),
            "state": cs.get("state"),
            "last_state": cs.get("lastState"),
            "findings": codes,
        })

        for code in codes:
            finding = {"code": code, "container": container_name}
            # The kubelet writes the diagnosis itself for config errors:
            # "couldn't find key db_host in ConfigMap default/app-config".
            # Pass it through verbatim rather than making the agent guess.
            if code == "CONFIG_ERROR":
                finding["kubelet_message"] = config_error(cs)[1]
            findings.append(finding)

    return {
        "name": pod_name,
        "phase": pod.get("status", {}).get("phase"),
        "containers": containers,
        "findings": findings,
    }