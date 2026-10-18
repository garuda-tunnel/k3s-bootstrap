"""Check the harness launchers use the module's own launch (main.tf local-exec).

The module runs one command with two environment variables; the Molecule task and the
container ``run_playbook`` must use the same command and the same variable transport.
Run with ``pytest tests/controller``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT = ("K3SB_VARS", "ANSIBLE_CONFIG")


def module_command() -> str:
    """Return the shell command of the module's ``local-exec`` with HCL escapes removed."""
    match = re.search(r'command\s*=\s*"(.*)"', (ROOT / "main.tf").read_text())
    assert match, "main.tf has no local-exec command"
    return match.group(1).replace('\\"', '"')


def test_molecule_launch_uses_the_module_command_and_environment() -> None:
    """Verify Molecule's ``apply.yml`` launches exactly the module's command.

    Validates: launch parity between the module and the Molecule harness.
    Code: main.tf (local-exec); molecule/shared/tasks/apply.yml

    Assertion: the command string and every environment variable of the module appear.

    Method:
    1. Arrange: read the command from ``main.tf`` and ``apply.yml``.
    2. Act: search the command and the variable names.
    3. Assert: all are present.
    """
    apply = (ROOT / "molecule" / "shared" / "tasks" / "apply.yml").read_text()

    assert module_command() in apply
    assert all(name in apply for name in ENVIRONMENT)


def test_container_launch_uses_the_module_command_and_environment() -> None:
    """Verify the container tier's ``run_playbook`` launches exactly the module's command.

    Validates: launch parity between the module and the container harness.
    Code: main.tf (local-exec); tests/container/conftest.py

    Assertion: ``LAUNCH_COMMAND`` equals the module's command with the playbook path as a
    field, and every environment variable of the module appears.

    Method:
    1. Arrange: read the command from ``main.tf`` and parse ``conftest.py``.
    2. Act: evaluate the ``LAUNCH_COMMAND`` literal (adjacent literals join in the AST).
    3. Assert: exact equality and all variable names present.
    """
    conftest = (ROOT / "tests" / "container" / "conftest.py").read_text()
    template = module_command().replace("ansible/k3s-node.yml", "{playbook}")
    launch = next(
        ast.literal_eval(node.value)
        for node in ast.parse(conftest).body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "LAUNCH_COMMAND"
    )

    assert "{playbook}" in template
    assert launch == template
    assert all(name in conftest for name in ENVIRONMENT)
