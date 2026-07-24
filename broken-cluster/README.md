# broken-cluster

Three pods that fail in three different ways, on purpose. This is the demo.

```bash
kind create cluster --name triage
kubectl apply -f broken-cluster/
sleep 60   # CrashLoopBackOff needs a few restart cycles to appear
python -m agent.triage --namespace default
```

## What breaks and why

| Manifest | Pod | Symptom | Root cause |
|---|---|---|---|
| `crashloop.yaml` | `payments-api` | CrashLoopBackOff | Mounts ConfigMap key `db_host` that doesn't exist in `app-config` |
| `oomkill.yaml` | `report-generator` | CrashLoopBackOff | OOMKilled — 10Mi limit, allocates ~100Mi |
| `pending.yaml` | `ml-trainer` | Pending | Requests 64Gi, no node can satisfy it |

Note that two of the three present as the *same* symptom. `kubectl get pods`
shows CrashLoopBackOff for both `payments-api` and `report-generator` and
gives you no way to tell them apart. The causes are unrelated: one is a
config error, one is a memory limit. Separating them is the point of the
agent.

`ml-trainer` also trips the missing-limits check, since it declares requests
but no limits. Acute and latent problems coexist.

## Cleanup

```bash
kubectl delete -f broken-cluster/
# or throw the whole cluster away
kind delete cluster --name triage
```
