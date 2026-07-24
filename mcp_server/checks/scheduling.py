"""Pod-level scheduling checks.

These take the whole pod, not a container status, because a pod that was
never scheduled has no containerStatuses at all. There is no container to
have a status: the scheduler never placed it on a node.

That asymmetry is why checks split into two levels rather than sharing one
signature. Pretending a Pending pod has containers would mean inventing
data the API doesn't return.
"""


def is_unschedulable(pod: dict) -> tuple[bool, str | None]:
    """Pod cannot be placed on any node.

    Returns (verdict, scheduler_message).

    Breaks the -> bool pattern the other checks follow, deliberately. The
    scheduler already writes a precise explanation into the condition
    message:

        "0/1 nodes are available: 1 Insufficient memory. no new claims to
         deallocate, preemption: 0/1 nodes are available: 1 Preemption is
         not helpful for scheduling."

    Discarding that and returning a bare True would throw away the single
    most useful string in the response. The message is the finding.

    Causes it covers without needing separate checks: insufficient CPU or
    memory, node taints the pod doesn't tolerate, unsatisfiable affinity
    rules, no nodes matching a nodeSelector.
    """
    for condition in pod.get("status", {}).get("conditions", []):
        if condition.get("type") != "PodScheduled":
            continue
        if condition.get("status") == "False" and condition.get("reason") == "Unschedulable":
            return True, condition.get("message")
    return False, None
