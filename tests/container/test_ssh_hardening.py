"""Exercise controller-gated SSH hardening and host-key policy."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

# Pytest puts the test directory on sys.path; pylint does not know about it.
from conftest import (  # pylint: disable=import-error
    SystemdHost,
    playbook_vars,
    run_playbook,
    ssh_as,
)

pytestmark = pytest.mark.container

SSH_DROP_IN = "/etc/ssh/sshd_config.d/00-k3s-bootstrap.conf"
SSH_TAGS = ("--tags", "preflight,access,ssh_hardening")
REACH = Path(__file__).resolve().parent / "reach.yml"


def sshd_effective(host: Any) -> dict[str, str]:
    """Return sshd's effective settings for assertions against the live daemon."""
    result = host.run("sshd -T")
    assert result.rc == 0, result.stdout + result.stderr
    return dict(line.split(maxsplit=1) for line in result.stdout.splitlines())


def run_hardening(node: SystemdHost, user: str) -> subprocess.CompletedProcess[str]:
    """Run preflight, new-account verification, and SSH hardening for ``user``."""
    key = node.key_path.with_suffix(".pub").read_text().strip()
    return run_playbook(
        node,
        playbook_vars(ssh_user=user, authorized_keys=[key]),
        extra_args=SSH_TAGS,
    )


@pytest.fixture()
def restore_sshd_state(systemd_host: SystemdHost) -> Iterator[None]:
    """Restore the test container's SSH drop-in after each test."""
    previous = systemd_host.host.run(f"cat {SSH_DROP_IN}")
    yield
    if previous.rc == 0:
        command = f"printf %s {shlex.quote(previous.stdout)} > {SSH_DROP_IN}"
    else:
        command = f"rm -f {SSH_DROP_IN}"
    restored = systemd_host.host.run(f"{command} && systemctl reload ssh")
    assert restored.rc == 0, restored.stdout + restored.stderr


@contextmanager
def replaced_template(body: str) -> Iterator[None]:
    """Temporarily replace the controller-side hardening template for validation testing."""
    path = (
        Path(__file__).resolve().parents[2]
        / "ansible"
        / "roles"
        / "ssh_hardening"
        / "templates"
        / "sshd_config.conf.j2"
    )
    original = path.read_text()
    try:
        path.write_text(body)
        yield
    finally:
        path.write_text(original)


@pytest.mark.usefixtures("restore_sshd_state")
def test_key_login_works_and_password_and_root_login_are_denied(systemd_host: SystemdHost) -> None:
    """Verify hardening leaves key login and sudo working and denies password and root login.

    Validates: the ssh_hardening drop-in is effective in the live daemon.
    Code: ansible/roles/ssh_hardening/templates/sshd_config.conf.j2; ansible/k3s-node.yml
        (play ``Verify new access``)

    Assertion: ``sshd -T`` reports password, keyboard-interactive and root login off; the new
    user logs in with its key and runs ``sudo -n``; root and a password attempt are refused.
    A lockout here would leave the node unmanageable.

    Method:
    1. Arrange: a new ``ssh_user`` authorized with the controller key.
    2. Act: run preflight, access and ssh_hardening.
    3. Assert: effective settings and the four login outcomes (exit codes only).
    """
    user = "sshpassworddisabled"
    result = run_hardening(systemd_host, user)

    assert result.returncode == 0, result.stdout + result.stderr
    effective = sshd_effective(systemd_host.host)
    assert effective["passwordauthentication"] == "no"
    assert effective["kbdinteractiveauthentication"] == "no"
    assert effective["permitrootlogin"] == "no"
    assert ssh_as(systemd_host, user, "sudo", "-n", "true").returncode == 0
    assert ssh_as(systemd_host, "root", "true").returncode != 0
    password_only = subprocess.run(
        [
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-p",
            str(systemd_host.ssh_port),
            f"{user}@127.0.0.1",
            "true",
        ],
        input="incorrect-password\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert password_only.returncode != 0


@pytest.mark.usefixtures("restore_sshd_state")
def test_invalid_sshd_drop_in_is_rolled_back(systemd_host: SystemdHost) -> None:
    """Verify a drop-in that ``sshd -t`` rejects is rolled back to the previous one.

    Validates: the validate-then-reload rescue of the ssh_hardening role.
    Code: ansible/roles/ssh_hardening/tasks/main.yml (block/rescue)

    Assertion: the run fails at the validation task, the old drop-in content is back and
    the live daemon still uses it. A broken sshd config would lock out every login.

    Method:
    1. Arrange: a known previous drop-in; replace the template with an invalid directive.
    2. Act: run the hardening tags.
    3. Assert: nonzero exit, failing task name, restored file and effective setting.
    """
    old_content = "PasswordAuthentication yes\n"
    host = systemd_host.host
    assert (
        host.run(f"printf %s {shlex.quote(old_content)} > {SSH_DROP_IN} && systemctl reload ssh").rc
        == 0
    )
    with replaced_template("InvalidSshDirective yes\n"):
        result = run_hardening(systemd_host, "sshinvalidsnippet")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Validate the complete SSH configuration" in result.stdout + result.stderr
    assert host.run(f"cat {SSH_DROP_IN}").stdout == old_content
    assert sshd_effective(host)["passwordauthentication"] == "yes"


def test_host_key_check_rejects_unknown_and_accepts_known_host_keys(
    systemd_host: SystemdHost, tmp_path: Path
) -> None:
    """Verify ``host_key_check`` enforces known host keys on node and gate connections.

    Validates: FR-007 (a verified host key is never silently skipped).
    Code: ansible/k3s-node.yml (``Add the target node``, play ``Access``, play
        ``Verify new access``)

    Assertion: with checking on, reaching the node with an unknown key fails, and the access
    run passes once the key is scanned, on the bootstrap connection and on the fresh
    ``ssh_user`` gate connection. Checking off is the default of every other container test.

    Method:
    1. Arrange: an empty known_hosts file in ``tmp_path``, handed to ssh through
       ``ansible_ssh_extra_args`` so the operator's own ``~/.ssh/known_hosts`` is never
       touched (ssh resolves ``~`` from the passwd entry, so ``HOME`` cannot redirect it).
    2. Act: reach the node, scan its key into the file, run the access gate.
    3. Assert: exit codes as above.
    """
    known_hosts = tmp_path / "known_hosts"
    known_hosts.touch()
    isolated = ("-e", f"ansible_ssh_extra_args='-o UserKnownHostsFile={known_hosts}'")
    key = systemd_host.key_path.with_suffix(".pub").read_text().strip()

    unknown = run_playbook(
        systemd_host,
        {"host_key_check": True},
        playbook=REACH,
        extra_args=("--tags", "always", *isolated),
    )
    scanned = subprocess.run(
        ["ssh-keyscan", "-p", str(systemd_host.ssh_port), "127.0.0.1"],
        capture_output=True,
        text=True,
        check=False,
    )
    known_hosts.write_text(scanned.stdout)
    gated = run_playbook(
        systemd_host,
        playbook_vars(ssh_user="sshhostkeygate", authorized_keys=[key], host_key_check=True),
        extra_args=("--tags", "access", *isolated),
    )

    assert unknown.returncode != 0, unknown.stdout + unknown.stderr
    assert scanned.returncode == 0, scanned.stdout + scanned.stderr
    assert gated.returncode == 0, gated.stdout + gated.stderr
