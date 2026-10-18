# Role `firewall`

With `firewall_mode: managed` on `initial` and `server`, installs one
isolated nftables table, `inet k3s_bootstrap`, that lets loopback,
`api_trusted_cidrs` and `cluster_cidrs` reach the k3s API (6443) and the
embedded etcd client and peer ports (2379-2380, which etcd binds on the node
address) and drops everyone else. Agents and `off` mode touch nothing.

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
