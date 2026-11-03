# Tests

Five tiers; lower ones are faster and more deterministic. Python
tiers share one `uv` venv:

```sh
uv venv
uv pip install -r tests/requirements.txt
```

| Tier | Scope | Prerequisites | Command (repository root) |
|---|---|---|---|
| interface | plan-time inputs and outputs | OpenTofu | `tofu test` |
| controller | the `inventory` role, role documentation, launch parity, run hash; no host | venv | `.venv/bin/pytest tests/controller` |
| container | preflight, access, SSH hardening, host refusals and a module apply (no k3s) | Docker, OpenTofu, host kernel modules, venv | `.venv/bin/pytest -v tests/container` |
| molecule | role behaviour on real VMs | libvirt, Vagrant, venv | see `molecule/README.md` |
| VM | module contracts on Vagrant VMs | libvirt, Vagrant, venv | see below |

## Interface tier (`interface.tftest.hcl`, `phases.tftest.hcl`)

`tofu test` from the repository root. Plan-only, no providers: variable
validation (node type and `head`, k3s version pin, reserved `k3s_config`
keys, firewall mode, CIDR lists, timeouts, Jinja delimiters), outputs, no
sensitive input, and the re-run trigger. Runs in CI.

## Controller tier (`controller/`)

pytest with no target host: the `inventory` role (pins, argument spec and input guards,
registered hostvars, host-key settings), launch parity between `main.tf`
and both harnesses, the Molecule launch judge, and `run_hash`. Runs in CI:
`.venv/bin/pytest tests/controller`.

## Container tier (`container/`)

pytest is the controller: it runs `ansible/k3s-node.yml` with the module's
command against systemd containers of every supported base image
(`K3SB_CONTAINER_IMAGE`, default `ubuntu:26.04`; the CI matrix also runs
`debian:13`) reached over SSH
(`test_preflight`, `test_access`, `test_ssh_hardening`, `test_host`), and a real
`tofu apply`/re-apply of the module (`test_module_apply`). The inputs the
controller-side `Inventory` play refuses are tested in the controller tier, not
here. A second container plays the head for the join
preflight cases. k3s cannot run in a container, so the `k3s` and
`readiness` roles skip Docker hosts; they are proven by the molecule and VM
tiers.

Needs privileged Docker, OpenTofu, and `/lib/modules` matching the Docker
host's running kernel with `br_netfilter` and `overlay` available. The
fixture loads those modules with a disposable privileged helper, then sets
the bridge sysctls in the test container's own network namespace and mounts
`/lib/modules` read-only so the role's real modprobe checks can run. Module
loading persists on the Docker host; helper and test containers are removed.
Runs in CI:

```sh
.venv/bin/pytest -v tests/container
```

### Lint

The only Python in this repository is the test suite, so the tooling
targets `tests/` (configuration in `pyproject.toml`). CI runs
`black --check tests`, `pylint tests` and `vulture tests`. Names that
pytest consumes by convention live in `vulture_whitelist.py`. There is no
coverage or mutation tooling: it would measure the test code itself, because
the playbook runs in subprocesses.

### Fixtures (`container/conftest.py`)

The session fixtures `systemd_host` (the node) and `head_host` (the head)
boot the image from `container/Dockerfile` (systemd as PID 1,
`openssh-server`, `sudo`, `python3`, `kmod`, `nftables`), publish SSH on a
random `127.0.0.1` port and authorize one ephemeral controller key for
root; nothing of the repository is copied in. `run_playbook()` launches the
playbook as the module does; `playbook_vars()` builds the inputs a test overrides on top of the valid node inputs
and `ssh_as()` probes a login. Keep these attribute names stable:

- `systemd_host.host` — testinfra host (docker backend)
- `systemd_host.container_id` — docker container id
- `systemd_host.ssh_port` — published SSH port on 127.0.0.1
- `systemd_host.key_path` — ephemeral ed25519 key authorized for root

## Molecule tier (`../molecule/`)

Role behaviour on real Vagrant/libvirt VMs, five scenarios (`default`,
`firewall`, `cluster`, `host`, `recovery`). Manual, never in CI. See
`molecule/README.md` for what each proves, prerequisites and commands.

## VM tier (`vm/`)

pytest against real provisioned VMs (Ubuntu 26.04 or Debian 13, see
`tests/vm/README.md`); module-level contracts
that each drive `tofu apply`/`destroy` (a bootstrapped node and its targeted
destroy, the parallel cluster composition, an interrupted agent join). Manual
only, never in CI. Prerequisites and the command are in `tests/vm/README.md`:

```sh
K3SB_VM_TESTS=1 /usr/bin/sg libvirt -c \
  'cd <repo> && source .venv/bin/activate && timeout -k 180 3000 pytest -s tests/vm'
```
