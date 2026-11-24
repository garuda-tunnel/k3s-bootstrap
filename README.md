# k3s-bootstrap

Generic OpenTofu module that prepares **one** k3s node over SSH: a hardened
host plus k3s as an `initial` node (the first server, embedded etcd), a
`server` that joins it, or an `agent`. No cloud provider: the host is any
reachable Ubuntu 26.04 LTS or Debian 13 machine.

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
k3s, and a bounded readiness check. Ansible runs on the controller and executes modules over SSH; those tasks
install packages and write configuration on the node. The node does not need
an Ansible installation. On a **first install** only, if the host asks for a
reboot (`/run/reboot-required`), it reboots with `ansible.builtin.reboot`
before k3s is installed; an installed or adopted node is never rebooted.
Ubuntu writes that file when an update needs a restart; stock Debian does
not, so on Debian the reboot fires only if something else creates the file.

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
  api_trusted_cidrs    = ["203.0.113.10/32"] # controller and operators only, not peers
  node_trusted_cidrs   = ["192.0.2.11/32", "192.0.2.12/32"] # peers once; derive from node-ip: [for n in local.k3s_nodes : "${n.node_ip}/32"]
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
  head's keys must already be in your `known_hosts` (collect once, verify the fingerprints through a trusted channel,
  then persist them; never rescan on every apply). An unknown or changed key
  fails the run. `false` trusts any key.
- **One key**: `ssh_private_key_file` reaches the node and its head; the
  module passes the path, never the key material.
- **Server order**: servers join one at a time; order them with
  `depends_on`. Agents may apply in parallel.
- **Firewall**: on every managed node — `initial`, `server` and `agent`
  alike — the following inbound sources are allowed; other sources are
  dropped on those ports only, for IPv4 and IPv6:

  | Ports | Allowed sources (plus loopback) |
  | --- | --- |
  | TCP 6443, 2379-2380 | `api_trusted_cidrs`, `cluster_cidrs`, `node_trusted_cidrs` |
  | TCP 10250 (kubelet) | `cluster_cidrs`, `node_trusted_cidrs` |
  | UDP 8472 (Flannel VXLAN) | `node_trusted_cidrs` |

  `api_trusted_cidrs` lists only sources that are not cluster nodes: the
  controller and operators. Peer nodes are listed once, in
  `node_trusted_cidrs`, which also grants the API and etcd ports, because a
  peer cannot function without the API (and with the embedded registry
  mirror every node serves 6443 to its peers). `cluster_cidrs` is required
  on every managed node. Derive `node_trusted_cidrs` from your node list
  instead of hand-maintaining it, from the addresses peers actually send
  from: each node's `node-ip` (or its flannel interface address), which is
  not necessarily the SSH `host` (NAT or a separate management interface
  in between); for example `[for n in local.k3s_nodes : "${n.node_ip}/32"]`
  (`/128` for IPv6). Listing the node itself is harmless. The initial
  server needs
  `node_trusted_cidrs` too once other nodes join; the plan does not catch
  an empty list there (initial is exempt because a standalone initial
  node has no peers), so populate it yourself when the first joiner
  arrives. On an installed server (`initial` or `server`, k3s present)
  preflight closes that gap at converge time: it reads every other
  node's InternalIP addresses from the node's own API and refuses the
  apply while any peer is outside `node_trusted_cidrs` (when the API is
  down the run continues with a warning; see Refusals). With
  metrics-server, pods scraping a remote kubelet arrive
  with the source node IP — flannel masquerades pod traffic to node
  addresses by default on IPv4; IPv6 masquerade needs
  `flannel-ipv6-masq`, and without it IPv6 pod traffic keeps the pod
  source, which `cluster_cidrs` already trusts — which is why 10250
  trusts node sources. SSH and unrelated
  ports are unchanged; `off` mode does not manage this firewall. Other
  host or network filters still apply.
- **Flannel backend**: the module sets `flannel-backend: wireguard-native`
  on servers unless your `k3s_config` sets `flannel-backend` (your value
  always wins; flannel options are server-only and agents follow the
  servers). WireGuard encrypts the overlay and authenticates peers by key,
  silently dropping everything else, so the firewall opens no UDP 51820 or
  51821 rule and cannot help there. UDP 51820 and 51821 are reserved for
  flannel: no hostPort or host service may use them. Callers that keep
  VXLAN set `k3s_config = { flannel-backend = "vxlan" }` and must keep
  `node_trusted_cidrs` covering the peers: VXLAN has no authentication and
  UDP sources can be spoofed, so on a public underlay prefer
  `wireguard-native`. The module loads the `wireguard` kernel module on
  every node. The embedded registry mirror (Spegel), when enabled, makes
  every node serve TCP 6443 and peers exchange on TCP 5001; 6443 is already
  covered by `node_trusted_cidrs` on every node, 5001 is not gated by this
  table.
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
  the module-owned keys there (`server`, `token-file`, `cluster-init`,
  the kubeconfig mode and group, `flannel-backend`); set the latter in
  `k3s_config` instead. `data-dir` and `datastore-endpoint` are
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

## Upgrading from v0.3.x (breaking)

1. **Agents are now firewalled** in `managed` mode (the default): they get
   the same nftables table as servers, because they serve kubelet and
   flannel and an open agent would re-open the unauthenticated VXLAN
   overlay for the whole cluster. Agents that must stay unmanaged now set
   `firewall_mode = "off"` explicitly; removing an existing module
   firewall is still the operator's step (see Refusals).
2. **In `managed` mode, `server` and `agent` nodes require non-empty
   `node_trusted_cidrs`, and agents now require `cluster_cidrs`**, both
   checked at plan time (`off` mode is exempt).
   The plan does not catch an empty list on the `initial` node (initial
   is exempt because a standalone initial node has no peers), so
   populate `node_trusted_cidrs` on the initial server yourself once
   joiners exist, together with the joiners' own lists. The converge-time
   guard refuses the apply while any peer the node's API reports is
   outside `node_trusted_cidrs`, so a v0.3 cluster whose head is not
   covered fails its first converge with the uncovered peers named,
   instead of silently dropping them; fix the list and apply again.
3. **The default flannel backend is `wireguard-native`.** Existing
   multi-node VXLAN clusters must set `flannel-backend: vxlan` in
   `k3s_config` **before** upgrading; otherwise pod networking partitions
   during the rollout: k3s/flannel leave the old `flannel.1` link in
   place when the backend changes, and nodes running different backends
   cannot talk to each other. To switch a running cluster deliberately,
   change the config on all servers, then reboot every node (servers
   first), and rehearse it on a scratch cluster first (see
   <https://github.com/k3s-io/k3s/discussions/8868>). A k3s service
   restart alone is not enough: the old per-peer routes via `flannel.1`
   are more specific than the WireGuard backend's cluster route and keep
   winning until the node reboots. Any other firewall on the path must
   pass peer UDP 51820/51821. Standalone single-node clusters switch on
   their next restart, with the stale `flannel.1` link left behind until
   a reboot. Preflight refuses the silent path: an installed server whose
   `k3s_config` sets no `flannel-backend` and that does not already run
   `wireguard-native` fails the run; set `flannel-backend: vxlan` to keep
   the cluster as it is, or `flannel-backend: wireguard-native` to perform
   the switch above deliberately.

## Refusals

Preflight refuses, before changing anything: a host other than Ubuntu
26.04 or Debian 13 with systemd; an existing SQLite datastore (the module
is etcd only
and never converts; migration is out of scope); a k3s layout it cannot see
(custom `data-dir` or `datastore-endpoint` in units, environment or k3s
config files, or a running k3s without recognisable data); a change of
node type, server/agent role or datastore; a k3s downgrade; an installed
server that would silently switch flannel backend (`k3s_config` without
`flannel-backend` and the node not already on `wireguard-native`; set the
key, see [Upgrading](#upgrading-from-v03x-breaking)); a node whose
cluster CA differs from its head's; an unreachable head or one that is not
an initialised server; enabled ufw or firewalld with `firewall_mode =
"managed"`; an installed server (`initial` or `server`) whose cluster
peers, read from its own node API, are not covered by `node_trusted_cidrs`
(a warning instead when the API does not answer); and `firewall_mode = "off"` over an existing module firewall
(remove it by hand: `systemctl disable --now k3s-bootstrap-firewall`,
`nft destroy table inet k3s_bootstrap`, delete the unit and
`/etc/k3s-bootstrap/firewall.nft`, `systemctl daemon-reload`).

The k3s play also refuses a missing distro `/etc/apparmor.d/runc` when
AppArmor and `apparmor_container_profile_fix` are enabled and
`kernel.apparmor_restrict_unprivileged_unconfined=1`, or an unsupported
distro runc profile declaration. Restore the distro policy and rerun.

## AppArmor embedded runc attachment

`apparmor_container_profile_fix = true` extends the distro `runc` profile to
k3s's embedded runtime. This prevents a plain `unconfined` stack member from
breaking signals and attaching runtime-profiled containers to unrelated host
profiles on restricted kernels. Recreate affected pods after rollout.

Unprofiled workloads can inherit the named, unconfined `runc` profile,
including its `userns` permission and host-profile exec attachment behavior.
This does not add container confinement. Setting the input to `false`
restores module-managed files to the stock policy without unloading live
profiles; existing processes are not relabelled.

The [k3s role documentation](ansible/roles/k3s/README.md#runc-apparmor-attachment)
contains the activation prerequisites, dependency guarantees and rollback
procedure. Read it before changing this setting.

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
| `apparmor_container_profile_fix` | `bool` | `true` | Extend the distro `runc` attachment to embedded runc, preventing forced stacking and unintended host-profile transitions for runtime-profiled containers. Unprofiled workloads can inherit `runc`, including `userns` permission and unconfined-style exec attachment into host profiles. Requires AppArmor, its boot service enabled and the distro runc file (missing policy fails on restricted kernels). False restores module-managed nodes to stock without unloading live policy. See [AppArmor attachment](#apparmor-embedded-runc-attachment). |

Outputs, all null on agents and available once the node is ready: `head`
(this node's SSH coordinates, for `head = module.<name>.head`),
`kubeconfig_path` (`/etc/rancher/k3s/k3s.yaml`, readable by `ssh_user`'s
primary group, which must be private or admin-only) and
`api_forward_target` (`127.0.0.1:6443`).

## Testing

| Tier | What it proves | Needs | Command (repository root) |
|---|---|---|---|
| `tofu test` | plan-time interface: input validation, outputs, re-run trigger | OpenTofu | `tofu test` |
| controller | inventory, role documentation, AppArmor policy transformation/preflight/reconcile/ownership, launch parity, run hash; no host | `uv` venv | `.venv/bin/pytest tests/controller` |
| container | preflight, access, SSH hardening, host refusals, native AppArmor parsing and full module apply/re-apply against systemd containers; no k3s | privileged Docker, OpenTofu, `uv` venv | `.venv/bin/pytest -v tests/container` |
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
- Target host: Ubuntu 26.04 LTS or Debian 13, with systemd and Python 3,
  and the `wireguard` kernel module available: preflight checks it on
  every node regardless of the flannel backend, because the module's
  default backend needs it on servers and agents alike.

