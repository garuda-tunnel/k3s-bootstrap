"""Verify real Ubuntu/Debian AppArmor policy parsing without loading kernel policy.

Run with ``pytest tests/container/test_apparmor.py`` on a dedicated Docker test host.
Live labels and the role's state transitions are covered by Molecule, not this tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Pytest adds the tier's fixture module to sys.path.
from conftest import SystemdHost, run_playbook  # pylint: disable=import-error

pytestmark = pytest.mark.container


def test_distro_runc_policy_parses_and_disable_marker_prevents_duplicate_loads(
    systemd_host: SystemdHost,
) -> None:
    """Pin the parser contract used by AppArmor boot/reload and package postinst.

    Code: ansible/roles/k3s/templates/apparmor/runc.j2, tasks/apparmor/enable.yml
    Assertion: the native distro ABI/body parses after extension, directory
    processing exposes exactly one runc, explicit parsing skips disabled stock,
    and removing the disable marker makes stock visible again.
    Method: run the parser-only helper over SSH; all parser calls use -QTK so
    the shared Docker host's kernel policy is never modified.
    """
    result = run_playbook(
        systemd_host,
        {},
        playbook=Path(__file__).with_name("apparmor_render.yml"),
        extra_args=("--tags", "always"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
