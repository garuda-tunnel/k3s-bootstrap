"""Exercise the module end to end through real OpenTofu applies against a container.

One ``initial`` node (tests/container/fixture/main.tf) is applied and re-applied; the
k3s and readiness roles skip Docker hosts, so this covers the launch, access, SSH
hardening and host phases. Run with ``pytest -v tests/container/test_module_apply.py``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

# Pytest puts the test directory on sys.path; pylint does not know about it.
from conftest import SystemdHost, run_docker, ssh_as, start_host  # pylint: disable=import-error

pytestmark = pytest.mark.container

FIXTURE_DIR = Path(__file__).parent / "fixture"


@dataclass(frozen=True)
class ModuleApply:
    """Keep the successful apply's state, inputs and output available until destroy."""

    state: Path
    variables: Path
    env: dict[str, str]
    output: str


def tofu(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run OpenTofu in the fixture without raising so tests can assert its result."""
    return subprocess.run(
        ["tofu", *args, "-no-color"],
        cwd=FIXTURE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def apply(state: Path, variables: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Apply the fixture with ``variables`` into ``state``; return the completed process."""
    return tofu("apply", "-auto-approve", f"-state={state}", f"-var-file={variables}", env=env)


@pytest.fixture(scope="module", name="apply_host")
def fixture_apply_host(controller_key: Path) -> Iterator[SystemdHost]:
    """Provide a container of its own: the apply hardens SSH, which would lock root out of
    the session's shared node for every later test."""
    node = start_host(controller_key)
    try:
        yield node
    finally:
        run_docker("rm", "-f", node.container_id, check=False)


@pytest.fixture(scope="module")
def module_apply(
    apply_host: SystemdHost, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ModuleApply]:
    """Require a successful real module apply, then destroy even if an assertion fails."""
    directory = tmp_path_factory.mktemp("module-apply")
    state = directory / "terraform.tfstate"
    variables = directory / "terraform.tfvars.json"
    variables.write_text(
        json.dumps(
            {
                "port": apply_host.ssh_port,
                "host_generation": apply_host.container_id,
                "ssh_private_key_file": str(apply_host.key_path),
                "authorized_keys": [apply_host.key_path.with_suffix(".pub").read_text().strip()],
            }
        )
    )
    env = os.environ | {
        "TF_DATA_DIR": str(directory / "tofu-data"),
        "TF_INPUT": "0",
        "SSH_AUTH_SOCK": "",
    }
    init = tofu("init", "-backend=false", env=env)
    assert init.returncode == 0, init.stdout + init.stderr
    try:
        first = apply(state, variables, env)
        assert first.returncode == 0, first.stdout + first.stderr
        yield ModuleApply(state, variables, env, first.stdout + first.stderr)
    finally:
        destroy = tofu(
            "destroy", "-auto-approve", f"-state={state}", f"-var-file={variables}", env=env
        )
        assert destroy.returncode == 0, destroy.stdout + destroy.stderr


def test_apply_keeps_key_content_out_of_state_and_output(
    module_apply: ModuleApply, apply_host: SystemdHost
) -> None:
    """Verify the private key's content reaches neither the state nor the apply output.

    Validates: NFR-004 (the module passes a key path, never key material).
    Code: locals.tf (run_vars); main.tf (K3SB_VARS)

    Assertion: no line of the private key's base64 body occurs in the state or output.
    """
    body = [
        line
        for line in apply_host.key_path.read_text().splitlines()
        if line and not line.startswith("-----")
    ]
    state = module_apply.state.read_text()

    assert body
    assert not any(line in state or line in module_apply.output for line in body)


def test_forced_reapply_changes_nothing_on_the_node(
    module_apply: ModuleApply, apply_host: SystemdHost
) -> None:
    """Verify a re-run of the playbook on a converged node reports no change.

    Validates: idempotence of the whole playbook through the real launch.
    Code: main.tf (terraform_data.converge, triggers_replace); ansible/k3s-node.yml

    Assertion: a new ``host_generation`` re-runs the playbook, which exits 0 with
    ``changed=0`` for the node. A changing re-run would mean drift on every apply. The first
    apply disabled root login (checked first), so the re-run also proves the Inventory play
    switches to ``ssh_user`` (``Probe the permanent SSH user``).

    Method:
    1. Arrange: change only ``host_generation`` in the inputs.
    2. Act: apply again.
    3. Assert: exit code and the node's recap row.
    """
    assert ssh_as(apply_host, "root", "true").returncode != 0
    payload = json.loads(module_apply.variables.read_text())
    payload["host_generation"] += "-again"
    module_apply.variables.write_text(json.dumps(payload))

    again = apply(module_apply.state, module_apply.variables, module_apply.env)

    output = again.stdout + again.stderr
    assert again.returncode == 0, output
    assert re.search(r"node\s+: ok=\d+\s+changed=0\s", output), output
