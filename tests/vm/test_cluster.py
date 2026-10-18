"""Cluster composition VM contract: one initial hub and two agents in one tofu config.

The caller composition in tests/vm/fixture/cluster/main.tf wires both agents to
``head = module.hub.head``, so OpenTofu applies the hub first and the agents in
parallel. Token secrecy in the verbose output is proven by the Molecule ``cluster``
scenario, and in the state by ``phases.tftest.hcl``. Run with
``K3SB_VM_TESTS=1 pytest tests/vm`` (see tests/vm/README.md for the libvirt wrapper and
timeout).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

import pytest

# Pytest puts the test directory on sys.path; pylint does not know about it.
from conftest import (  # pylint: disable=import-error
    BOOTSTRAP_SECONDS,
    FIXTURE_DIR,
    K3S_VERSION,
    SHORT_TIMEOUT,
    command,
    management_cidr,
    probe,
)

pytestmark = pytest.mark.vm

CLUSTER_DIR = FIXTURE_DIR / "cluster"


@dataclass(frozen=True)
class Cluster:
    """The applied composition: its stands."""

    hub: Any
    agents: list[Any]


@pytest.fixture(scope="module")
def cluster(vm_factory: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    """Provision three VMs with one key, apply the composition, destroy at the end."""
    work = tmp_path_factory.mktemp("k3sb-cluster-")
    state = work / "terraform.tfstate"
    env = os.environ | {
        "TF_DATA_DIR": str(work / "tofu-data"),
        "TF_INPUT": "0",
    }
    with ExitStack() as stands:
        hub = stands.enter_context(vm_factory(bootstrap=False))
        agents = [
            stands.enter_context(vm_factory(bootstrap=False, key_path=hub.key_path))
            for _ in range(2)
        ]
        variables = work / "terraform.tfvars.json"
        variables.write_text(
            json.dumps(
                {
                    "hub": {"host": hub.host_name, "host_generation": hub.instance_name},
                    "agents": {
                        agent.name_suffix: {
                            "host": agent.host_name,
                            "host_generation": agent.instance_name,
                        }
                        for agent in agents
                    },
                    "k3s_version": K3S_VERSION,
                    "ssh_private_key_file": str(hub.key_path),
                    "authorized_keys": [hub.key_path.with_suffix(".pub").read_text().strip()],
                    "api_trusted_cidrs": [management_cidr()],
                }
            )
        )
        init = command(
            "tofu", "init", "-backend=false", cwd=CLUSTER_DIR, env=env, timeout=SHORT_TIMEOUT
        )
        assert init.returncode == 0, init.stdout + init.stderr
        tofu = ("-auto-approve", "-no-color", f"-state={state}", f"-var-file={variables}")
        try:
            applied = command(
                "tofu", "apply", *tofu, cwd=CLUSTER_DIR, env=env, timeout=2 * BOOTSTRAP_SECONDS
            )
            (hub.log_dir / "cluster-apply.log").write_text(applied.stdout + applied.stderr)
            assert applied.returncode == 0, f"cluster apply failed; log: {hub.log_dir}"
            yield Cluster(hub, agents)
        finally:
            command("tofu", "destroy", *tofu, cwd=CLUSTER_DIR, env=env, timeout=SHORT_TIMEOUT)


def test_agents_join_the_hub_in_parallel(cluster: Cluster) -> None:
    """Verify the composition yields three Ready nodes and no leftover bootstrap token.

    Validates: FR-034/FR-036 (per-node modules joined through ``head``; agents join with a
    short-lived bootstrap token deleted after use).
    Code: tests/vm/fixture/cluster/main.tf; ansible/roles/k3s/tasks/join_agent.yml

    Assertion: three nodes are Ready, one of them control-plane, and the hub holds no
    bootstrap token after both agents joined.

    Method:
    1. Arrange: the applied composition (fixture).
    2. Act: read nodes and bootstrap-token secrets on the hub.
    3. Assert: counts above.
    """
    hub = cluster.hub
    probe(hub, "sudo -n k3s kubectl wait --for=condition=Ready node --all --timeout=180s")
    nodes = json.loads(probe(hub, "sudo -n k3s kubectl get nodes -o json"))["items"]
    tokens = json.loads(
        probe(
            hub,
            "sudo -n k3s kubectl get secrets -n kube-system"
            " --field-selector type=bootstrap.kubernetes.io/token -o json",
        )
    )["items"]

    assert len(nodes) == 3
    assert [
        "node-role.kubernetes.io/control-plane" in node["metadata"]["labels"] for node in nodes
    ].count(True) == 1
    assert not tokens
