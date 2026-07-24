"""Container-level checks.

Each function takes a single entry from pod.status.containerStatuses and
returns a verdict. No model involved: a dict goes in, a boolean comes out.

These operate per-container, not per-pod, because a pod with a sidecar has
several container statuses and any one of them can be the broken one.
"""

# One restart is a blip. Three is a pattern.
CRASHLOOP_RESTART_THRESHOLD = 3


def is_crashloop(cs: dict) -> bool:
    """Container is restarting repeatedly and never becoming ready.

    Deliberately not keyed on state.waiting.reason == "CrashLoopBackOff"
    alone. A crashlooping container alternates between two states: waiting
    (kubelet is backing off before the next attempt) and terminated (it
    just died again). Sampling at the wrong moment sees terminated and the
    check returns False even though the pod has restarted 4 times.

    restartCount is monotonic and doesn't race. The waiting reason is kept
    as a confirming signal for the case where a container is in backoff
    before it has accumulated enough restarts to trip the threshold.
    """
    restarts = cs.get("restartCount", 0)
    ready = cs.get("ready", False)
    waiting_reason = cs.get("state", {}).get("waiting", {}).get("reason")

    if waiting_reason == "CrashLoopBackOff":
        return True
    return restarts >= CRASHLOOP_RESTART_THRESHOLD and not ready


def is_oomkilled(cs: dict) -> bool:
    """Container was killed for exceeding its memory limit.

    Reads lastState, not state: by the time you look, the OOMKilled
    container is already gone and a fresh one is starting. The evidence
    lives in the corpse.

    Keyed on reason rather than exitCode. 137 is 128 + 9 (SIGKILL) and any
    SIGKILL produces it, including an operator running kubectl delete. The
    kubelet sets reason="OOMKilled" specifically.
    """
    last_terminated = cs.get("lastState", {}).get("terminated", {})
    return last_terminated.get("reason") == "OOMKilled"


def is_imagepull_failure(cs: dict) -> bool:
    """Container image cannot be pulled.

    Three distinct reasons, all meaning the image never arrived:
      ErrImagePull      - the pull failed just now
      ImagePullBackOff  - it failed repeatedly, kubelet is backing off
      InvalidImageName  - the image reference doesn't parse
    """
    waiting_reason = cs.get("state", {}).get("waiting", {}).get("reason", "")
    return waiting_reason in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName")


def config_error(cs: dict) -> tuple[bool, str | None]:
    """Container could not be created because its configuration is invalid.

    Returns (verdict, kubelet_message).

    Added after the first live run against a real cluster. A Deployment
    referencing a ConfigMap key that does not exist produces
    CreateContainerConfigError, and none of the other five checks fire on it:

        restartCount        0        -> not a crashloop
        lastState           {}       -> not OOMKilled
        state.waiting       CreateContainerConfigError -> not an image pull failure
        PodScheduled        True     -> not unschedulable
        resources.limits    set      -> limits are fine

    A visibly broken pod with zero findings. The reason is that this container
    never ran. The kubelet could not assemble its environment, so it was never
    created, never started, never crashed. CrashLoopBackOff requires a
    container that ran and died; this one never got that far, and restartCount
    stays 0 forever.

    Like is_unschedulable, this returns the message rather than a bare bool.
    The kubelet writes a precise explanation:

        "couldn't find key db_host in ConfigMap default/app-config"

    That string names the key, the ConfigMap, and the namespace. Discarding it
    to return True would throw away the entire diagnosis.

    Covers missing ConfigMap keys, missing Secret keys, and unresolvable
    volume or env references — among the most common config failures in
    production, and invisible to every status-based check.
    """
    waiting = cs.get("state", {}).get("waiting", {})
    reason = waiting.get("reason", "")
    if reason in ("CreateContainerConfigError", "CreateContainerError"):
        return True, waiting.get("message")
    return False, None


def check_container(cs: dict) -> list[str]:
    """Run every container-level check, return the codes that fired.

    A container can trip more than one. An OOMKilled container that keeps
    getting restarted is both OOMKILLED and CRASHLOOP, and that overlap is
    the useful part: CRASHLOOP is the symptom, OOMKILLED is the cause.
    """
    findings = []
    if is_crashloop(cs):
        findings.append("CRASHLOOP")
    if is_oomkilled(cs):
        findings.append("OOMKILLED")
    if is_imagepull_failure(cs):
        findings.append("IMAGE_PULL_FAILURE")
    if config_error(cs)[0]:
        findings.append("CONFIG_ERROR")
    return findings