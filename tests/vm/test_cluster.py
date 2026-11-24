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
import time
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


def test_cross_node_traffic_through_managed_firewalls(cluster: Cluster) -> None:
    """Verify pod-to-pod and kubelet-metrics traffic across firewalled nodes.

    Validates: the managed firewall on every node and the wireguard-native flannel
    default keep cross-node traffic working: a pod reaches a pod on another node
    through the WireGuard overlay (UDP 51820, deliberately not gated), and
    metrics-server scrapes kubelet 10250 across nodes, which flannel's ip-masq
    rules make arrive with the source node IP (node_trusted_cidrs).
    Code: ansible/roles/firewall/templates/firewall.nft.j2; ansible/roles/k3s/tasks/main.yml

    Assertion: a ping between pods on two different nodes succeeds, the Metrics
    API returns usage for all three nodes, and the overlay interface is
    ``flannel-wg`` (never ``flannel.1``).

    Method:
    1. Arrange: the applied composition, all nodes Ready; one busybox pod pinned
    to each of two distinct nodes.
    2. Act: ping the second pod's IP from the first; poll the Metrics API
    (``/apis/metrics.k8s.io/v1beta1/nodes``) and read the overlay links on the hub.
    3. Assert: the statements above.

    Status: UNVERIFIED. Written against the fixture and the k3s/flannel
    behaviour documented in the root README; this tier needs Vagrant/libvirt,
    which is not available where this test was authored. Run it with the
    command in tests/vm/README.md before trusting it.
    """
    hub = cluster.hub
    kubectl = "sudo -n k3s kubectl"
    nodes = json.loads(probe(hub, f"{kubectl} get nodes -o json"))["items"]
    names = sorted(node["metadata"]["name"] for node in nodes)
    assert len(names) == 3
    image = "docker.io/library/busybox:1.37.0"

    for pod, node in zip(("k3sb-cross-a", "k3sb-cross-b"), (names[0], names[1]), strict=True):
        probe(
            hub,
            f"sudo -n k3s kubectl run {pod} --image={image} --restart=Never"
            f' --overrides \'{{"spec":{{"nodeName":"{node}"}}}}\''
            " --command -- sleep 600",
        )
        probe(hub, f"{kubectl} wait --for=condition=Ready pod/{pod} --timeout=180s")

    # The overrides pin the pods; prove they really landed on distinct nodes.
    pod_nodes = [
        probe(hub, f"{kubectl} get pod {pod} -o jsonpath='{{.spec.nodeName}}'")
        for pod in ("k3sb-cross-a", "k3sb-cross-b")
    ]
    assert pod_nodes[0] and pod_nodes[1] and pod_nodes[0] != pod_nodes[1]

    ips = [
        probe(hub, f"{kubectl} get pod {pod} -o jsonpath='{{.status.podIP}}'")
        for pod in ("k3sb-cross-a", "k3sb-cross-b")
    ]
    assert ips[0] and ips[1] and ips[0] != ips[1]

    # The overlay carries the ping between the two nodes.
    probe(hub, f"{kubectl} exec k3sb-cross-a -- ping -c 1 -W 2 {ips[1]}")

    # kubelet 10250 across nodes: metrics-server scrapes each kubelet, and
    # flannel masquerades pod traffic to node IPs, so the source is a peer
    # node IP that node_trusted_cidrs must allow. `kubectl top nodes`
    # prints <unknown> for nodes it cannot scrape, so poll the Metrics API
    # instead: it only lists nodes that were actually scraped.
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        raw = hub.ssh(f"{kubectl} get --raw /apis/metrics.k8s.io/v1beta1/nodes")
        if raw.returncode == 0:
            items = json.loads(raw.stdout).get("items", [])
            scraped = {item["metadata"]["name"] for item in items}
            if scraped >= set(names) and all(item.get("usage") for item in items):
                break
        time.sleep(15)
    else:
        pytest.fail(f"kubelet metrics never covered every node:\n{raw.stderr}")

    # The wireguard-native default built the overlay; VXLAN would have left flannel.1.
    probe(hub, "sudo -n ip link show flannel-wg")
    assert hub.ssh("sudo -n ip link show flannel.1").returncode != 0

    for pod in ("k3sb-cross-a", "k3sb-cross-b"):
        hub.ssh(f"{kubectl} delete pod {pod} --wait=false --ignore-not-found")
