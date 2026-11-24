# AGENTS.md

Rules for humans and automated agents working in this repository.

## Ground rules

- Agents must follow the organization-wide coding rules:
  https://github.com/GFN-CIS/coding-agent-public-rules
  (that repository is the source of truth; its text is intentionally
  not copied here).
- Everything in this repository - code, comments, commits, docs and
  test names - is in English.
- This module is generic: names, charts or profiles of any project that
  consumes it must never appear in code, docs or tests.

- Every Ansible role keeps its documentation next to it: `README.md` (what
  it does and why, invariants, what it reads from other roles) and
  `meta/argument_specs.yml` (its inputs, enforced by Ansible on role entry).
  `ansible/README.md` only describes the plays and links the roles.

## Architecture rule: Ansible owns the host

- Every host change, check and the reboot is an Ansible task in
  `ansible/k3s-node.yml`, which runs on the operator's controller over SSH.
  Ansible itself stays on the controller; its modules execute on the target
  over SSH and manage target packages and files.
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

## Verification

Use the commands and prerequisites in [tests/README.md](tests/README.md)
and [molecule/README.md](molecule/README.md). The VM tiers are manual and
require dedicated disposable hosts; documentation-only changes do not need
a live infrastructure apply.
