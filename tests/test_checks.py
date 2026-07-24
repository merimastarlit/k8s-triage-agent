"""Unit tests for the deterministic checks.

No cluster, no API calls, no model. Fixtures are real responses captured
from a kind cluster running Kubernetes 1.36, not hand-written dicts. That
distinction matters: an invented fixture tests the check against what you
believed the API returns, which is exactly the assumption a test is
supposed to catch.

Runs in CI for free in under a second.
"""

import json
from pathlib import Path

import pytest

from mcp_server.checks.config import missing_resource_limits
from mcp_server.checks.pods import (
    check_container,
    config_error,
    is_crashloop,
    is_imagepull_failure,
    is_oomkilled,
)
from mcp_server.checks.scheduling import is_unschedulable
from mcp_server.summarize import summarize_pod

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def crashloop_status() -> dict:
    return load("crashloop_status.json")


@pytest.fixture
def oomkill_status() -> dict:
    return load("oomkill_status.json")


@pytest.fixture
def pending_pod() -> dict:
    return load("pending_pod.json")


@pytest.fixture
def config_error_status() -> dict:
    return load("config_error_status.json")


# --- crashloop -------------------------------------------------------------


def test_crashloop_detected_while_terminated(crashloop_status):
    """The regression this whole check exists for.

    This fixture was captured mid-cycle: restartCount is 4, but state is
    terminated, not waiting/CrashLoopBackOff. A check keyed only on the
    waiting reason returns False here and the pod looks healthy.
    """
    assert crashloop_status["state"].get("waiting") is None
    assert is_crashloop(crashloop_status) is True


def test_crashloop_detected_while_backing_off(oomkill_status):
    """The other half of the cycle: waiting/CrashLoopBackOff."""
    assert oomkill_status["state"]["waiting"]["reason"] == "CrashLoopBackOff"
    assert is_crashloop(oomkill_status) is True


def test_healthy_container_is_not_crashlooping():
    healthy = {"restartCount": 0, "ready": True, "state": {"running": {}}}
    assert is_crashloop(healthy) is False


def test_single_restart_is_not_a_crashloop():
    """One restart is a blip. Don't page anyone for it."""
    blip = {"restartCount": 1, "ready": True, "state": {"running": {}}}
    assert is_crashloop(blip) is False


def test_restarting_but_ready_is_not_a_crashloop():
    """Restarts that recover are not a crashloop. A container that OOMs
    nightly and comes back is a different problem from one stuck in a loop.
    """
    recovered = {"restartCount": 5, "ready": True, "state": {"running": {}}}
    assert is_crashloop(recovered) is False


# --- oomkilled -------------------------------------------------------------


def test_oomkill_detected(oomkill_status):
    assert is_oomkilled(oomkill_status) is True


def test_ordinary_crash_is_not_oomkill(crashloop_status):
    """exitCode 1, reason Error. Crashing, but not for memory."""
    assert is_oomkilled(crashloop_status) is False


def test_sigkill_without_oom_reason_is_not_oomkill():
    """137 is 128+SIGKILL and any SIGKILL produces it, including a manual
    kubectl delete. Keying on the exit code would false-positive here.
    """
    sigkilled = {
        "restartCount": 1,
        "ready": False,
        "lastState": {"terminated": {"exitCode": 137, "reason": "Error"}},
    }
    assert is_oomkilled(sigkilled) is False


def test_no_laststate_is_not_oomkill():
    """A container that has never restarted has no lastState at all."""
    fresh = {"restartCount": 0, "ready": True, "state": {"running": {}}}
    assert is_oomkilled(fresh) is False


# --- image pull ------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["ErrImagePull", "ImagePullBackOff", "InvalidImageName"]
)
def test_imagepull_failures(reason):
    cs = {"restartCount": 0, "ready": False, "state": {"waiting": {"reason": reason}}}
    assert is_imagepull_failure(cs) is True


def test_crashloop_is_not_an_imagepull_failure(oomkill_status):
    """Both live in state.waiting.reason. Don't confuse them."""
    assert is_imagepull_failure(oomkill_status) is False


# --- scheduling ------------------------------------------------------------


def test_unschedulable_detected(pending_pod):
    unschedulable, message = is_unschedulable(pending_pod)
    assert unschedulable is True
    assert "Insufficient memory" in message


def test_unschedulable_pod_has_no_container_statuses(pending_pod):
    """Documents why checks split into pod-level and container-level.

    There is no containerStatuses key. Any check that assumed one would
    KeyError on every Pending pod in the cluster.
    """
    assert "containerStatuses" not in pending_pod["status"]


def test_scheduled_pod_is_not_unschedulable():
    scheduled = {
        "status": {
            "conditions": [
                {"type": "PodScheduled", "status": "True"},
            ]
        }
    }
    unschedulable, message = is_unschedulable(scheduled)
    assert unschedulable is False
    assert message is None


# --- missing limits --------------------------------------------------------


def test_missing_limits_on_pending_pod(pending_pod):
    """Requests but no limits. Fires even though the pod's real problem
    is that it's unschedulable.
    """
    assert missing_resource_limits(pending_pod) == ["app"]


def test_partial_limits_still_flagged():
    """A memory limit with no cpu limit is still a gap."""
    pod = {
        "spec": {
            "containers": [
                {"name": "app", "resources": {"limits": {"memory": "512Mi"}}}
            ]
        }
    }
    assert missing_resource_limits(pod) == ["app"]


def test_fully_specified_pod_is_clean():
    pod = {
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "resources": {"limits": {"memory": "512Mi", "cpu": "500m"}},
                }
            ]
        }
    }
    assert missing_resource_limits(pod) == []


def test_only_the_offending_sidecar_is_named():
    """The sidecar trap, at pod level. Don't stop at containers[0]."""
    pod = {
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "resources": {"limits": {"memory": "512Mi", "cpu": "500m"}},
                },
                {"name": "log-shipper", "resources": {}},
            ]
        }
    }
    assert missing_resource_limits(pod) == ["log-shipper"]


# --- config error ----------------------------------------------------------


def test_config_error_detected(config_error_status):
    detected, message = config_error(config_error_status)
    assert detected is True
    assert "db_host" in message
    assert "app-config" in message


def test_config_error_invisible_to_every_other_check(config_error_status):
    """The regression that put this check in the suite.

    Captured from the first live run. payments-api was visibly broken and
    every one of the original five checks returned nothing:

      restartCount 0        -> not a crashloop
      lastState {}          -> not OOMKilled
      waiting reason is
      CreateContainerConfigError -> not an image pull failure

    A triage tool that reports zero findings on a broken pod is worse than
    one that reports the wrong cause: the operator concludes it is fine.
    """
    assert is_crashloop(config_error_status) is False
    assert is_oomkilled(config_error_status) is False
    assert is_imagepull_failure(config_error_status) is False


def test_config_error_container_never_ran(config_error_status):
    """Documents why CrashLoopBackOff cannot apply here.

    The kubelet could not build the container's environment, so no container
    was created. Nothing ran, nothing died, nothing restarted.
    """
    assert config_error_status["restartCount"] == 0
    assert config_error_status["lastState"] == {}


def test_healthy_container_has_no_config_error():
    healthy = {"restartCount": 0, "ready": True, "state": {"running": {}}}
    detected, message = config_error(healthy)
    assert detected is False
    assert message is None


def test_crashloop_is_not_a_config_error(oomkill_status):
    """Both surface in state.waiting.reason. Don't conflate them."""
    assert config_error(oomkill_status)[0] is False


# --- correlation -----------------------------------------------------------


def test_oomkilled_container_reports_both_findings(oomkill_status):
    """The overlap that makes the agent worth building.

    kubectl says CrashLoopBackOff and stops there. Both checks firing is
    what lets the agent say: it's crashlooping *because* it's being
    OOMKilled against a 10Mi limit.
    """
    findings = check_container(oomkill_status)
    assert set(findings) == {"CRASHLOOP", "OOMKILLED"}


def test_plain_crashloop_reports_only_crashloop(crashloop_status):
    assert check_container(crashloop_status) == ["CRASHLOOP"]


def test_config_error_reported_by_check_container(config_error_status):
    assert check_container(config_error_status) == ["CONFIG_ERROR"]


def test_healthy_container_reports_nothing():
    healthy = {"restartCount": 0, "ready": True, "state": {"running": {}}}
    assert check_container(healthy) == []


# --- pending pods report their requests ------------------------------------


def test_pending_pod_declares_requests_despite_no_container_statuses(pending_pod):
    """The regression behind the wrong ml-trainer advice.

    list_pods used to build its container list by iterating containerStatuses.
    A Pending pod has none, so the loop never ran and the tool reported no
    resource information at all. The agent concluded the pod had no memory
    requests and advised setting some — when it was requesting 64Gi, which was
    the entire reason it could not be scheduled.

    The requests are in spec the whole time. Nothing was missing from the API;
    the tool was reading the wrong half of the pod.
    """
    assert "containerStatuses" not in pending_pod["status"]

    container = pending_pod["spec"]["containers"][0]
    assert container["resources"]["requests"]["memory"] == "64Gi"


def test_pending_pod_has_requests_but_no_limits(pending_pod):
    """Documents the distinction the agent got wrong.

    MISSING_RESOURCE_LIMITS means limits, not requests. A pod with requests
    and no limits is Burstable QoS, not BestEffort — BestEffort requires
    neither to be set.
    """
    container = pending_pod["spec"]["containers"][0]
    assert "requests" in container["resources"]
    assert "limits" not in container["resources"]
    assert missing_resource_limits(pending_pod) == ["app"]


# --- summarize_pod ---------------------------------------------------------


@pytest.fixture
def config_error_pod() -> dict:
    return load("config_error_pod.json")


def test_summary_reports_the_pod_name_not_the_container_name(config_error_pod):
    """The regression that made an entire triage report unusable.

    This logic used to live inline in list_pods, where the loop over
    spec.containers reassigned the same variable that held the pod name. Every
    pod then reported itself as its container's name. All three demo pods have
    a container called "app", so every finding in the report was headed "app"
    and none of them could be told apart.

    It cascaded: the agent called describe_deployment("app"), got a 404, and
    concluded from the error text that the pods were standalone. payments-api
    is a Deployment. One shadowed variable produced a report that read
    plausibly and was wrong in three places.
    """
    summary = summarize_pod(config_error_pod)
    assert summary["name"] == "payments-api-5bb5b8ff88-2hpr4"
    assert summary["containers"][0]["name"] == "app"


def test_summary_attaches_the_kubelet_message(config_error_pod):
    summary = summarize_pod(config_error_pod)
    config_findings = [f for f in summary["findings"] if f["code"] == "CONFIG_ERROR"]
    assert len(config_findings) == 1
    assert "db_host" in config_findings[0]["kubelet_message"]
    assert config_findings[0]["container"] == "app"


def test_summary_exposes_requests_on_a_pending_pod(pending_pod):
    """A Pending pod has no containerStatuses, but it still declares what it
    asked for — and that request is the reason it cannot be scheduled.
    """
    # Captured from a throwaway pod before broken-cluster/ existed; the name
    # differs from the demo manifest but the shape is the one that matters.
    summary = summarize_pod(pending_pod)
    assert summary["name"] == "pending-demo"

    container = summary["containers"][0]
    assert container["created"] is False
    assert container["resources"]["requests"]["memory"] == "64Gi"

    codes = [f["code"] for f in summary["findings"]]
    assert "UNSCHEDULABLE" in codes
    assert "MISSING_RESOURCE_LIMITS" in codes


def test_summary_carries_the_scheduler_message(pending_pod):
    summary = summarize_pod(pending_pod)
    sched = [f for f in summary["findings"] if f["code"] == "UNSCHEDULABLE"][0]
    assert "Insufficient memory" in sched["scheduler_message"]


def test_summary_distinguishes_pods_sharing_a_container_name(
    config_error_pod, pending_pod
):
    """Both pods have a container named "app". The summaries must not collide."""
    names = {summarize_pod(config_error_pod)["name"], summarize_pod(pending_pod)["name"]}
    assert names == {"payments-api-5bb5b8ff88-2hpr4", "pending-demo"}
    assert len(names) == 2

    