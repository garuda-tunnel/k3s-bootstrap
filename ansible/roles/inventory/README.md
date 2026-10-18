# Role `inventory`

Runs on the controller (play `Inventory`, tag `always`), before any host is
reached. It asserts the pinned toolchain (`ansible`, `ansible-core` and one
module of each bundled collection, against `ansible/requirements.txt`),
validates the inputs, derives the connection settings and registers the node
and, for `server` and `agent`, its head with `add_host`.

It probes whether `ssh_user` can already log in with the key and registers the
node with that user, else with `bootstrap_user`, so a re-run of a hardened
node (root login off) still connects. `add_host` reports no change, so
Molecule's idempotence step stays meaningful.

## Invariants

- Types, required inputs and choices come from the argument spec. The rules
  that format cannot express are asserts here: `head` exactly for `server`
  and `agent`, the timeout format, `ssh_user` never root, and no
  `cluster-reset` or `cluster-reset-restore-path` in `k3s_config` (they wipe
  the datastore outside the module's checks).
- Host keys: `host_key_check` sets `StrictHostKeyChecking=yes` against the
  caller's `known_hosts`, or `no` with `/dev/null`; the hostvars outrank an
  inherited `ANSIBLE_HOST_KEY_CHECKING`.

## Dependencies

Provides, as `hostvars['localhost']`: `inventory_connection_seconds`,
`inventory_readiness_seconds`, `inventory_ssh_host_key_args` and
`inventory_connection_user`. Later plays, the `host` handler and the `k3s`
and `readiness` roles read them.

## Variables

Inputs, types and allowed values: `meta/argument_specs.yml`, checked by Ansible on role entry; render with `ansible-doc -t role -r ansible/roles inventory`.
