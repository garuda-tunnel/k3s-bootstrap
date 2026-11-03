# Role `host`

Installs the caller's extra packages (`extra_packages`, apt `state:
present`; an empty list installs nothing; listing `qemu-guest-agent`
also enables and starts its service), loads and persists the kernel
modules (`br_netfilter`, `overlay` and `kernel_modules`), applies the
k3s-owned and caller sysctls (`/etc/sysctl.d/90-k3s-bootstrap.conf`),
optionally grows the root partition and filesystem, and requests the
first-install reboot.

## Invariants

- The role only changes the host: the module list and the root growth
  layout are checked by `preflight` before any change of the run; a missing
  k3s-owned sysctl fails in `ansible.posix.sysctl` itself.
- Extra packages are installed `state: present` and never removed or
  upgraded by the module; the package names are validated at plan time
  (`^[a-z0-9][a-z0-9+.-]+$`).
- Root growth: `growpart` (exit code 1 means already full), then
  `resize2fs` or `xfs_growfs`; `changed` is decided by the sizes before and
  after, never by tool output. Only ext4 or xfs on a direct partition.
- Reboot: only when `/run/reboot-required` exists and the node is
  pristine (`preflight_k3s_pristine`). Ubuntu writes that file when an
  update needs a restart; stock Debian does not, so on Debian it appears
  only if another component creates it. The handler `reboot required` runs
  `ansible.builtin.reboot` bounded by `connection_timeout` (the request lives
  on tmpfs, so the reboot clears it); the Host play flushes it before k3s is
  installed. An installed, adopted or partly installed node is never
  rebooted.

## Dependencies

Reads `preflight_k3s_pristine`, `preflight_kernel_modules` and, with
`grow_root_filesystem`, `preflight_root`, `preflight_root_disk` and
`preflight_root_partn` from the preflight play,
`inventory_connection_seconds` from `hostvars['localhost']` and
`k3s_owned_sysctls` from `ansible/group_vars/all.yml`.

## Variables

Inputs, types and allowed values: `meta/argument_specs.yml`, checked by Ansible on role entry; render with `ansible-doc -t role -r ansible/roles host`.
