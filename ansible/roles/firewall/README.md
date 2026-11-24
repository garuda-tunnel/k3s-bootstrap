# Role `firewall`

With `firewall_mode: managed` — on `initial`, `server` and `agent` nodes
alike — installs one isolated nftables table, `inet k3s_bootstrap`. It
allows loopback on all ports and gates other traffic as follows (IPv4 and
IPv6):

| Ports | Additional allowed sources |
| --- | --- |
| TCP 2379, 2380, 6443 | `api_trusted_cidrs`, `cluster_cidrs`, `node_trusted_cidrs` |
| TCP 10250 (kubelet) | `cluster_cidrs`, `node_trusted_cidrs` |
| UDP 8472 (Flannel VXLAN) | `node_trusted_cidrs` |

Other sources are dropped on those ports; unrelated ports and other
filters are unchanged. Node trust implies API/etcd trust because a peer
node cannot function without the API (and with the embedded registry
mirror every node serves 6443 to its peers), so peers are listed once,
in `node_trusted_cidrs`. Agents are included because they serve kubelet
and flannel like servers do, and an open agent would re-open the
unauthenticated VXLAN overlay for the whole cluster. `off` mode touches
nothing, and preflight refuses `off` while this table exists. See the
root README for multi-node upgrade guidance and the WireGuard ports.

## Invariants

- The ruleset is validated with `nft -c` before it replaces the live table;
  a change is applied by reloading the unit `k3s-bootstrap-firewall`, one
  atomic `nft -f` transaction, never by a restart (which would leave the
  ports open in between).
- Other nftables tables are never touched.
- Preflight refuses `off` while the module firewall exists; removing it is
  the operator's step (see `ansible/README.md`).

## Dependencies

None.

## Variables

Inputs, types and allowed values: `meta/argument_specs.yml`, checked by Ansible on role entry; render with `ansible-doc -t role -r ansible/roles firewall`.
