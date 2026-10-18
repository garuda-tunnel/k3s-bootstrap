"""Provide Docker-backed fixtures for the container integration tier.

The tier mirrors the module's deployment: pytest is the controller, a systemd
container (Ubuntu 26.04, ``openssh-server``, ``sudo``, ``python3``) is a plain
SSH target that never receives a copy of the repository, and ``run_playbook``
launches the real ``ansible/k3s-node.yml`` through ``uv run`` as the module
does. Keep these attribute names stable:

    systemd_host.host          testinfra host (docker backend), for arrange and assert steps
    systemd_host.container_id  docker container id
    systemd_host.ssh_port      published SSH port on 127.0.0.1
    systemd_host.key_path      ephemeral ed25519 key authorized for root

``systemd_host`` is the node under test; ``head_host`` is a second container of
the same kind that plays the cluster head. Both are removed at the end of the
session. This module does not test role behavior. Run with ``pytest tests/container``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest
import testinfra

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO_ROOT / "ansible" / "k3s-node.yml"
# The module's local-exec command (main.tf), with the playbook path as a field.
LAUNCH_COMMAND = (
    "uv run --no-project --with-requirements ansible/requirements.txt "
    'ansible-playbook -i localhost, {playbook} -e "$K3SB_VARS"'
)
IMAGE_TAG = "k3s-bootstrap-test:ubuntu-2604"
SYSTEMD_WAIT_SECONDS = 60
SSH_WAIT_SECONDS = 30
PLAYBOOK_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class SystemdHost:
    """Expose a running systemd container, reachable over SSH as root, to tests."""

    host: Any
    container_id: str
    ssh_port: int
    key_path: Path


def run_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run Docker and fail with its output unless a test needs the nonzero result."""
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed with code {result.returncode}:\n"
            f"{result.stderr.strip()}"
        )
    return result


def node_vars(node: SystemdHost) -> dict[str, Any]:
    """Return the module inputs that address ``node`` as an initial node, as the module does."""
    return {
        "node_type": "initial",
        "host": "127.0.0.1",
        "port": node.ssh_port,
        "bootstrap_user": "root",
        "ssh_user": "controller",
        "ssh_private_key_file": str(node.key_path),
        "host_key_check": False,
        "connection_timeout": "5s",
        "readiness_timeout": "1m",
        "firewall_mode": "managed",
        "k3s_config": {},
        "authorized_keys": [node.key_path.with_suffix(".pub").read_text().strip()],
        "sysctls": {},
        "kernel_modules": [],
        "k3s_version": "v1.33.0+k3s1",
        "api_trusted_cidrs": [],
        "cluster_cidrs": [],
        "grow_root_filesystem": False,
    }


def playbook_vars(**overrides: Any) -> dict[str, Any]:
    """Build the inputs a preflight-style run overrides on top of ``node_vars``.

    ``run_playbook`` merges them over ``node_vars``, which already holds the other valid
    defaults, so each test changes only its target condition.
    """
    return {
        "ssh_user": "preflightuser",
        "authorized_keys": [
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFRlc3RLZXlGb3JQcmVmbGlnaHQxMjM0NTY3ODkw test"
        ],
        "connection_timeout": "5m",
        "readiness_timeout": "10m",
    } | overrides


def ssh_as(
    node: SystemdHost, user: str, *command: str, key_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a bounded, key-only, non-interactive SSH command against the node as ``user``."""
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-i",
            str(key_path or node.key_path),
            "-p",
            str(node.ssh_port),
            f"{user}@127.0.0.1",
            *command,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def run_playbook(
    node: SystemdHost,
    variables: dict[str, Any],
    *,
    head: SystemdHost | None = None,
    playbook: Path = PLAYBOOK,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """Launch the playbook from the controller against ``node`` without raising on its result.

    ``variables`` override the connection inputs of ``node_vars``; ``head`` turns the run
    into a join through that container; ``extra_args`` go to ``ansible-playbook`` (for
    example ``--tags``). Arrange steps on the node use ``node.host`` or ``docker exec``.
    """
    joined = {"head": {"host": "127.0.0.1", "port": head.ssh_port, "user": "root"}} if head else {}
    merged = node_vars(node) | joined | variables
    # The environment of the module's local-exec (main.tf). Test-only override: no SSH
    # agent, so only the controller key can authenticate.
    environment = os.environ | {
        "K3SB_VARS": json.dumps(merged),
        "ANSIBLE_CONFIG": str(REPO_ROOT / "ansible" / "ansible.cfg"),
        "SSH_AUTH_SOCK": "",
    }
    command = LAUNCH_COMMAND.format(playbook=playbook.relative_to(REPO_ROOT))
    command += "".join(f" {shlex.quote(arg)}" for arg in extra_args)
    return subprocess.run(
        command,
        shell=True,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=PLAYBOOK_TIMEOUT_SECONDS,
    )


def wait_for_systemd(container_id: str) -> None:
    """Block until systemd reports running or degraded (bounded wait)."""
    deadline = time.monotonic() + SYSTEMD_WAIT_SECONDS
    while True:
        result = run_docker("exec", container_id, "systemctl", "is-system-running", check=False)
        state = (result.stdout + result.stderr).strip()
        if state in ("running", "degraded"):
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"systemd did not reach running/degraded within "
                f"{SYSTEMD_WAIT_SECONDS}s (last state: {state!r})"
            )
        time.sleep(1.0)


def wait_for_ssh_port(port: int) -> None:
    """Wait for the published SSH port so subsequent real SSH probes are deterministic."""
    deadline = time.monotonic() + SSH_WAIT_SECONDS
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            if time.monotonic() >= deadline:
                pytest.fail(f"SSH port {port} not reachable within {SSH_WAIT_SECONDS}s")
            time.sleep(1.0)


def start_host(key_path: Path) -> SystemdHost:
    """Boot one systemd container reachable over SSH as root with ``key_path``."""
    container_id = run_docker(
        "run",
        "-d",
        "--privileged",
        "--cgroupns=host",
        "--sysctl",
        "net.bridge.bridge-nf-call-iptables=1",
        "--sysctl",
        "net.bridge.bridge-nf-call-ip6tables=1",
        "-v",
        "/sys/fs/cgroup:/sys/fs/cgroup:rw",
        "-v",
        "/lib/modules:/lib/modules:ro",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/run/lock",
        "-p",
        "127.0.0.1::22",
        IMAGE_TAG,
    ).stdout.strip()
    try:
        wait_for_systemd(container_id)
        run_docker(
            "exec",
            container_id,
            "sh",
            "-c",
            "(systemctl disable --now ssh.socket >/dev/null 2>&1 || true) && "
            "mkdir -p /run/sshd && "
            "systemctl enable --now ssh.service >/dev/null 2>&1",
        )
        ssh_port = int(
            run_docker("port", container_id, "22").stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        )
        wait_for_ssh_port(ssh_port)
        public_key = key_path.with_suffix(".pub").read_text().strip()
        run_docker(
            "exec",
            container_id,
            "sh",
            "-c",
            "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
            f"echo {shlex.quote(public_key)} >> /root/.ssh/authorized_keys && "
            "chmod 600 /root/.ssh/authorized_keys",
        )
    except BaseException:
        run_docker("rm", "-f", container_id, check=False)
        raise
    return SystemdHost(
        host=testinfra.get_host(f"docker://{container_id}"),
        container_id=container_id,
        ssh_port=ssh_port,
        key_path=key_path,
    )


@pytest.fixture(scope="session", name="controller_key")
def fixture_controller_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the test image, then create the controller's ephemeral key (not copied anywhere)."""
    if shutil.which("uv") is None or not PLAYBOOK.is_file():
        pytest.fail(f"the controller needs uv on PATH and {PLAYBOOK}")
    run_docker("build", "-t", IMAGE_TAG, str(REPO_ROOT / "tests" / "container"))
    # Load the real host modules before Docker creates the test network namespace.
    # A privileged helper avoids requiring sudo on the machine running pytest.
    run_docker(
        "run",
        "--rm",
        "--privileged",
        "-v",
        "/lib/modules:/lib/modules:ro",
        "--entrypoint",
        "modprobe",
        IMAGE_TAG,
        "-a",
        "br_netfilter",
        "overlay",
    )
    key_path = tmp_path_factory.mktemp("ssh") / "id_ed25519"
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "k3s-bootstrap-container-test",
            "-f",
            str(key_path),
        ],
        check=True,
    )
    return key_path


@pytest.fixture(scope="session")
def systemd_host(controller_key: Path) -> Iterator[SystemdHost]:
    """Provide the node under test and remove its container after the session."""
    node = start_host(controller_key)
    try:
        yield node
    finally:
        run_docker("rm", "-f", node.container_id, check=False)


@pytest.fixture(scope="session")
def head_host(controller_key: Path) -> Iterator[SystemdHost]:
    """Provide a second container that plays the cluster head, authorized for the same key."""
    head = start_host(controller_key)
    try:
        yield head
    finally:
        run_docker("rm", "-f", head.container_id, check=False)
