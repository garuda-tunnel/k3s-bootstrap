# Role `access`

Creates `ssh_user` when absent (an existing account keeps its uid, home and
groups), authorizes `authorized_keys` and installs passwordless sudo
(`/etc/sudoers.d/90-k3s-bootstrap`, validated by `visudo`).

## Invariants

- Keys are a plain upsert (`exclusive: false`): keys already there stay.
- The proof that the new access works is the play `Verify new access` in
  `ansible/k3s-node.yml`: a fresh, key-only connection as `ssh_user` and a
  `sudo` check, tagged with `ssh_hardening` so hardening never runs without
  it.

## Dependencies

Runs as the connection user the `inventory` role selected (`bootstrap_user`
on a fresh node).

## Variables

Inputs, types and allowed values: `meta/argument_specs.yml`, checked by Ansible on role entry; render with `ansible-doc -t role -r ansible/roles access`.
