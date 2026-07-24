"""Pod-level configuration checks.

Latent problems. Nothing here means the pod is broken right now: it means
the pod is set up in a way that causes an incident later.
"""


def missing_resource_limits(pod: dict) -> list[str]:
    """Containers declaring no memory limit or no cpu limit.

    Returns the names of the offending containers.

    Reads spec, not status. This is a property of how the pod was written,
    not of how it's behaving, so it fires on a perfectly healthy pod.

    That's the point, and it's the check worth defending hardest: it means
    a triage run against a cluster where nothing is on fire still returns
    something. A tool that prints "no findings" on a healthy cluster gets
    run once and never again.

    Why it matters operationally: a container with no memory limit can
    consume everything on the node and get the kubelet to evict its
    neighbours. The pod that caused the incident isn't the pod that dies.
    A container with no cpu limit is less dangerous but still noisy.
    """
    offenders = []
    for container in pod.get("spec", {}).get("containers", []):
        limits = container.get("resources", {}).get("limits", {})
        if not limits.get("memory") or not limits.get("cpu"):
            offenders.append(container["name"])
    return offenders