"""Check that the runc attachment extension preserves distro policy verbatim.

No kernel policy is loaded; parser integration belongs to the container tier.
Run with ``pytest tests/controller/test_apparmor_render.py``.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
STOCK_ATTACHMENT = "/usr/{bin,sbin}/runc"
K3S_ATTACHMENT = "/{usr/{bin,sbin},var/lib/rancher/k3s/data/*/bin}/runc"


def run_tasks(
    tmp_path: Path, tasks: list[dict], variables: dict, *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run local Ansible task contracts; check mode protects main-path guard probes."""
    play = tmp_path / "probe.yml"
    play.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tasks": [
                        {
                            "ansible.builtin.include_vars": str(
                                ROOT / "ansible/roles/k3s/vars/main.yml"
                            )
                        },
                        *tasks,
                    ],
                }
            ]
        )
    )
    return subprocess.run(
        [
            str(Path(sys.executable).parent / "ansible-playbook"),
            "-i",
            "localhost,",
            str(play),
            "-e",
            json.dumps(variables),
            *(["--check"] if check else []),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("abi", "exec_rule"),
    [("4.0", ""), ("5.0", "  @{exec_path} mr,\n")],
    ids=["debian-13", "ubuntu-26.04"],
)
def test_extended_attachment_preserves_distro_policy(
    tmp_path: Path, abi: str, exec_rule: str
) -> None:
    """Prevent ABI or permission drift when extending the distro's attachment.

    Code: ansible/roles/k3s/templates/apparmor/runc.j2
    Assertion: only the attachment changes, including Ubuntu's exec mmap rule
    and an operator's additional rule; the conffile input is not rewritten.
    Method: run the real Ansible template action on both supported distro styles.
    """
    stock = (
        f"abi <abi/{abi}>,\ninclude <tunables/global>\n\n"
        f"profile runc {STOCK_ATTACHMENT} flags=(unconfined) {{\n"
        f"  userns,\n{exec_rule}  include if exists <local/runc>\n"
        "  /operator-rule r,\n}\n"
    )
    output = tmp_path / "rendered"
    result = run_tasks(
        tmp_path,
        [
            {
                "ansible.builtin.template": {
                    "src": str(ROOT / "ansible/roles/k3s/templates/apparmor/runc.j2"),
                    "dest": str(output),
                    "mode": "0600",
                }
            }
        ],
        {"k3s_apparmor_stock": {"content": base64.b64encode(stock.encode()).decode()}},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == stock.replace(STOCK_ATTACHMENT, K3S_ATTACHMENT)


def test_unknown_distro_declaration_fails_before_installation(tmp_path: Path) -> None:
    """Fail closed rather than install a policy that leaves embedded runc unattached.

    Code: ansible/roles/k3s/tasks/apparmor/main.yml
    Assertion: the real enable task rejects a changed distro declaration before
    checking/installing userspace or writing any policy. Only the input is local.
    Method: include the main tasks in check mode with an unsupported stock file.
    """
    stock = tmp_path / "stock"
    stock.write_text("profile runc /new/distro/runc flags=(unconfined) { userns, }\n")
    result = run_tasks(
        tmp_path,
        [
            {
                "ansible.builtin.include_tasks": str(
                    ROOT / "ansible/roles/k3s/tasks/apparmor/main.yml"
                )
            }
        ],
        {
            "k3s_apparmor_distro_profile": str(stock),
            "k3s_apparmor_enabled": True,
            "apparmor_container_profile_fix": True,
            "k3s_marker_path": str(tmp_path / "installed"),
        },
        check=True,
    )

    assert result.returncode != 0
    assert re.findall(r"TASK \[(.*?)\]", result.stdout)[-1] == (
        "Require the supported distro runc attachment"
    ), (result.stdout + result.stderr)
    assert "TASK [Migrate the v0.3.1 containerd profile override]" not in result.stdout
    assert "changed=0" in result.stdout


def test_rollback_preserves_operator_disable_marker_without_module_state(
    tmp_path: Path,
) -> None:
    """Only module-owned state authorizes rollback to remove a distro disable marker.

    Code: ansible/roles/k3s/tasks/apparmor/disable.yml
    Assertion: an operator's symlink alone is untouched and reports no changes.
    AppArmor is off so no kernel policy is loaded.
    """
    paths = {name: tmp_path / name for name in ("profile", "stamp", "stock", "disable")}
    paths["stock"].write_text("stock\n")
    paths["disable"].symlink_to(paths["stock"])
    result = run_tasks(
        tmp_path,
        [
            {
                "ansible.builtin.include_tasks": str(
                    ROOT / "ansible/roles/k3s/tasks/apparmor/disable.yml"
                )
            }
        ],
        {
            "k3s_apparmor_profile": str(paths["profile"]),
            "k3s_apparmor_loaded_stamp": str(paths["stamp"]),
            "k3s_apparmor_disable_link": str(paths["disable"]),
            "k3s_apparmor_distro_profile": str(paths["stock"]),
            "k3s_apparmor_enabled": False,
            "k3s_apparmor_distro": {"stat": {"exists": True}},
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert paths["disable"].is_symlink()
    assert not paths["profile"].exists()
    assert not paths["stamp"].exists()
    assert "changed=0" in result.stdout


@pytest.mark.parametrize("reason", ["unchanged", "missing-kernel", "changed-file"])
def test_load_reconciliation_checks_kernel_presence_and_file_changes(
    tmp_path: Path, reason: str
) -> None:
    """Reload if the kernel lost runc or template repair restored identical content.

    Code: ansible/roles/k3s/tasks/apparmor/enable.yml (load-needed task)
    Assertion: a matching completed-load stamp is insufficient in either case;
    unchanged file, symlink, stamp and loaded policy remain a no-op.
    Method: execute the actual production decision task with boundary facts.
    """
    tasks = yaml.safe_load((ROOT / "ansible/roles/k3s/tasks/apparmor/enable.yml").read_text())
    decision = next(
        task
        for task in tasks
        if "k3s_apparmor_load_needed" in task.get("ansible.builtin.set_fact", {})
    )
    result = run_tasks(
        tmp_path,
        [
            decision,
            {
                "ansible.builtin.assert": {
                    "that": f"k3s_apparmor_load_needed == {reason != 'unchanged'}"
                }
            },
        ],
        {
            "k3s_apparmor_stamp_file": {"stat": {"exists": True}},
            "k3s_apparmor_stamp": {"content": base64.b64encode(b"same\n").decode()},
            "k3s_apparmor_override": {"checksum": "same", "changed": reason == "changed-file"},
            "k3s_apparmor_distro_disabled": {"changed": False},
            "k3s_apparmor_loaded_profiles": {"matched": 0 if reason == "missing-kernel" else 1},
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("status", ["enabled", "enabled-runtime", "disabled", "masked", None])
def test_boot_loader_must_be_persistently_enabled(tmp_path: Path, status: str | None) -> None:
    """Reject boot paths that cannot load the module policy before k3s.

    Code: ansible/roles/k3s/tasks/apparmor/main.yml (boot-loader assertion)
    Assertion: only a persistently enabled AppArmor service is accepted.
    Method: execute the real assertion with systemd unit-facts boundary data.
    """
    tasks = yaml.safe_load((ROOT / "ansible/roles/k3s/tasks/apparmor/main.yml").read_text())
    validation = next(task["block"] for task in tasks if "block" in task)
    guard = next(
        task for task in validation if task["name"] == "Require persistent AppArmor boot loading"
    )
    units = {"apparmor.service": {"unitfilestate": status}} if status else {}
    result = run_tasks(tmp_path, [guard], {"k3s_apparmor_service": {"units": units}})

    assert (result.returncode == 0) == (status == "enabled"), result.stdout + result.stderr
    assert "changed=0" in result.stdout
