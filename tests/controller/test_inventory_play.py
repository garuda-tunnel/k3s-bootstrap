"""Exercise the ``Inventory`` play of ``ansible/k3s-node.yml`` without any target host.

The play runs on the controller only, so these tests need no Docker and no SSH daemon.
Each test runs a copy of ``ansible/`` so it can change the pins or the playbook.
Run with ``pytest tests/controller``; ``uv`` must be on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
HEAD = {"host": "192.0.2.11", "port": 2200, "user": "headadmin"}
NODE_VARS: dict[str, Any] = {
    "node_type": "initial",
    "host": "192.0.2.10",
    "port": 2222,
    "bootstrap_user": "root",
    "ssh_user": "admin",
    "ssh_private_key_file": "/nonexistent/key",
    "host_key_check": True,
    "connection_timeout": "1s",
    "readiness_timeout": "1m",
    "firewall_mode": "managed",
    "k3s_config": {},
    "authorized_keys": [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFRlc3RLZXlGb3JQcmVmbGlnaHQxMjM0NTY3ODkw x"
    ],
    "sysctls": {},
    "kernel_modules": [],
    "extra_packages": [],
    "k3s_version": "v1.33.0+k3s1",
    "api_trusted_cidrs": [],
    "cluster_cidrs": [],
    "grow_root_filesystem": False,
}
GUARD = "inventory : Assert the head matches the node type"
ROOT_USER = "inventory : Refuse root as the SSH user"
RESET_KEYS = "inventory : Refuse k3s_config keys"
SPEC = "inventory : Validating arguments against arg spec 'main'"
TOOLCHAIN = "inventory : Assert the pinned controller toolchain"
COLLECTIONS = "inventory : Assert the pinned collections resolve"
ADD_NODE = "TASK [inventory : Add the target node]"


@pytest.fixture(name="work")
def fixture_work(tmp_path: Path) -> Path:
    """Return a scratch directory with a copy of ``ansible/`` and the helper playbooks."""
    shutil.copytree(ROOT / "ansible", tmp_path / "ansible")
    for helper in ("dump_inventory.yml", "probe_ssh.yml"):
        shutil.copy(HERE / helper, tmp_path / helper)
    return tmp_path


def run_play(
    work: Path,
    *,
    playbook: str = "dump_inventory.yml",
    extra: dict[str, Any] | None = None,
    omit: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``playbook`` through uv as the module does; ``extra`` overrides NODE_VARS.

    The probe playbook runs at -vvv so the ssh command line is printed.
    """
    variables = {k: v for k, v in (NODE_VARS | (extra or {})).items() if k not in omit}
    command = [
        "uv",
        "run",
        "--no-project",
        "--with-requirements",
        str(ROOT / "ansible" / "requirements.txt"),
        "ansible-playbook",
        "-i",
        "localhost,",
        # ``always`` selects the inventory play (and these helpers): no play touches a host.
        "--tags",
        "always",
        playbook,
        "-e",
        json.dumps(variables),
    ] + (["-vvv"] if playbook == "probe_ssh.yml" else [])
    environment = os.environ | {"ANSIBLE_CONFIG": str(work / "ansible" / "ansible.cfg")}
    environment.update(env or {})
    return subprocess.run(
        command,
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def dumped(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Return the inventory printed by ``dump_inventory.yml``."""
    match = re.search(r'"msg": ("K3SB_DUMP .*")', result.stdout)
    assert match, result.stdout + result.stderr
    return json.loads(json.loads(match.group(1)).removeprefix("K3SB_DUMP "))


def failed_task(result: subprocess.CompletedProcess[str]) -> str:
    """Return the name of the last task that started, which is the one that failed."""
    return re.findall(r"TASK \[(.*?)\]", result.stdout)[-1]


@pytest.mark.parametrize(
    ("node_type", "host_key_check"),
    [("initial", True), ("server", False), ("agent", True)],
    ids=["initial-checked", "server-unchecked", "agent-checked"],
)
def test_valid_node_registers_inventory_and_is_idempotent(
    work: Path, node_type: str, host_key_check: bool
) -> None:
    """Verify the registered node and head hostvars, and that a second registration is a no-op.

    Validates: FR-034 inputs, FR-007 host-key settings, Molecule idempotence (design section 3).
    Code: ansible/roles/inventory/tasks/main.yml (tasks ``Add the target node`` and
        ``Add the cluster head``)

    Assertion: node and head carry the address, port, user, key and host-key settings, the
    registration ran twice, and the recap shows ``changed=0``. The host-key setting does not
    depend on the node type, so each node type runs with one value and both values run.
    A changed ``add_host`` would
    break the native idempotence step of every Molecule scenario. The Inventory play selects
    ``bootstrap_user`` (the ``ssh_user`` probe cannot connect here); the always-run pre-task
    of the Host play then makes ``ssh_user`` the node's user, while the head keeps its own.

    Method:
    1. Arrange: valid variables for the node type, with a head unless ``initial``.
    2. Act: run the playbook, which imports ``k3s-node.yml`` twice, then dumps the hostvars.
    3. Assert: exit 0, ``changed=0``, two registrations, and the exact hostvars.
    """
    extra: dict[str, Any] = {"node_type": node_type, "host_key_check": host_key_check}
    if node_type != "initial":
        extra["head"] = HEAD
    result = run_play(work, extra=extra)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "changed=0" in result.stdout
    assert result.stdout.count(ADD_NODE) == 2
    ssh_args = (
        "-o StrictHostKeyChecking=yes"
        if host_key_check
        else "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    )
    common = {
        "ansible_ssh_private_key_file": "/nonexistent/key",
        "ansible_ssh_common_args": ssh_args,
        "ansible_host_key_checking": host_key_check,
    }
    inventory = dumped(result)
    assert inventory["selected_user"] == "root"
    assert inventory["node"] == common | {
        "ansible_host": "192.0.2.10",
        "ansible_port": 2222,
        "ansible_user": "admin",
        "group_names": ["k3s_node"],
    }
    if node_type == "initial":
        assert inventory["head"] is None
    else:
        assert inventory["head"] == common | {
            "ansible_host": "192.0.2.11",
            "ansible_port": 2200,
            "ansible_user": "headadmin",
            "group_names": ["k3s_head"],
        }


@pytest.mark.parametrize(
    ("host_key_check", "enforced"), [(True, "yes"), (False, "no")], ids=["on", "off"]
)
def test_host_key_check_beats_inherited_environment(
    work: Path, host_key_check: bool, enforced: str
) -> None:
    """Verify ``host_key_check`` decides, even against an inherited env override.

    Validates: FR-007 (a verified host key is never silently skipped).
    Code: ansible/roles/inventory/tasks/main.yml (hostvars ``ansible_host_key_checking`` on
        node and head)

    Assertion: with ``ANSIBLE_HOST_KEY_CHECKING=False`` inherited, the ssh command line built
    for the node carries ``StrictHostKeyChecking=<enforced>`` only. Ansible puts its own
    ``=no`` first when the env disables checking, and ssh honours the first value.

    Method:
    1. Arrange: ``host_key_check`` as parametrized, the env variable set to the opposite of
       what a True check needs.
    2. Act: run a probe that makes Ansible build the ssh command (connection refused).
    3. Assert: only the expected ``StrictHostKeyChecking`` value is in the ssh command line.
    """
    result = run_play(
        work,
        playbook="probe_ssh.yml",
        extra={"host": "127.0.0.1", "port": 1, "host_key_check": host_key_check},
        env={"ANSIBLE_HOST_KEY_CHECKING": "False"},
    )

    assert result.returncode != 0
    settings = re.findall(r"StrictHostKeyChecking=(\w+)", result.stdout)
    assert settings
    assert set(settings) == {enforced}


@pytest.mark.parametrize(
    "requirements",
    [
        "ansible==14.5.0\nansible-core==0.0.0\n",
        "ansible==0.0.0\nansible-core==2.21.5\n",
        "ansible==14.5.0\n",
        "ansible-core==2.21.5\n",
    ],
    ids=["core-differs", "package-differs", "core-missing", "package-missing"],
)
def test_pin_differing_from_requirements_fails_before_any_host_task(
    work: Path, requirements: str
) -> None:
    """Verify the running toolchain is compared with ``requirements.txt`` and fails closed.

    Validates: CON-004, CON-017 (pins checked before any change; one pin source).
    Code: ansible/roles/inventory/tasks/main.yml (task ``Assert the pinned controller toolchain``)

    Assertion: the toolchain task fails, and no node is registered. A missing or empty
    expectation must not turn validation off.

    Method:
    1. Arrange: a copy of the playbook whose ``requirements.txt`` disagrees with the install.
    2. Act: run the playbook (the real pins still install the toolchain).
    3. Assert: non-zero exit, the toolchain task failed, no ``add_host`` ran.
    """
    (work / "ansible" / "requirements.txt").write_text(requirements)
    result = run_play(work)

    assert result.returncode != 0
    assert failed_task(result) == TOOLCHAIN
    assert ADD_NODE not in result.stdout


def test_unpinned_requirements_line_fails(work: Path) -> None:
    """Verify a requirements line without an exact ``==`` pin stops the run.

    Validates: CON-004 (exact pins only).
    Code: ansible/roles/inventory/tasks/main.yml (task ``Read the pins of requirements.txt``)

    Assertion: non-zero exit and no node registered.

    Method:
    1. Arrange: ``ansible>=14`` in the copy's requirements.
    2. Act: run the playbook.
    3. Assert: non-zero exit, no ``add_host`` ran.
    """
    (work / "ansible" / "requirements.txt").write_text("ansible>=14\nansible-core==2.21.5\n")
    result = run_play(work)

    assert result.returncode != 0
    assert ADD_NODE not in result.stdout


def test_unresolvable_collection_fails_before_any_host_task(work: Path) -> None:
    """Verify a pinned collection that cannot be resolved fails the play before host work.

    Validates: CON-004 (pinned collections available before any change).
    Code: ansible/roles/inventory/tasks/main.yml (task ``Assert the pinned collections resolve``)

    Assertion: with a required module renamed to one that does not exist, the resolve task
    fails and no node is registered. The unmodified playbook passes in the tests above, so
    the check is positive in both directions.

    Method:
    1. Arrange: a copy of the playbook requiring ``ansible.posix.nosuchmodule``.
    2. Act: run the playbook.
    3. Assert: non-zero exit, the resolve task failed, no ``add_host`` ran.
    """
    pins = work / "ansible" / "roles" / "inventory" / "vars" / "main.yml"
    pins.write_text(pins.read_text().replace("ansible.posix.sysctl", "ansible.posix.nosuch"))
    result = run_play(work)

    assert result.returncode != 0
    assert failed_task(result) == COLLECTIONS
    assert "ansible.posix.nosuch" in result.stdout
    assert ADD_NODE not in result.stdout


@pytest.mark.parametrize(
    ("extra", "failing"),
    [
        ({"node_type": "server"}, GUARD),
        ({"node_type": "agent"}, GUARD),
        ({"node_type": "server", "head": None}, GUARD),
        ({"node_type": "initial", "head": HEAD}, GUARD),
        ({"node_type": "agent", "head": {"host": "192.0.2.11"}}, SPEC),
        ({"node_type": "unknown"}, SPEC),
        ({"ssh_user": "root"}, ROOT_USER),
        ({"k3s_config": {"cluster-reset": True}}, RESET_KEYS),
        ({"k3s_config": {"cluster-reset-restore-path": "/x"}}, RESET_KEYS),
    ],
    ids=[
        "server-no-head",
        "agent-no-head",
        "server-null-head",
        "initial-with-head",
        "head-without-user",
        "unknown-node-type",
        "root-ssh-user",
        "cluster-reset",
        "cluster-reset-restore-path",
    ],
)
def test_invalid_inputs_fail_before_registration(
    work: Path, extra: dict[str, Any], failing: str
) -> None:
    """Verify an invalid node type, ``head``, root ``ssh_user`` or reset key fails first.

    Validates: CON-005, CON-012 (``head`` only for server and agent; three node types),
        FR-003 (root ssh_user and the data-wiping ``k3s_config`` keys are refused). Every
        node type outside the ``choices`` list takes the same check as ``unknown``.
    Code: ansible/roles/inventory/meta/argument_specs.yml (types, choices, required head
        keys); ansible/roles/inventory/tasks/main.yml (``Assert the head matches the node
        type``, ``Refuse root as the SSH user``, ``Refuse k3s_config keys``)

    Assertion: non-zero exit, the expected check (the argument spec or an input assert) is
    the failure, and no node is registered. The failure happens on the controller, before any
    host is reached.

    Method:
    1. Arrange: one invalid input combination.
    2. Act: run the playbook.
    3. Assert: non-zero exit, the failing task, no node registered.
    """
    result = run_play(work, extra=extra)

    assert result.returncode != 0
    assert failed_task(result).startswith(failing)
    assert ADD_NODE not in result.stdout


def test_missing_node_type_fails_in_the_argument_spec(work: Path) -> None:
    """Verify an absent ``node_type`` fails the argument spec instead of defaulting.

    Validates: FR-003 (invalid inputs fail before any change).
    Code: ansible/roles/inventory/meta/argument_specs.yml (``node_type`` required)

    Assertion: non-zero exit, the argument-spec check is the failure, no node is registered.

    Method:
    1. Arrange: variables with the ``node_type`` key omitted.
    2. Act: run the playbook.
    3. Assert: non-zero exit, failed task is the argument-spec check, no node registered.
    """
    result = run_play(work, omit=("node_type",))

    assert result.returncode != 0
    assert failed_task(result).startswith(SPEC)
    assert ADD_NODE not in result.stdout


ROLES = [
    "inventory",
    "preflight",
    "access",
    "ssh_hardening",
    "host",
    "firewall",
    "k3s",
    "readiness",
]


def test_every_role_documents_its_inputs() -> None:
    """Verify each role's argument spec renders through ``ansible-doc``.

    Validates: the role documentation contract (``meta/argument_specs.yml`` next to the role,
    linked from its README) that Ansible also enforces on role entry.
    Code: ansible/roles/<role>/meta/argument_specs.yml; ansible/roles/<role>/README.md

    Assertion: one ``ansible-doc -t role -j`` run for all roles exits 0 and returns each
    role's ``main`` entry point with a short description, and the role README links the
    spec. A broken or missing spec would also disable the entry validation silently.

    Method:
    1. Arrange: the role names.
    2. Act: run ``ansible-doc`` once through the pinned toolchain.
    3. Assert: exit code, entry point and README reference of every role.
    """
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--with-requirements",
            str(ROOT / "ansible" / "requirements.txt"),
            "ansible-doc",
            "-t",
            "role",
            "-j",
            "-r",
            str(ROOT / "ansible" / "roles"),
            *ROLES,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=os.environ | {"ANSIBLE_CONFIG": str(ROOT / "ansible" / "ansible.cfg")},
    )

    assert result.returncode == 0, result.stderr
    documented = json.loads(result.stdout)
    assert sorted(documented) == sorted(ROLES)
    for role in ROLES:
        assert documented[role]["entry_points"]["main"]["short_description"], role
        readme = (ROOT / "ansible" / "roles" / role / "README.md").read_text()
        assert "meta/argument_specs.yml" in readme, role
