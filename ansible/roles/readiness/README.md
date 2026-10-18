# Role `readiness`

Within one `readiness_timeout` deadline: the unit (`k3s` or `k3s-agent`) is
active, the node's own `Ready` condition is `True`, and its kubeconfig is not
readable by others. A failure names the predicate and appends the k3s journal
tail.

## Invariants

- Servers read `/etc/rancher/k3s/k3s.yaml` as `ssh_user` (no become), which
  proves the kubeconfig group access end to end; agents read the kubelet
  kubeconfig as root.
- `Unauthorized|Forbidden|x509` in kubectl's stderr fails at once: the one
  documented exception to never matching tool text (AGENTS.md); kubectl
  exits 1 for every error. Other errors are retried until the deadline.
- Skipped inside Docker containers (AGENTS.md, documented exception).

## Dependencies

Reads `inventory_readiness_seconds` from `hostvars['localhost']`.

## Variables

Inputs, types and allowed values: `meta/argument_specs.yml`, checked by Ansible on role entry; render with `ansible-doc -t role -r ansible/roles readiness`.
