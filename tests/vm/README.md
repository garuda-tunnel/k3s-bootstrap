# VM test tier (Vagrant/libvirt)

This manual tier tests the OpenTofu module itself: each test drives real
`tofu apply` runs against Ubuntu 26.04 VMs that Vagrant manages
(`tests/vm/vagrant/Vagrantfile`, vagrant-libvirt provider, one directory
per stand). Role behaviour lives in the Molecule scenarios; this tier is
opt-in, serial, and never runs in CI.

## The module contracts

- `test_k3s_initial.py::test_destroying_bootstrap_resource_keeps_k3s`
  — one `tofu apply` of the module (`initial`) on a fresh VM ends in a
  Ready node with an active k3s; then `tofu destroy
  -target=module.k3s_bootstrap` leaves k3s active and the datastore
  intact.
- `test_cluster.py::test_agents_join_the_hub_in_parallel` — the caller
  composition in `fixture/cluster/`: an `initial` hub and two agents with
  `head = module.hub.head`, applied in parallel. Three Ready nodes and no
  bootstrap token left on the hub.
- `test_k3s_recovery.py::test_rerun_after_agent_join_killed_once_its_token_was_minted`
  — the agent apply is killed on the task that writes the minted token
  (the controller-death case); an unchanged retry joins, and every token
  left on the head expires within `readiness_timeout` + 5 minutes.

The first-install reboot and the day-2 no-reboot are proven by the Molecule
`host` scenario, token secrecy by the Molecule `cluster` scenario (outputs)
and `phases.tftest.hcl` (state).

## Prerequisites

- Packages: `qemu-system-x86`, `libvirt-daemon-system`; Vagrant with the
  `vagrant-libvirt` plugin and the box
  `cloud-image/ubuntu-26.04` (`20260927.0.0`) imported.
- Your user in the `libvirt` group (`sudo usermod -aG libvirt $USER`).
  A session opened before joining the group does not carry it yet: run
  pytest and molecule through `/usr/bin/sg libvirt -c '<cmd>'` (the full
  path; bare `sg` may be shadowed by other tools).
- The `vagrant-libvirt` network active, `/dev/kvm` present. Each stand
  is a 2 vCPU / 2 GB VM; the recovery test holds two at once, the
  cluster test three.

`api_trusted_cidrs` is the management network itself, read live from the
`vagrant-libvirt` network XML (by default `192.168.121.0/24`): the runner
and every joining stand must reach the head's API through the managed
firewall. Host keys are not checked (`host_key_check = false`): every
stand is a new VM.

## Running

```sh
K3SB_VM_TESTS=1 /usr/bin/sg libvirt -c \
  'cd <repo> && source .venv/bin/activate && timeout -k 180 3000 pytest -s tests/vm'
```

Live progress is in the per-stand logs even without `-s`:
`tail -f /tmp/k3sb-vm-logs/*/0*.log`. The kill delay (`-k 180`) is the
teardown grace after an outer SIGTERM. Every child runs in its own
session and is reaped as a whole process group (SIGTERM, 15 s, SIGKILL,
10 s). Budget: reaping the in-flight group (25 s; also when the
interrupt lands inside a `vagrant destroy`, which is retried once) plus,
per stand, `vagrant destroy -f` bounded to 45 s (+10 s kill-after) — the
bookkeeping `tofu destroy` is skipped on interruption — so the
two-stand recovery test needs 25 + 2 x 55 = 135 s, inside the 180 s
grace. Set `TMPDIR=/var/tmp/k3sb-vm-tmp` and
`VAGRANT_HOME=$HOME/.vagrant.d` when `/tmp` is a small tmpfs.

A single file or a single test (`pytest tests/vm/test_k3s_initial.py`) is
for debugging only — the full tier is the unit of work.

## Staged lifecycle and live logs

Every stand comes up in bounded stages — `provision` (`vagrant up`:
import, boot, pubkey provisioning, DHCP lease, ≤10 min) and `bootstrap`
(module apply, ≤10 min) — and each stage tees its output live to
`/tmp/k3sb-vm-logs/<name_suffix>/<NN>-<stage>.log`. The log directory is
printed to stderr when the stand starts, so a human can `tail -f` it
during the run. Stage durations land in `timings.txt` in the same
directory. A failed stage fails the test immediately with the last 40
lines of its log.

The fixture generates one SSH keypair per stand (joining stands share
their head's, since a module has one `ssh_private_key_file`); the
Vagrantfile's shell provisioner authorizes it for the box's `vagrant`
sudo user, and the module bootstraps through that user
(`bootstrap_user`), then converges as its own `ssh_user`. The k3s version pin comes from
`tests/k3s-version.txt` only.

## Leftovers

Teardown runs `tofu destroy` (skipped after an interrupt) and then
always `vagrant destroy -f`, even when the tofu destroy fails or hangs,
including after test failures and after an outer SIGTERM (a handler
turns it into an exception so the teardown still runs). Teardown failure
prints the leftover domain. Stand directories are named
`k3sb-stand-*` (under `$TMPDIR/pytest-of-$USER/`), so the libvirt
domains are `k3sb-stand-*_default`.

If an interrupted run leaves something behind, inspect and delete only
those resources. The Vagrantfile needs no environment for cleanup:

```sh
/usr/bin/sg libvirt -c 'virsh -c qemu:///system list --all | grep k3sb-'
# destroy through the stand's Vagrantfile directory if it still exists:
cd <stand-dir> && /usr/bin/sg libvirt -c 'vagrant destroy -f'
# stand directory gone: remove the named domain directly
/usr/bin/sg libvirt -c 'virsh -c qemu:///system destroy <domain>; virsh -c qemu:///system undefine <domain> --remove-all-storage'
```
