"""Prove the change sensitivity of the module's re-run trigger ``run_hash``.

``tofu test`` cannot compare two plans, so each case copies the module to a scratch
directory and runs ``tofu plan`` there. Needs ``tofu`` on PATH, no providers beyond the
built-in one and no network. Run with ``pytest tests/controller/test_run_hash.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VARS = {
    "node_type": "initial",
    "host": "192.0.2.10",
    "host_generation": "instance-1",
    "ssh_user": "admin",
    "ssh_private_key_file": "/secure/key",
    "authorized_keys": ["ssh-ed25519 AAAA admin@example"],
    "k3s_version": "v1.33.0+k3s1",
    "cluster_cidrs": ["10.42.0.0/16", "10.43.0.0/16"],
}


@pytest.fixture(name="module")
def fixture_module(tmp_path: Path) -> Path:
    """Return a scratch copy of the module (``*.tf`` and ``ansible/``) with its tfvars."""
    for source in ROOT.glob("*.tf"):
        shutil.copy(source, tmp_path / source.name)
    shutil.copytree(ROOT / "ansible", tmp_path / "ansible")
    write_vars(tmp_path, {})
    tofu(tmp_path, "init", "-backend=false")
    return tmp_path


def tofu(module: Path, *args: str) -> str:
    """Run ``tofu`` in ``module`` and return stdout; fail loudly on a non-zero exit."""
    result = subprocess.run(
        ["tofu", *args, "-no-color"],
        cwd=module,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def write_vars(module: Path, override: dict[str, str]) -> None:
    """Write ``terraform.tfvars.json`` with the base variables and ``override``."""
    (module / "terraform.tfvars.json").write_text(json.dumps(VARS | override))


def run_hash(module: Path) -> str:
    """Plan the scratch module and return the only value of ``triggers_replace``."""
    tofu(module, "plan", "-out=plan.bin")
    plan = json.loads(tofu(module, "show", "-json", "plan.bin"))
    changes = [c for c in plan["resource_changes"] if c["address"] == "terraform_data.converge"]
    (trigger,) = changes[0]["change"]["after"]["triggers_replace"]
    return trigger


def test_identical_rerun_and_edit_outside_ansible_keep_the_hash(module: Path) -> None:
    """Verify an identical plan and an edit outside ``ansible/`` leave ``run_hash`` alone.

    Validates: FR-017 (a reconciled host is not re-run).
    Code: locals.tf (``run_hash``, ``ansible_tree_hash``)

    Assertion: the hash is stable across plans and ignores files outside ``ansible/``, so
    a docs or test edit never forces a playbook run.

    Method:
    1. Arrange: scratch module copy; plan once.
    2. Act: plan again; then add and edit files outside ``ansible/``; plan again.
    3. Assert: all three hashes are equal.
    """
    first = run_hash(module)
    assert run_hash(module) == first
    (module / "README.md").write_text("docs\n")
    (module / "molecule").mkdir()
    (module / "molecule" / "scenario.yml").write_text("x: 1\n")
    assert run_hash(module) == first


@pytest.mark.parametrize("change", ["edit-role", "add-file"])
def test_ansible_tree_change_alters_the_hash(module: Path, change: str) -> None:
    """Verify an edited or added file under ``ansible/`` changes ``run_hash``.

    Validates: FR-017 (a changed playbook is applied).
    Code: locals.tf (``ansible_tree_hash``)

    Assertion: the hash differs after the change.

    Method:
    1. Arrange: scratch module copy; plan once.
    2. Act: edit a role file (the playbook is one more file of the same tree hash), or add
       a new file under ``ansible/``.
    3. Assert: the new hash differs from the first.
    """
    first = run_hash(module)
    if change == "edit-role":
        role_file = next((module / "ansible" / "roles").glob("*/tasks/main.yml"))
        role_file.write_text(role_file.read_text() + "\n# edited\n")
    else:
        (module / "ansible" / "roles" / "added.yml").write_text("---\n")

    assert run_hash(module) != first


@pytest.mark.parametrize(
    "override",
    [{"host_generation": "instance-2"}, {"k3s_version": "v1.33.1+k3s1"}],
    ids=["generation", "input"],
)
def test_generation_and_input_change_alter_the_hash(module: Path, override: dict[str, str]) -> None:
    """Verify a new ``host_generation`` or a changed input changes ``run_hash``.

    Validates: FR-017 (a recreated VM or new settings are applied).
    Code: locals.tf (``run_hash``)

    Assertion: the hash differs after the change.

    Method:
    1. Arrange: scratch module copy; plan once.
    2. Act: rewrite the tfvars with the override; plan again.
    3. Assert: the new hash differs from the first.
    """
    first = run_hash(module)
    write_vars(module, override)

    assert run_hash(module) != first
