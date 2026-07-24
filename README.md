# Kubernetes Triage Agent

An AI agent that diagnoses *why* Kubernetes workloads are broken — built with
the Claude Agent SDK and a custom read-only MCP server.

`kubectl get pods` tells you a pod is in CrashLoopBackOff. It does not tell you
that it's crashlooping because it mounts a ConfigMap key that doesn't exist.
The gap between those two sentences is what this agent closes.

## Architecture

```
kubeconfig → Agent → MCP Server (4 read-only tools) → Kubernetes API
                ↓
        Deterministic Checks
                ↓
          Correlated Diagnosis
```

- **Agent** (Claude Agent SDK) — decides which pods are worth investigating,
  which tools to call on them, and correlates the results into a causal story
- **MCP Server** (custom) — four read-only tools over the Kubernetes API
- **Checks** — deterministic Python functions. A dict goes in, a verdict comes
  out. No model involved.

## The division of labor

This is the design, and it's the whole point:

**Checks are deterministic.** `is_crashloop(container_status)` returns True or
False. Testable with real captured fixtures, which is why CI runs in under a
second and costs nothing.

**The agent correlates.** It's looking at a CrashLoopBackOff *and* an event
about a missing ConfigMap key *and* an empty log, and connecting them into an
explanation. Rules can't do that — the space of causes is too large to
enumerate.

Let the model decide whether a pod is CrashLooping and you've built something
untestable. Let rules write the diagnosis and you've built a worse
`kubectl describe`. The line between them is the architecture.

## What it detects

| Check | Level | Signal |
|---|---|---|
| `CRASHLOOP` | container | `restartCount` climbing, never ready |
| `OOMKILLED` | container | `lastState.terminated.reason` |
| `IMAGE_PULL_FAILURE` | container | `state.waiting.reason` |
| `CONFIG_ERROR` | container | `state.waiting.reason` + kubelet message |
| `UNSCHEDULABLE` | pod | `status.conditions[PodScheduled]` |
| `MISSING_RESOURCE_LIMITS` | pod | `spec.containers[].resources.limits` |

Six checks. Five were designed up front; `CONFIG_ERROR` was added after the
first live run, when a pod broken by a missing ConfigMap key produced zero
findings across all five (see Tests below). The *explanation* space is open — the agent reads
events and logs, so the cause can be anything: a missing ConfigMap key, a bad
env var, a failed migration, a port collision. The checks catch the symptoms
Kubernetes surfaces reliably; the model handles the long tail of causes.

Checks split into pod-level and container-level because the API forces it: a
Pending pod has no `containerStatuses` at all — no container was ever created
to have a status.

## Quick Start

```bash
git clone <repo-url>
cd k8s-triage-agent
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

echo "ANTHROPIC_API_KEY=your-key-here" > .env

# Stand up a cluster and break it on purpose
kind create cluster --name triage
kubectl apply -f broken-cluster/
sleep 60   # CrashLoopBackOff needs a few restart cycles

python -m agent.triage --namespace default

# Tests: no cluster, no API calls, no cost
python -m pytest tests/ -v
```

## The demo

`broken-cluster/` ships three pods that fail in three different ways:

| Pod | Symptom | Cause |
|---|---|---|
| `payments-api` | CreateContainerConfigError | ConfigMap key `db_host` doesn't exist |
| `report-generator` | CrashLoopBackOff | OOMKilled — 10Mi limit, allocates 100Mi |
| `ml-trainer` | Pending | Requests 64Gi, no node can satisfy it |

Three different failure modes, three different code paths: a container that
was never created, a container that ran and was killed for memory, and a pod
that was never scheduled. Only one of them has logs.

## Read-only by design

The MCP server exposes four tools and none of them mutate anything:
`list_pods`, `get_pod_events`, `get_logs`, `describe_deployment`.

There is no write path. Nobody wants an LLM holding `kubectl delete`.

## Tests

Fixtures are **real API responses** captured from a kind cluster running
Kubernetes 1.36 — not hand-written dicts. That distinction caught a live bug:
a crashlooping container alternates between `state.waiting` (backing off) and
`state.terminated` (just died). A check keyed only on
`state.waiting.reason == "CrashLoopBackOff"` returns False roughly half the
time depending on when you sample it. An invented fixture would have encoded
the same wrong assumption as the code and the test would have passed.

The check keys on `restartCount` instead, which is monotonic and doesn't race.

The first live run surfaced a second one. `payments-api`, broken by a missing
ConfigMap key, tripped none of the five checks: `restartCount` was 0, so not a
crashloop; `lastState` was empty, so not OOMKilled; the waiting reason was
`CreateContainerConfigError`, so not an image pull failure. Zero findings on a
visibly broken pod — worse than a wrong diagnosis, because the operator
concludes it's fine.

The cause is that `CrashLoopBackOff` requires a container that *ran and died*.
When the kubelet can't assemble the environment, no container is ever created,
so it never runs, never crashes, and `restartCount` stays 0 forever. That's
`CONFIG_ERROR`, and its test asserts the other three checks stay silent on it.

## Project Structure

```
k8s-triage-agent/
    agent/
        triage.py           # entry point, --namespace / --output
        prompts.py          # system prompt
    mcp_server/
        server.py           # 4 read-only tools
        summarize.py        # pure pod -> findings assembly
        checks/
            pods.py         # container-level: crashloop, OOM, imagepull
            scheduling.py   # pod-level: unschedulable
            config.py       # pod-level: missing limits
    broken-cluster/         # three pods that break on purpose
    tests/
        fixtures/           # real captured API responses
        test_checks.py
```

## Built With

Claude Agent SDK · Model Context Protocol · Kubernetes Python client ·
Python 3.12 · pytest · Langfuse · GitHub Actions
