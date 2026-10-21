# k3s-bootstrap

Generic OpenTofu module that prepares **one** k3s node over SSH: a hardened
host plus k3s as an `initial` node (the first server, embedded etcd), a
`server` that joins it, or an `agent`. No cloud provider: the host is any
reachable Ubuntu 26.04 LTS machine.

## How it works

One module call is one node. Its only resource, `terraform_data.converge`,
runs one `local-exec` on the operator's machine (the controller):

```sh
uv run --no-project --with-requirements ansible/requirements.txt \
  ansible-playbook -i localhost, ansible/k3s-node.yml -e "$K3SB_VARS"
```

The playbook connects to the node over SSH and does every host change:
preflight checks, the admin user (`ssh_user`, passwordless sudo, keys),
SSH hardening (key-only, root login off, applied only after a fresh login as
`ssh_user` succeeded), kernel modules and sysctls, the managed firewall,
k3s, and a bounded readiness check. Nothing is copied to or installed on
the node for the run. On a **first install** only, if the host asks for a
reboot (`/run/reboot-required`), it reboots with `ansible.builtin.reboot`
before k3s is installed; an installed or adopted node is never rebooted.

A `server` or `agent` joins through its `head`, a node that already runs
the cluster. The playbook reads the join data from the head over SSH,
inside the run:

- `server`: the head's server token (`/var/lib/rancher/k3s/server/token`);
- `agent`: a short-lived bootstrap token minted on the head
  (`k3s token create`, TTL `readiness_timeout` + 5 minutes) and deleted on
  the head when the run ends, joined or not.

Either lands in `/etc/rancher/k3s/join-token` (0600 root) on the node. No
token is a variable, an output, a state value, a playbook input or a log
line. Once a converge completed (the node's marker
`/var/lib/k3s-bootstrap/installed`), the node never reads or mints a token
again.

The run re-executes when an input, `host_generation` or a file under
`ansible/` changes. A failed run leaves the resource tainted; the next apply
retries it, and the playbook is idempotent.

## Usage

```hcl
module "hub" {
  source = "git::https://github.com/example-org/k3s-bootstrap.git?ref=vX.Y.Z"

  node_type            = "initial"
  host                 = "192.0.2.10"
  host_generation      = "instance-0001"
  ssh_user             = "admin"
  ssh_private_key_file = "/secure/k3s-bootstrap.ed25519"
  authorized_keys      = ["ssh-ed25519 AAAA... admin@example"]
  k3s_version          = "v1.36.5+k3s1"
  cluster_cidrs        = ["10.42.0.0/16", "10.43.0.0/16"]
  api_trusted_cidrs    = ["192.0.2.0/24"] # the controller and every joining node
}

module "server_2" {
  source     = "git::https://github.com/example-org/k3s-bootstrap.git?ref=vX.Y.Z"
  node_type  = "server"
  head       = module.hub.head
  # ... host, host_generation, ssh_user, keys, k3s_version, cidrs as above
}

module "server_3" {
  source     = "git::https://github.com/example-org/k3s-bootstrap.git?ref=vX.Y.Z"
  node_type  = "server"
  head       = module.hub.head
  depends_on = [module.server_2] # servers join one at a time
  # ...
}

module "agent" {
  source    = "git::https://github.com/example-org/k3s-bootstrap.git?ref=vX.Y.Z"
  for_each  = var.agents # agents join in parallel
  node_type = "agent"
  head      = module.hub.head
  # ...
}
```

`tests/vm/fixture/cluster/main.tf` is a complete, tested composition.

## Operator duties

- **Controller**: `uv` on `PATH`; the run uses the pinned
  `ansible/requirements.txt` in an isolated environment and fails before
  touching a host if the toolchain differs.
- **Host keys**: with `host_key_check = true` (default) the node's and the
  head's keys must already be in your `known_hosts` (`ssh-keyscan` once,
  then persist it; never rescan on every apply). An unknown or changed key
  fails the run. `false` trusts any key.
- **One key**: `ssh_private_key_file` reaches the node and its head; the
  module passes the path, never the key material.
- **Server order**: servers join one at a time; order them with
  `depends_on`. Agents may apply in parallel.
- **Firewall**: in `managed` mode the node gates the k3s API (6443) and
  etcd (2379-2380) to loopback, `api_trusted_cidrs` and `cluster_cidrs`.
  List the controller and every node that joins through this head (or the
  other servers' addresses, for etcd) in its `api_trusted_cidrs`.
- **Back up the cluster token**: `/var/lib/rancher/k3s/server/token` on a
  server is the cluster's identity for every future server join and for
  datastore restores.
- **Your own k3s settings**: `/etc/rancher/k3s/config.yaml` and the k3s
  unit belong to the module; every converge renders them from the inputs and
  replaces manual edits. Keep settings you manage outside the module in
  `/etc/rancher/k3s/config.yaml.d/*.yaml` (k3s merges them after
  `config.yaml`: the last value wins, `key+` appends) and in unit drop-ins
  (`systemctl edit k3s`); the module never writes there, and a converge
  restarts k3s when a fragment changed after it started. Do not override
  the module-owned keys there (`server`, `token-file`, `cluster-init`, the
  kubeconfig mode and group); `data-dir` and `datastore-endpoint` are
  refused.
- **No secrets in `k3s_config`**: it is passed on the controller command
  line and hashed into the re-run trigger. Join tokens are never input.
- **Agent rejoin limit**: an agent that must request a new client
  certificate without a valid one (offline past expiry, or certificates
  rotated on the agent) can no longer authenticate, because its bootstrap
  token was deleted. Remove `/var/lib/k3s-bootstrap/installed` on it and
  re-run its module with
  `tofu apply -replace='module.<name>.terraform_data.converge'` (removing
  the marker changes no input, so a plain apply does nothing): the run mints
  a new token and rejoins it.

## Refusals

Preflight refuses, before changing anything: a host other than Ubuntu
26.04 with systemd; an existing SQLite datastore (the module is etcd only
and never converts; migration is out of scope); a k3s layout it cannot see
(custom `data-dir` or `datastore-endpoint` in units, environment or k3s
config files, or a running k3s without recognisable data); a change of
node type, server/agent role or datastore; a k3s downgrade; a node whose
cluster CA differs from its head's; an unreachable head or one that is not
an initialised server; enabled ufw or firewalld with `firewall_mode =
"managed"`; and `firewall_mode = "off"` over an existing module firewall
(remove it by hand: `systemctl disable --now k3s-bootstrap-firewall`,
`nft destroy table inet k3s_bootstrap`, delete the unit and
`/etc/k3s-bootstrap/firewall.nft`, `systemctl daemon-reload`).

## Inputs and outputs

See `variables.tf` and `outputs.tf` for the annotated list. `head` is
required for `server` and `agent` and forbidden for `initial`.
`k3s_version` must be an exact pin such as `v1.36.5+k3s1`. `k3s_config`
rejects module-owned keys (`token`, `token-file`, `agent-token`,
`agent-token-file`, also as `key+`, `server`, `cluster-init`,
`write-kubeconfig-mode`, `write-kubeconfig-group`) and keys that bypass
its data safety (`data-dir`, `datastore-endpoint`, `cluster-reset`,
`cluster-reset-restore-path`).

| Optional input | Type | Default | Description |
|---|---|---|---|
| `extra_packages` | `list(string)` | `[]` | Additional apt packages to install (`state: present`, never removed by the module). Each name must match `^[a-z0-9][a-z0-9+.-]+$`. Listing `qemu-guest-agent` also enables and starts its service. |

Outputs, all null on agents and available once the node is ready: `head`
(this node's SSH coordinates, for `head = module.<name>.head`),
`kubeconfig_path` (`/etc/rancher/k3s/k3s.yaml`, readable by `ssh_user`'s
primary group, which must be private or admin-only) and
`api_forward_target` (`127.0.0.1:6443`).

## Testing

| Tier | What it proves | Needs | Command (repository root) |
|---|---|---|---|
| `tofu test` | plan-time interface: input validation, outputs, re-run trigger | OpenTofu | `tofu test` |
| controller | the `inventory` role, role documentation, launch parity, run hash; no host | `uv` venv | `.venv/bin/pytest tests/controller` |
| container | preflight, access, SSH hardening, host refusals and a full module apply/re-apply against systemd containers; no k3s | privileged Docker, OpenTofu, `uv` venv | `.venv/bin/pytest -v tests/container` |
| molecule | role behaviour on real VMs: `default`, `host`, `cluster`, `firewall`, `recovery` | libvirt, Vagrant, activated venv | `molecule test -s <scenario>` |
| `tests/vm` | module contracts with real `tofu apply` on Vagrant VMs | libvirt, Vagrant, `K3SB_VM_TESTS=1` | `pytest -s tests/vm` |

Setup for the Python tiers: `uv venv && uv pip install -r tests/requirements.txt`.
The molecule and VM tiers are manual, never in CI, and run through
`/usr/bin/sg libvirt -c '...'` unless your session already carries the
`libvirt` group; see `tests/README.md`, `molecule/README.md` and
`tests/vm/README.md`.

## Requirements

- OpenTofu >= 1.9.0.
- Controller: `uv`, `ssh`.
- Target host: Ubuntu 26.04 LTS with systemd and Python 3.
