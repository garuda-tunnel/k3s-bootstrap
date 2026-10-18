# AGENTS.md

Rules for humans and automated agents working in this repository.

## Ground rules

- Agents must follow the organization-wide coding rules:
  https://github.com/GFN-CIS/coding-agent-public-rules
  (that repository is the source of truth; its text is intentionally
  not copied here).
- Everything in this repository — code, comments, commits, docs and
  test names — is in English.
- This module is generic: names, charts or profiles of any project that
  consumes it must never appear in code, docs or tests.

- Every Ansible role keeps its documentation next to it: `README.md` (what
  it does and why, invariants, what it reads from other roles) and
  `meta/argument_specs.yml` (its inputs, enforced by Ansible on role entry).
  `ansible/README.md` only describes the plays and links the roles.

## Architecture rule: Ansible owns the host

- Every host change, check and the reboot is an Ansible task in
  `ansible/k3s-node.yml`, which runs on the operator's controller over SSH.
  Nothing is installed on, staged to or run locally on the target.
- Terraform only launches that playbook: one `terraform_data` with one
  `local-exec` (`uv run` against `ansible/requirements.txt`). No other host
  logic in shell or inline commands, no `connection` blocks, no `file()` of
  the SSH key, no token variable, input, output, state value or `extra_vars`
  entry.

## Documented exceptions

- The `k3s` role and the readiness k3s predicate skip themselves when
  `ansible_virtualization_type == 'docker'`. This deliberately breaks the
  org rule against environment forks in code: k3s cannot run inside the
  container test tier, and no supported host is a Docker container. k3s
  and readiness are proven on VMs by Molecule and `tests/vm`. Do not add
  further environment forks.
- Every play except `Inventory` sets `become: true` at play level, against
  the rule to scope `become` per task: every task of those plays changes or
  reads root-owned host state, so per-task `become` would only repeat it.
  The `Inventory` play runs on the controller and never becomes root.
- The readiness role matches `Unauthorized|Forbidden|x509` in kubectl
  stderr: kubectl exits 1 for every error, and these are stable API error
  reasons, not localized prose.

## Review checklist (blocking)

A PR is blocked if it:

- violates coding-agent-public-rules,
- adds consumer-specific code, names or profiles,
- adds host logic to shell instead of Ansible.

## Test commands

Run from the repository root:

```sh
tofu fmt -check -recursive
tofu validate
tofu test            # plan-time interface tests, no providers
.venv/bin/ansible-lint ansible/ molecule/
```

Local Python runs use a `uv` venv from `tests/requirements.txt` (never
pip-install into the system Python); CI does the same:

```sh
uv venv
uv pip install -r tests/requirements.txt
.venv/bin/pytest tests/container     # needs Docker
.venv/bin/black --check tests && .venv/bin/pylint tests && .venv/bin/vulture tests
```

The molecule scenarios (`default`, `firewall`, `cluster`, `host`,
`recovery`) and the VM tier (`tests/vm`, three module contracts) run on
Vagrant over libvirt, manually, never in CI. Run them through
`/usr/bin/sg libvirt` (the full path; bare `sg` may be shadowed) because
the session may not carry the `libvirt` group yet, with the venv
activated so `molecule` and `ansible` resolve. Molecule's verifier is
Ansible (`molecule/<scenario>/verify.yml`), not testinfra:

```sh
/usr/bin/sg libvirt -c 'cd <repo> && source .venv/bin/activate && molecule test -s cluster'
K3SB_VM_TESTS=1 /usr/bin/sg libvirt -c \
  'cd <repo> && source .venv/bin/activate && timeout -k 180 3000 pytest -s tests/vm'
```

Prerequisites and scenario details: `molecule/README.md`,
`tests/README.md`, `tests/vm/README.md`.
