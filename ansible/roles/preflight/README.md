# Role `preflight`

Refuses an unsupported or unsafe node before any role changes it; the role
itself changes nothing.

## Invariants

- Ubuntu 26.04 with systemd; no caller or foreign sysctl that conflicts with
  the k3s-owned bridge sysctls; with `firewall_mode: managed` no enabled ufw
  (config or loaded chains, decided by file content and an exit code, never
  by `ufw status` text) and no running firewalld; with `off` no existing
  module firewall.
- k3s safety: embedded etcd is the only datastore. SQLite and layouts the
  module cannot see (custom `data-dir`/`datastore-endpoint` in units,
  environment or k3s config files; a running k3s without recognisable data)
  are refused. The class of the existing install (`none`, `etcd`, `agent`,
  `sqlite`, `unknown`) must match `node_type`; the marker
  `/var/lib/k3s-bootstrap/installed` records the node type and refuses a
  change; a downgrade is refused.
- The module's own unfinished first install (only module-managed units, no
  marker, no datastore) is `none`, so a failed run can be retried. For
  `initial` a server CA keeps it out of that class: `cluster-init` over
  copied bootstrap data would start a second cluster on another's CA.
- `server` and `agent` (`tasks/head.yml`, delegated to the head): the head is
  reachable and an initialised server (its token file is only `stat`ed);
  any local cluster CA must equal the head's.
- Host capability: every kernel module (`br_netfilter`, `overlay`,
  `kernel_modules`) is loadable (`modinfo`, nothing loaded); with
  `grow_root_filesystem` (the default) the root is ext4 or xfs on a direct
  disk partition, otherwise the node is refused.
- Unit and environment files may hold credentials: they are read with
  `no_log`.

## Dependencies

Provides the node facts `preflight_k3s_installed_class`,
`preflight_k3s_pristine` (no k3s data and none running: the only state the
`host` role may reboot), `preflight_installed_marker` and
`preflight_k3s_installed_version`, `preflight_kernel_modules` (every one
checked loadable with `modinfo`) and, with `grow_root_filesystem`, the
verified root layout `preflight_root`, `preflight_root_disk` and
`preflight_root_partn`, read by the `host` and `k3s` roles in the
same run. Reads `k3s_owned_sysctls` and `k3s_marker_path` from
`ansible/group_vars/all.yml`.

## Variables

Inputs, types and allowed values: `meta/argument_specs.yml`, checked by Ansible on role entry; render with `ansible-doc -t role -r ansible/roles preflight`.
