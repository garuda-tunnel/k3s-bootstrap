"""Exercise refusals of the host role through the controller-run playbook.

Run with ``pytest tests/container/test_host.py``.
"""

from __future__ import annotations

import re

import pytest

# Pytest puts the test directory on sys.path; pylint does not know about it.
from conftest import SystemdHost, playbook_vars, run_playbook  # pylint: disable=import-error

pytestmark = pytest.mark.container


def test_rejects_missing_kernel_prerequisites(systemd_host: SystemdHost) -> None:
    """Verify unavailable bridge sysctls fail the run instead of being skipped.

    Validates: host preparation never silently skips a k3s-owned sysctl.
    Code: ansible/roles/host/tasks/main.yml ("Configure k3s-owned sysctls")

    Method: temporarily mask the container's bridge sysctls with an empty mount, then
    require failure and restore the real procfs view even if the assertion fails.
    """
    host = systemd_host.host
    key = systemd_host.key_path.with_suffix(".pub").read_text().strip()
    variables = playbook_vars(ssh_user="hostkernel", authorized_keys=[key])
    assert host.run("mount -t tmpfs -o size=4k tmpfs /proc/sys/net/bridge").rc == 0
    try:
        result = run_playbook(
            systemd_host, variables, extra_args=("--tags", "preflight,access,host")
        )

        assert result.returncode != 0, result.stdout + result.stderr
        assert re.search(
            r"TASK \[host : Configure k3s-owned sysctls\][^\n]*\n"
            r"(?:(?!TASK \[)[\s\S])*?(?:fatal|failed):",
            result.stdout,
        ), (
            result.stdout + result.stderr
        )
    finally:
        assert host.run("umount /proc/sys/net/bridge").rc == 0
