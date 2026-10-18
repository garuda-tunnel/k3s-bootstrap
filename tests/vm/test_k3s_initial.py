"""Initial-node module contract.

The role-level contracts of an `initial` node (etcd datastore, kubeconfig
group, input rejections, no-op reconverge, config-change restart) live in
the Molecule `default` scenario; only the contract of one real `tofu apply`
and the targeted-destroy contract stay here because they drive the module
itself. It does not duplicate Molecule role-level coverage. Run with
``K3SB_VM_TESTS=1 pytest tests/vm`` (see tests/vm/README.md for the libvirt wrapper
and timeout).
"""

from __future__ import annotations

from typing import Any

import pytest

# Pytest puts the test directory on sys.path; pylint does not know about it.
from conftest import DESTROY_SECONDS, probe, snapshot  # pylint: disable=import-error

pytestmark = pytest.mark.vm


def test_destroying_bootstrap_resource_keeps_k3s(vm_factory: Any) -> None:
    """Verify a module apply yields a Ready node, and destroying it leaves k3s and its data.

    Validates: the whole module apply (the controller-side playbook run) produces a working
    initial node; ``tofu destroy -target`` removes only the bootstrap bookkeeping, not the host.
    Code: main.tf (terraform_data resources have no destroy-time provisioners);
        ansible/k3s-node.yml

    Assertion: after the apply ``kubectl get nodes`` shows Ready without NotReady and k3s is
    active. After the destroy k3s is still active and the config, CA, datastore inode, binary
    mtime, service start timestamp and a marker ConfigMap are identical to before. A destroy
    that uninstalled or restarted k3s would turn a state cleanup into data loss.

    Method:
    1. Arrange: an initial stand (provision plus bootstrap); check the node; add a marker
       ConfigMap and take a snapshot.
    2. Act: ``tofu destroy -target=module.k3s_bootstrap``.
    3. Assert: ``systemctl is-active k3s`` is "active" and the snapshot is unchanged.
    """
    with vm_factory() as stand:
        nodes = probe(stand, "sudo -n k3s kubectl get nodes")
        assert "Ready" in nodes
        assert "NotReady" not in nodes
        assert probe(stand, "systemctl is-active k3s") == "active"
        # Datastore marker: any silent reset of the k3s data wipes it,
        # which the before/after snapshots would then catch.
        probe(
            stand,
            "sudo -n k3s kubectl create configmap k3sb-safety-marker"
            " --from-literal=value=protected",
        )
        before = snapshot(stand)
        stand.stage_tofu(
            "destroy-module",
            DESTROY_SECONDS,
            "destroy",
            "-auto-approve",
            "-no-color",
            "-target=module.k3s_bootstrap",
            f"-state={stand.state}",
            f"-var-file={stand.work_dir / 'terraform.tfvars.json'}",
        )
        assert probe(stand, "systemctl is-active k3s") == "active"
        assert snapshot(stand) == before
