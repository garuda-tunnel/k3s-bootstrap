# Molecule scenarios

Role-level tests on real Ubuntu 26.04 VMs (Vagrant over libvirt). Every
scenario launches `ansible/k3s-node.yml` from the controller with the
module's own command and environment (`shared/tasks/apply.yml`, kept equal
to `main.tf` by `tests/controller/test_launch_parity.py`), so the roles are
tested as the module runs them. A launch is judged by its own PLAY RECAP
(`shared/tasks/launch.yml`), so Molecule's idempotence step sees every
change. The verifier is Ansible
(`verify.yml` per scenario), not testinfra. Manual, never in CI; for the
module-level tier see `tests/vm/README.md`.

## Scenarios

Each runs `create, prepare, converge, idempotence, verify, destroy`.
`shared/prepare.yml` authorizes one ephemeral controller key on every VM,
so a node and its head share one `ssh_private_key_file`. Refusals use
`shared/tasks/reject.yml`: the failing task (never its message) and an
unchanged scenario snapshot.

Non-empty `cluster_cidrs` is covered at plan time by the interface tier;
the VM scenarios do not add a separate runtime case. The `cluster-cidr`/`service-cidr` consistency check is also plan time only.

| Scenario | VMs | What it proves |
|---|---|---|
| `default` | 1 | `initial` on embedded etcd: etcd datastore with no `state.db`, module-owned config and node-type marker, no token in the module's files, kubeconfig group (the `ssh_user` reads the node list with it, but not `config.yaml`, and cannot list the `0710` directory), restart survival, no-op reconverge, config-change restart without reinstall, bounded failure with a journal tail and no marker written. Readiness negatives: a world-readable kubeconfig, a stopped unit (bounded by a 30 s timeout) and a foreign CA (fails at once). Preflight refusals (node type change, downgrade, unreadable version, SQLite) are tested in `tests/container`. |
| `firewall` | 1 | The managed nftables gate: SSH stays reachable, the API and etcd ports answer on loopback and from the trusted controller but time out (not refuse) from it once untrusted, foreign nft tables survive managed and off mode, an invalid ruleset is refused before replacing the live table, enabled UFW and UFW chains left loaded are rejected, and off mode is refused while the module unit file or table exists, then passes after the manual cleanup (unit, ruleset and table removed). |
| `cluster` | 3 | `initial`, `server` and `agent` joined through the initial node as head, converged at `-vvv`: three Ready nodes, two control-plane; no bootstrap token left on the head; the join token file `0600` root and unreadable to the SSH user; config directory `0710` on the server and `0700` on the agent, which has no `k3s.yaml`; the agent Ready again after a restart with its token deleted; joined re-runs change nothing and run no token task; no token secret in any controller output. |
| `host` | 1 | Host role and reboot handler: a seeded `/run/reboot-required` on a first install gives one reboot before k3s is installed (boot id changes, request cleared, the idempotence converge reports no change), kernel modules and sysctls effective, persisted and surviving that reboot and a k3s restart, a nonexistent module refused before any change, an installed node never rebooted (an etcd datastore is never pristine, so the reboot gate never depends on the marker), and root filesystem growth without data loss. |
| `recovery` | 3 | An agent join that fails after its bootstrap token was minted (unknown pin) still deletes the token on the head, and the converge retries it as a first install; the module adopts a legacy upstream etcd install (`--cluster-init`) without re-running the installer, keeping its CA, datastore and data. |

## Prerequisites

- libvirt (`qemu-system-x86`, `libvirt-daemon-system`) with `/dev/kvm`
  and the `default` libvirt network active.
- Vagrant 2.4.9 with the `vagrant-libvirt` plugin 0.12.2 and the box
  `cloud-image/ubuntu-26.04` 20260927.0.0 (the scenarios pin this version).
- Your user in the `libvirt` group. A session opened before joining it
  does not carry the group yet, so run every command through
  `/usr/bin/sg libvirt -c '...'` (the full path; bare `sg` may be shadowed).
- `TMPDIR` and `VAGRANT_HOME` on a disk with room: a small tmpfs `/tmp`
  cannot hold the box images, for example
  `TMPDIR=/var/tmp/k3sb-vm-tmp VAGRANT_HOME=$HOME/.vagrant.d`.
- An activated venv with molecule and Ansible installed:
  `uv venv && uv pip install -r tests/requirements.txt`, then
  `source .venv/bin/activate` inside the `sg` command.

## Inner loop

Keep the VMs up and iterate on one scenario (replace `<scenario>`):

```sh
/usr/bin/sg libvirt -c 'cd <repo> && source .venv/bin/activate && \
  TMPDIR=/var/tmp/k3sb-vm-tmp VAGRANT_HOME=$HOME/.vagrant.d \
  molecule converge -s <scenario>'
# then, as often as needed:  molecule verify -s <scenario>
# shell into a VM:           molecule login -s <scenario> -h <instance>
```

`molecule converge` creates the VMs on first use and keeps them;
re-running it re-applies the module. Finish with
`molecule destroy -s <scenario>`.

## Full run

```sh
/usr/bin/sg libvirt -c 'cd <repo> && source .venv/bin/activate && \
  TMPDIR=/var/tmp/k3sb-vm-tmp VAGRANT_HOME=$HOME/.vagrant.d \
  timeout -k 120 2700 molecule test -s <scenario>'
```

The scenario runs the whole sequence and destroys its VMs at the end.
`cluster` and `recovery` take the longest (three VMs, real joins).

## Leftover cleanup

An interrupted run can leave VMs behind. Domains are named
`k3sb-molecule-*`. Prefer `molecule destroy -s <scenario>`; if that
cannot find them, remove only those domains:

```sh
/usr/bin/sg libvirt -c 'virsh -c qemu:///system list --all | grep k3sb-molecule'
/usr/bin/sg libvirt -c 'virsh -c qemu:///system destroy <domain>; \
  virsh -c qemu:///system undefine <domain> --remove-all-storage'
```
