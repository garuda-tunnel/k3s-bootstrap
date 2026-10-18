# Ansible: `k3s-node.yml`

The playbook runs on the controller and reaches one node (and, for `server`
and `agent`, its head) over SSH. The supported launch is the module's
`local-exec` (`main.tf`):

```sh
uv run --no-project --with-requirements ansible/requirements.txt \
  ansible-playbook -i localhost, ansible/k3s-node.yml -e "$K3SB_VARS"
```

`requirements.txt` is the only toolchain pin; `ansible.cfg` keeps user-level
collections out (`collections_path` empty) and command lines out of the output.
Any other environment is the caller's responsibility.

## Plays, in order

| Play | Hosts, user | Tags | Roles or tasks |
|---|---|---|---|
| `Inventory` | controller | `always` | [`inventory`](roles/inventory/README.md) |
| `Preflight` | node, connection user | `preflight` | [`preflight`](roles/preflight/README.md) |
| `Access` | node, connection user | `access` | [`access`](roles/access/README.md) |
| `Verify new access` | node, `ssh_user` | `access`, `ssh_hardening` | A fresh, key-only connection as `ssh_user` and a passwordless `sudo` check; tagged with `ssh_hardening` so hardening never runs without it. |
| `Host` | node, `ssh_user` | per role | An `always` pre-task makes `ssh_user` the node's connection user for the rest of the run (a node fact, so tasks delegated to the head keep `head.user`); [`ssh_hardening`](roles/ssh_hardening/README.md), [`host`](roles/host/README.md), [`firewall`](roles/firewall/README.md); then `flush_handlers` runs a first-install reboot before k3s. |
| `K3s` | node, `ssh_user` | `k3s` | [`k3s`](roles/k3s/README.md) |
| `Readiness` | node, `ssh_user` | `readiness` | [`readiness`](roles/readiness/README.md) |

Each role documents what it does, its invariants and what it reads from other
roles in its `README.md`; its inputs live in `meta/argument_specs.yml`, which
Ansible checks on role entry (render one with
`ansible-doc -t role -r ansible/roles <role>`). Shared constants read by more
than one role are in `group_vars/all.yml`.

Every play but `Inventory` uses play-level `become` (AGENTS.md, documented
exceptions). Tag-filtered runs must keep `preflight`: the `host` and `k3s`
roles read its facts and the `k3s` role refuses to run without them.

## Off mode

`firewall_mode: off` never removes rules. Preflight refuses it while the
module unit `/etc/systemd/system/k3s-bootstrap-firewall.service` or the
`inet k3s_bootstrap` table exists, and fails if `nft` is missing while that
unit or `/etc/k3s-bootstrap/firewall.nft` exists. Remove the module firewall
by hand first: `systemctl disable --now k3s-bootstrap-firewall`,
`nft destroy table inet k3s_bootstrap`, delete the unit and the ruleset file,
`systemctl daemon-reload`.
