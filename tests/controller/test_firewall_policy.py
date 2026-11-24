"""Check real nftables verdicts in a disposable network namespace."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent


def unavailable(reason: str) -> None:
    """Fail the required CI gate, but permit local machines without namespaces."""
    if os.environ.get("K3SB_FIREWALL_REQUIRED") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.mark.parametrize("peers", [True, False], ids=["peers", "no-peers"])
def test_managed_firewall_packet_verdicts(tmp_path: Path, peers: bool) -> None:
    """Verify node, API, cluster and foreign sources against the rendered ruleset."""
    if not all(shutil.which(tool) for tool in ("unshare", "ip", "nft")):
        unavailable("sandbox tools unavailable")
    check = subprocess.run(
        [sys.executable, str(HERE / "firewall_sandbox.py"), "--check"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if check.returncode:
        unavailable(f"disposable nftables namespace unavailable: {check.stderr!r}")
    rules = tmp_path / "firewall.nft"
    variables = {
        "template_path": str(ROOT / "ansible/roles/firewall/templates/firewall.nft.j2"),
        "rules_path": str(rules),
        "api_trusted_cidrs": ["203.0.113.0/24", "2001:db8:2::/48"],
        "cluster_cidrs": ["10.42.0.0/16", "fd00:42::/48"],
        "node_trusted_cidrs": ["192.0.2.0/24", "2001:db8:1::/48"] if peers else [],
    }
    varfile = tmp_path / "vars.json"
    varfile.write_text(json.dumps(variables), encoding="utf-8")
    subprocess.run(
        [
            str(Path(sys.executable).parent / "ansible-playbook"),
            str(HERE / "render_firewall.yml"),
            "-e",
            f"@{varfile}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = subprocess.run(
        [sys.executable, str(HERE / "firewall_sandbox.py"), str(rules)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    verdicts = {
        parts[0]: [int(value) for value in parts[1:]]
        for line in result.stdout.splitlines()
        if (parts := line.split())
    }
    for address in ("192.0.2.10", "2001:db8:1::1"):
        # Node trust implies API/etcd trust, so a peer is accepted on all
        # five gated ports when peers are listed, and dropped on all of
        # them when they are not.
        assert verdicts[address] == [int(peers)] * 5 + [1]
    for address in ("203.0.113.10", "2001:db8:2::1"):
        assert verdicts[address] == [1, 1, 1, 0, 0, 1]
    for address in ("10.42.1.1", "fd00:42::1"):
        assert verdicts[address] == [1, 1, 1, 1, 0, 1]
    for address in ("198.18.0.1", "2001:db8:3::1"):
        assert verdicts[address] == [0, 0, 0, 0, 0, 1]

    for address in ("127.0.0.1", "::1"):
        for port in (2379, 2380, 6443, 10250, 8472):
            assert verdicts[f"loop-{address}-{port}"] == [1]
