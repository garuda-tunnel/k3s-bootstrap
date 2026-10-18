"""Recovery VM contract: an agent join killed after its token was minted.

The failed-join (the role's `always` deletes the token) and adoption contracts live in
the Molecule `recovery` scenario; this file keeps the case only a real `tofu apply` can
produce: the controller dies, so nothing deletes the minted token and its TTL is the
only backstop. The file holds the initial node and the agent at the same time. Run with
``K3SB_VM_TESTS=1 pytest tests/vm`` (see tests/vm/README.md for the libvirt wrapper and
timeout).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

import pytest

# Pytest puts the test directory on sys.path; pylint does not know about it.
from conftest import (  # pylint: disable=import-error
    CONVERGE_ADDR,
    assert_converge_interrupted,
    probe,
)

pytestmark = pytest.mark.vm

# The agent's readiness_timeout; the module mints its token for this plus 5 minutes.
READINESS = "2m"
TOKEN_TTL_SECONDS = 2 * 60 + 300


def bootstrap_token_expiries(stand: Any) -> list[datetime]:
    """Return the expiry of every bootstrap token left on ``stand`` (no secret is read)."""
    listed = json.loads(
        probe(
            stand,
            "sudo -n k3s kubectl get secrets -n kube-system"
            " --field-selector type=bootstrap.kubernetes.io/token -o json",
        )
    )["items"]
    return [
        datetime.fromisoformat(base64.b64decode(item["data"]["expiration"]).decode())
        for item in listed
    ]


def test_rerun_after_agent_join_killed_once_its_token_was_minted(vm_factory: Any) -> None:
    """Verify a re-apply joins an agent whose first apply died after the token was minted.

    Validates: an interrupted agent join recovers with unchanged inputs, and the token
    the dead run left behind expires within the module's TTL.
    Code: main.tf (terraform_data.converge); ansible/roles/k3s/tasks/join_agent.yml

    Assertion: after the kill the converge resource is tainted or absent; the retry exits
    0, logs "Creation complete" for the converge, the cluster reports two Ready nodes, and
    every bootstrap token still on the head expires within readiness_timeout + 5 m. A no-op
    retry would pass vacuously, so it is excluded.

    Method:
    1. Arrange: an initial stand (firewall off) and an agent stand sharing its key.
    2. Act: apply the agent and kill tofu on the token-write task, then apply again.
    3. Assert: the checks above, read on the initial node.
    """
    with vm_factory(firewall_mode="off") as head:
        head_input = {"host": head.host_name, "user": "ops"}
        with vm_factory(
            bootstrap=False,
            key_path=head.key_path,
            node_type="agent",
            head=head_input,
            firewall_mode="off",
            readiness_timeout=READINESS,
        ) as agent:
            killed = agent.run_apply_killing_converge()
            assert killed.returncode != 0
            assert_converge_interrupted(agent)

            retry = agent.run_apply()
            checked_at = datetime.now(timezone.utc)

            assert retry.returncode == 0, retry.stdout + retry.stderr
            assert f"{CONVERGE_ADDR}: Creation complete" in retry.stdout
            probe(head, "sudo -n k3s kubectl wait --for=condition=Ready node --all --timeout=180s")
            nodes = json.loads(probe(head, "sudo -n k3s kubectl get nodes -o json"))["items"]
            assert len(nodes) == 2
            assert all(
                (expiry - checked_at).total_seconds() <= TOKEN_TTL_SECONDS
                for expiry in bootstrap_token_expiries(head)
            )
