"""Prove Molecule's child launch is judged by the child's PLAY RECAP, not by its display.

``molecule/shared/tasks/launch.yml`` runs the module playbook as a child process, so
Molecule's idempotence step (a ``changed=N`` search in its own output) only sees a change
when the launch task reports one. These tests run that task against a fake child under
display settings that hide task lines. Run with ``pytest tests/controller``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
LAUNCH = ROOT / "molecule" / "shared" / "tasks" / "launch.yml"
BIN = Path(sys.executable).parent


@pytest.fixture(name="child")
def fixture_child(tmp_path: Path) -> Path:
    """Write a one-task child playbook whose change is chosen with ``-e child_changes=``."""
    child = tmp_path / "child.yml"
    child.write_text(
        "- hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n"
        '    - ansible.builtin.command: "true"\n      changed_when: child_changes | bool\n'
    )
    return child


def launch(child: Path, command_env: dict[str, str], changes: bool, exit_code: int = 0):
    """Run the probe around a child; return the probe's process and its own changed count."""
    command = (
        f"{BIN}/ansible-playbook -i localhost, {child} -e child_changes={str(changes).lower()}"
    )
    command += f" && exit {exit_code}" if exit_code else ""
    probe = subprocess.run(
        [
            str(BIN / "ansible-playbook"),
            "-i",
            "localhost,",
            str(HERE / "launch_probe.yml"),
            "-e",
            json.dumps(
                {
                    "probe_launch_tasks": str(LAUNCH),
                    "probe_command": command,
                    "probe_env": command_env,
                    "probe_dir": str(child.parent),
                }
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=child.parent,
    )
    rows = re.findall(r"^localhost\s+:\s+ok=\d+\s+changed=(\d+)", probe.stdout, re.MULTILINE)
    return probe, int(rows[-1]) if rows else None


@pytest.mark.parametrize(
    ("changes", "expected"), [(True, 1), (False, 0)], ids=["changed", "unchanged"]
)
def test_child_changes_propagate_to_the_launch_task(
    child: Path, changes: bool, expected: int
) -> None:
    """Verify the launch task is changed exactly when the child's recap has changes.

    Validates: Molecule idempotence must not pass vacuously.
    Code: molecule/shared/tasks/launch.yml (``changed_when`` from the recap)

    Assertion: the probe's own recap shows ``changed=1`` for a changing child and
    ``changed=0`` for a quiet one, with task-line display suppressed (only the recap is left,
    so the display-on case reads the same recap).

    Method:
    1. Arrange: a child playbook that changes or not, with task-line display suppressed.
    2. Act: run the launch task around it and read the probe's own recap.
    3. Assert: exit 0 and the probe's changed count.
    """
    probe, changed = launch(
        child,
        {"ANSIBLE_DISPLAY_OK_HOSTS": "False", "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "False"},
        changes,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert changed == expected


def test_run_without_recap_fails(child: Path) -> None:
    """Verify rc 0 with no PLAY RECAP row is an error, not "unchanged".

    Validates: Molecule idempotence must not pass vacuously.
    Code: molecule/shared/tasks/launch.yml (``failed_when`` on a missing recap)

    Assertion: a child run under the ``minimal`` callback (no recap) fails the launch task.

    Method:
    1. Arrange: a changing child whose callback prints no recap.
    2. Act: run the launch task around it.
    3. Assert: non-zero exit.
    """
    probe, _ = launch(child, {"ANSIBLE_STDOUT_CALLBACK": "minimal"}, True)

    assert probe.returncode != 0
    assert "FAILED" in probe.stdout


def test_failing_child_fails(child: Path) -> None:
    """Verify a child that exits non-zero fails the launch task.

    Validates: failures are never hidden by the recap judgement.
    Code: molecule/shared/tasks/launch.yml (``failed_when`` on rc)

    Assertion: the probe exits non-zero when the child does.

    Method:
    1. Arrange: a quiet child followed by ``exit 3``.
    2. Act: run the launch task around it.
    3. Assert: non-zero exit.
    """
    probe, _ = launch(child, {}, False, exit_code=3)

    assert probe.returncode != 0
