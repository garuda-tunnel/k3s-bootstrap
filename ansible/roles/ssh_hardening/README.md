# Role `ssh_hardening`

Installs `/etc/ssh/sshd_config.d/00-k3s-bootstrap.conf` (password and
keyboard-interactive login off, root login off) and reloads sshd before any
later role can fail.

## Invariants

- `sshd -t` validates the complete configuration. On a failed validation or
  reload the previous drop-in is restored (or the new one removed), sshd is
  validated and reloaded again, and the run fails.
- It flushes handlers itself, so it runs before `host` in the Host play: a
  reboot queued by `host` must not fire inside this role's block.

## Dependencies

Runs only after the play `Verify new access` proved key login as
`ssh_user`.

## Variables

Inputs, types and allowed values: `meta/argument_specs.yml`, checked by Ansible on role entry; render with `ansible-doc -t role -r ansible/roles ssh_hardening`.
