# Deploy

Read-only RBAC for the triage agent.

`rbac.yaml` defines the identity the agent runs as in-cluster and constrains
it to reading pods, logs, events, and deployments. It is the enforcement
behind the "read-only by design" claim: the constraint lives at the API
server, not in the Python code or the prompt.

## Apply

```bash
kubectl create namespace triage-agent
kubectl apply -f deploy/rbac.yaml
```

## Prove it's correct

A permission manifest is only as good as your ability to show it does what it
claims. `kubectl auth can-i` answers "could this identity do X?" without
deploying anything — it asks the API server directly.

The `--as` flag impersonates the agent's ServiceAccount, so these check the
agent's real authority, not yours.

Allowed — every one should print **yes**:

```bash
kubectl auth can-i list pods         --as=system:serviceaccount:triage-agent:triage-agent
kubectl auth can-i get  pods/log     --as=system:serviceaccount:triage-agent:triage-agent
kubectl auth can-i list events       --as=system:serviceaccount:triage-agent:triage-agent
kubectl auth can-i get  deployments  --as=system:serviceaccount:triage-agent:triage-agent
```

Denied — every one should print **no**. This half matters more: it proves the
role is tight, not just functional.

```bash
kubectl auth can-i delete pods       --as=system:serviceaccount:triage-agent:triage-agent
kubectl auth can-i create deployments --as=system:serviceaccount:triage-agent:triage-agent
kubectl auth can-i get secrets       --as=system:serviceaccount:triage-agent:triage-agent
kubectl auth can-i '*' '*'           --as=system:serviceaccount:triage-agent:triage-agent
```

The `get secrets` denial is the one to notice. A read-only role that could
still read Secrets would leak credentials, TLS keys, and tokens — "read-only"
is not the same as "safe". This role can't touch them.