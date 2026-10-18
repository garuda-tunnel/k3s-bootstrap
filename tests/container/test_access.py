"""Exercise access provisioning and the new-account connection gate."""

from __future__ import annotations

import shlex
import subprocess
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


def generate_public_key(tmp_path: Path, name: str) -> str:
    """Generate a disposable public key used to arrange key-file state."""
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
    )
    return key_path.with_suffix(".pub").read_text().strip()


def authorized_keys(host: Any, user: str) -> list[str]:
    """Read a user's key file so tests assert its observable contents."""
    result = host.run(f"cat /home/{shlex.quote(user)}/.ssh/authorized_keys")
    assert result.rc == 0, result.stdout + result.stderr
    return result.stdout.splitlines()


def run_access(node: SystemdHost, user: str, keys: list[str]) -> subprocess.CompletedProcess[str]:
    """Run preflight and access, including the fresh ``ssh_user`` connection gate."""
    return run_playbook(
        node,
        playbook_vars(ssh_user=user, authorized_keys=keys),
        extra_args=("--tags", "preflight,access"),
    )


def test_creates_missing_ssh_user_and_verifies_its_login(systemd_host: SystemdHost) -> None:
    """Verify access creates a missing ``ssh_user`` that can log in with its key and sudo.

    Validates: the access role and the new-access gate.
    Code: ansible/roles/access/tasks/main.yml; ansible/k3s-node.yml (play ``Verify new access``)

    Assertion: the run exits 0, the account exists, and an SSH login as it runs ``sudo -n``.

    Method:
    1. Arrange: a user name that does not exist on the node.
    2. Act: run preflight and access.
    3. Assert: exit code, account presence and the SSH ``sudo -n`` exit code.
    """
    user = "accessnewuser"
    key = systemd_host.key_path.with_suffix(".pub").read_text().strip()
    result = run_access(systemd_host, user, [key])

    assert result.returncode == 0, result.stdout + result.stderr
    assert systemd_host.host.user(user).exists
    ssh = ssh_as(systemd_host, user, "sudo", "-n", "true")
    assert ssh.returncode == 0, ssh.stdout + ssh.stderr


def test_existing_user_keeps_uid_home_and_primary_group(systemd_host: SystemdHost) -> None:
    """Verify access adopts a pre-existing user without altering uid, home or primary group.

    Validates: converge never rewrites an account the operator already created.
    Code: ansible/roles/access/tasks/main.yml ("Create the SSH user when absent")

    Assertion: the playbook exits 0 and the account's ``getent passwd`` line (uid 1500, its
    home and the admins primary group) is identical afterwards. Changing identity attributes
    would orphan files and break existing logins.

    Method:
    1. Arrange: create the user with uid 1500 and group admins; record its passwd entry.
    2. Act: run the preflight and access roles for that user.
    3. Assert: exit code 0 and an unchanged passwd entry.
    """
    user = "accessexisting"
    host = systemd_host.host
    assert host.run("groupadd -f admins && useradd -m -u 1500 -g admins accessexisting").rc == 0
    before = host.run(f"getent passwd {user}").stdout.strip()
    result = run_access(
        systemd_host, user, [systemd_host.key_path.with_suffix(".pub").read_text().strip()]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert host.run(f"getent passwd {user}").stdout.strip() == before


def test_existing_distinct_keys_survive_when_a_new_key_is_added(
    systemd_host: SystemdHost, tmp_path: Path
) -> None:
    """Verify authorization adds a requested key without removing an existing distinct key.

    Validates: the access role uses additive authorized-key management.
    Code: ansible/roles/access/tasks/main.yml ("Authorize SSH keys")

    Assertion: the key file ends as [key1, key2] after separate converges for each key.
    Removing key1 would revoke an existing operator's access.

    Method:
    1. Arrange: generate two disposable keys and converge once with key1.
    2. Act: converge again with key2.
    3. Assert: both distinct keys remain in the authorized-keys file.
    """
    key1 = systemd_host.key_path.with_suffix(".pub").read_text().strip()
    key2 = generate_public_key(tmp_path, "key2")
    user = "accesskeys"
    assert run_access(systemd_host, user, [key1]).returncode == 0
    assert run_access(systemd_host, user, [key2]).returncode == 0
    assert authorized_keys(systemd_host.host, user) == [key1, key2]


def test_invalid_sudoers_never_installed(systemd_host: SystemdHost) -> None:
    """Verify a malformed sudoers template is rejected and never installed.

    Validates: sudoers content is validated before it replaces anything.
    Code: ansible/roles/access/tasks/main.yml ("Install passwordless sudo access", visudo
        validation); ansible/roles/access/templates/sudoers.j2

    Assertion: the playbook exits nonzero and /etc/sudoers.d/90-k3s-bootstrap does not exist.
    A broken sudoers file can lock every administrator out of sudo.

    Method:
    1. Arrange: remove any existing sudoers drop-in; overwrite the staged template with junk.
    2. Act: run the preflight and access roles.
    3. Assert: nonzero exit and the drop-in path absent.
    """
    user = "accessinvalidsudo"
    template = (
        Path(__file__).resolve().parents[2]
        / "ansible"
        / "roles"
        / "access"
        / "templates"
        / "sudoers.j2"
    )
    assert systemd_host.host.run("rm -f /etc/sudoers.d/90-k3s-bootstrap").rc == 0
    original = template.read_text()
    try:
        template.write_text("this is not sudoers\n")
        result = run_access(
            systemd_host,
            user,
            [systemd_host.key_path.with_suffix(".pub").read_text().strip()],
        )
    finally:
        template.write_text(original)

    assert result.returncode != 0, result.stdout + result.stderr
    assert systemd_host.host.run("test -e /etc/sudoers.d/90-k3s-bootstrap").rc != 0


def test_failed_new_access_check_keeps_bootstrap_access_and_sshd_unchanged(
    systemd_host: SystemdHost,
) -> None:
    """Verify a user without a working key stops the run at the gate, before hardening.

    Validates: hardening runs only after a fresh key login as ``ssh_user`` succeeded.
    Code: ansible/k3s-node.yml (play ``Verify new access``, tagged with ``ssh_hardening``)

    Assertion: the run fails at the gate, no hardening drop-in exists and root key login
    still works. Hardening first would lock the operator out.

    Method:
    1. Arrange: an existing user without the controller key; no drop-in.
    2. Act: run preflight and ssh_hardening (the gate is selected by the hardening tag).
    3. Assert: nonzero exit, failing task name, absent drop-in, working bootstrap login.
    """
    drop_in = "/etc/ssh/sshd_config.d/00-k3s-bootstrap.conf"
    host = systemd_host.host
    user = "accessgatefailure"
    assert host.run(f"rm -f {drop_in}").rc == 0
    assert host.run(f"useradd -m {user}").rc == 0
    result = run_playbook(
        systemd_host,
        playbook_vars(ssh_user=user, connection_timeout="1s"),
        extra_args=("--tags", "preflight,ssh_hardening"),
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Wait for a fresh SSH connection as the new user" in result.stdout + result.stderr
    assert not host.file(drop_in).exists
    bootstrap = ssh_as(systemd_host, "root", "true")
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr
