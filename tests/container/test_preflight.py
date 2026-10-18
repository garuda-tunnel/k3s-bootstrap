"""Exercise the preflight play: every refusal happens before the node is changed.

The controller (pytest) runs ``ansible/k3s-node.yml --tags preflight`` against the node
container; a second container plays the cluster head for the server and agent cases.
Does not cover successful host preparation. Run with ``pytest tests/container/test_preflight.py``.
"""

from __future__ import annotations

import re
import shlex
from contextlib import AbstractContextManager, contextmanager
from subprocess import CompletedProcess
from typing import Any, Iterator

import pytest

# Pytest puts the test directory on sys.path; pylint does not know about it.
from conftest import SystemdHost, playbook_vars, run_playbook  # pylint: disable=import-error

pytestmark = pytest.mark.container

K3S = "/var/lib/rancher/k3s"
MARKER = "/var/lib/k3s-bootstrap/installed"
CA_A = "-----BEGIN CERTIFICATE-----\nQUFB\n-----END CERTIFICATE-----\n"
CA_B = "-----BEGIN CERTIFICATE-----\nQkJC\n-----END CERTIFICATE-----\n"
TOKEN = "K10secret-token-marker::server:secret-token-marker"
DEAD_HEAD = {"host": "127.0.0.1", "port": 1, "user": "root"}


def preflight(
    node: SystemdHost, head: SystemdHost | None = None, **overrides: Any
) -> CompletedProcess:
    """Run only the preflight play (and the always-on inventory play) for ``node``."""
    return run_playbook(
        node, playbook_vars(**overrides), head=head, extra_args=("--tags", "preflight")
    )


def node_changed(result: CompletedProcess) -> int:
    """Return the node's ``changed`` count, failing if preflight never reached the node."""
    match = re.search(r"^node\s+:\s+ok=\d+\s+changed=(\d+)", result.stdout, re.MULTILINE)
    assert match is not None, result.stdout + result.stderr
    return int(match.group(1))


def assert_refused(result: CompletedProcess, message: str) -> None:
    """Require a failed run that names ``message`` and changed nothing on the node."""
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert message in output, output
    assert node_changed(result) == 0, output


def assert_accepted(result: CompletedProcess) -> None:
    """Require a passing run that changed nothing on the node."""
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert node_changed(result) == 0, output


@contextmanager
def arranged(host: Any, setup: str, cleanup: str) -> Iterator[None]:
    """Run the ``setup`` shell on ``host``, and ``cleanup`` even when the test fails."""
    try:
        result = host.run(setup)
        assert result.rc == 0, result.stdout + result.stderr
        yield
    finally:
        host.run(cleanup)


def write(path: str, body: str) -> str:
    """Return the shell that creates ``path`` (and its directory) holding ``body``."""
    return (
        f"mkdir -p {shlex.quote(path.rsplit('/', 1)[0])} && printf %s {shlex.quote(body)} > {path}"
    )


def head_state(head: SystemdHost, ca: str | None, token: bool) -> AbstractContextManager[None]:
    """Arrange the head container: a cluster CA and/or a server token file."""
    setup = "true"
    if ca is not None:
        setup += f" && {write(f'{K3S}/server/tls/server-ca.crt', ca)}"
    if token:
        setup += f" && {write(f'{K3S}/server/token', TOKEN)}"
    return arranged(head.host, setup, "rm -rf /var/lib/rancher")


def test_rejects_unsupported_os(systemd_host: SystemdHost) -> None:
    """Verify preflight rejects an unsupported release before any change.

    Validates: the supported-host assert gates every later play.
    Code: ansible/roles/preflight/tasks/main.yml ("Verify the supported host")

    Method: override ``ansible_distribution_version`` to 24.04 and run preflight.
    """
    result = run_playbook(
        systemd_host,
        playbook_vars(),
        extra_args=("--tags", "preflight", "-e", "ansible_distribution_version=24.04"),
    )

    assert_refused(result, "Bootstrap requires Ubuntu 26.04")


@pytest.mark.parametrize(
    ("overrides", "failing"),
    [
        ({"firewall_mode": "invalid"}, "preflight : Validating arguments against arg spec 'main'"),
        (
            {"grow_root_filesystem": True},
            "preflight : Verify the root filesystem type and device",
        ),
    ],
    ids=["firewall-mode", "root-growth"],
)
def test_rejects_invalid_inputs(
    systemd_host: SystemdHost, overrides: dict[str, Any], failing: str
) -> None:
    """Verify inputs that only a host can refuse stop a direct launch before anything changes.

    Validates: the preflight ``firewall_mode`` enum and root growth on a root that is not a
        disk partition (the container overlay). Inputs the controller-side Inventory play
        refuses are covered by tests/controller/test_inventory_play.py.
    Code: ansible/roles/preflight/meta/argument_specs.yml (``firewall_mode``);
        ansible/roles/preflight/tasks/main.yml (root growth layout)

    Assertion: each invalid input exits nonzero at the expected check (the role's argument
    spec on entry, or the layout assert), and no task changed the node.
    """
    result = preflight(systemd_host, **overrides)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert re.search(
        r"TASK \[" + re.escape(failing) + r"[^\n]*\n(?:(?!TASK \[)[\s\S])*?fatal:", result.stdout
    ), output
    assert not re.search(r"^node\s+:\s+ok=\d+\s+changed=[1-9]", result.stdout, re.MULTILINE), output


def test_off_mode_refuses_an_existing_module_firewall(systemd_host: SystemdHost) -> None:
    """Verify off mode requires explicit operator cleanup of a managed firewall.

    Validates: preflight refuses an enabled module firewall.
    Code: ansible/roles/preflight/tasks/main.yml ("Refuse off mode with a module firewall")

    Assertion: the refusal names the manual commands and changes nothing.
    """
    unit = "/etc/systemd/system/k3s-bootstrap-firewall.service"
    body = "[Service]\nType=oneshot\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n"
    with arranged(
        systemd_host.host,
        f"printf %s {shlex.quote(body)} > {unit} && systemctl daemon-reload "
        "&& systemctl enable k3s-bootstrap-firewall",
        f"systemctl disable --now k3s-bootstrap-firewall; rm -f {unit}; systemctl daemon-reload",
    ):
        result = preflight(systemd_host, firewall_mode="off")

    assert_refused(
        result,
        "systemctl disable --now k3s-bootstrap-firewall; nft destroy table inet k3s_bootstrap",
    )


def test_rejects_caller_sysctl_owned_by_k3s(systemd_host: SystemdHost) -> None:
    """Verify a caller sysctl that k3s overwrites at start is refused, also in slashed form.

    Validates: FR-011 (conflict with a k3s-owned sysctl). The slashed key goes through the
        key normalisation, which leaves a dotted key unchanged, so it covers both forms.
    Code: ansible/roles/preflight/tasks/main.yml ("Reject caller sysctls that conflict with k3s")
    """
    result = preflight(systemd_host, sysctls={"net/bridge/bridge-nf-call-ip6tables": "0"})

    assert_refused(result, "conflicts with k3s")


def test_rejects_foreign_sysctl_override_of_a_k3s_owned_key(systemd_host: SystemdHost) -> None:
    """Verify a foreign sysctl file that overrides a k3s-owned key is refused and kept.

    Validates: FR-011 (someone else's persistent override).
    Code: ansible/roles/preflight/tasks/main.yml ("Reject foreign k3s sysctl overrides")
    """
    path = "/etc/sysctl.d/99-foreign.conf"
    with arranged(
        systemd_host.host,
        write(path, "net.bridge.bridge-nf-call-iptables = 0\n"),
        f"rm -f {path}",
    ):
        result = preflight(systemd_host)
        kept = systemd_host.host.file(path).exists

    assert_refused(result, "conflicts with k3s-owned sysctl")
    assert kept


@pytest.mark.parametrize(
    ("name", "refused"), [("99-foreign.conf", True), ("10-foreign.conf", False)]
)
def test_foreign_file_applied_after_ours_refuses_a_caller_sysctl(
    systemd_host: SystemdHost, name: str, refused: bool
) -> None:
    """Verify a foreign file that would replace a caller sysctl at boot is refused.

    Validates: caller sysctls persist across reboots; a file applied before ours is harmless.
    Code: ansible/roles/preflight/tasks/main.yml
        ("Reject foreign files that override caller sysctls at boot")

    Assertion: a foreign ``vm.max_map_count`` in a file named after ``90-k3s-bootstrap.conf`` is
    refused by that task with nothing changed; the same file named before ours passes.
    """
    path = f"/etc/sysctl.d/{name}"
    with arranged(systemd_host.host, write(path, "vm.max_map_count = 65530\n"), f"rm -f {path}"):
        result = preflight(systemd_host, sysctls={"vm.max_map_count": "262144"})

    if refused:
        assert_refused(result, f"{path} is applied after")
    else:
        assert_accepted(result)


@pytest.mark.parametrize(
    ("conf", "iptables_rc"), [("yes", 1), ("no", 0)], ids=["enabled", "rules-loaded"]
)
def test_rejects_ufw_in_managed_mode(
    systemd_host: SystemdHost, conf: str, iptables_rc: int
) -> None:
    """Verify an enabled ufw, or loaded ufw rules, refuse managed firewall mode.

    Validates: FR-023 (never mix with a foreign host firewall).
    Code: ansible/roles/preflight/tasks/main.yml ("Reject enabled ufw or loaded ufw rules")

    Method: stage ``ufw.conf`` and a stand-in ``iptables`` whose exit code is the loaded-chain
    probe the role reads (0 chain exists, 1 no such chain); the image has no real iptables.
    """
    script = f"#!/bin/sh\nexit {iptables_rc}\n"
    with arranged(
        systemd_host.host,
        f"{write('/etc/ufw/ufw.conf', f'ENABLED={conf}' + "\n")} "
        f"&& {write('/usr/sbin/iptables', script)} && chmod 755 /usr/sbin/iptables",
        "rm -rf /etc/ufw /usr/sbin/iptables",
    ):
        result = preflight(systemd_host)

    assert_refused(result, "ufw is enabled or its rules are loaded")


def test_accepts_installed_but_disabled_ufw(systemd_host: SystemdHost) -> None:
    """Verify ufw that is disabled with no loaded chain does not block managed mode."""
    with arranged(
        systemd_host.host,
        f"{write('/etc/ufw/ufw.conf', 'ENABLED=no' + "\n")} "
        f"&& {write('/usr/sbin/iptables', '#!/bin/sh' + "\n" + 'exit 1' + "\n")} "
        "&& chmod 755 /usr/sbin/iptables",
        "rm -rf /etc/ufw /usr/sbin/iptables",
    ):
        result = preflight(systemd_host)

    assert_accepted(result)


def test_rejects_running_firewalld(systemd_host: SystemdHost) -> None:
    """Verify a running firewalld refuses managed firewall mode.

    Validates: FR-023.
    Code: ansible/roles/preflight/tasks/main.yml ("Reject active host firewall managers")

    Method: a stand-in ``firewalld.service`` unit that runs ``sleep``, so service facts see it.
    """
    unit = "/etc/systemd/system/firewalld.service"
    body = "[Service]\nExecStart=/bin/sleep infinity\n"
    with arranged(
        systemd_host.host,
        f"{write(unit, body)} && systemctl daemon-reload && systemctl start firewalld",
        f"systemctl stop firewalld; rm -f {unit}; systemctl daemon-reload",
    ):
        result = preflight(systemd_host)

    assert_refused(result, "firewalld is active")


@pytest.mark.parametrize(
    ("path", "setting", "data_dir"),
    [
        ("k3s.service", "--data-dir=/var/lib/k3s-legacy", ""),
        ("k3s.service", "-d /var/lib/k3s-legacy", ""),
        ("k3s.service", "K3S_DATA_DIR=/var/lib/k3s-legacy", ""),
        ("k3s.service", "--datastore-endpoint=sqlite:///var/lib/legacy.db", ""),
        ("k3s.service", "K3S_DATASTORE_ENDPOINT=sqlite:///var/lib/legacy.db", ""),
        ("k3s-legacy.service.env", "K3S_DATA_DIR=/var/lib/k3s-legacy", ""),
        ("k3s-agent.service", "", ""),
        ("k3s.service", "", f"{K3S}/server"),
        ("/usr/local/bin/k3s", "", ""),
    ],
)
def test_rejects_unknown_layout_install(
    systemd_host: SystemdHost, head_host: SystemdHost, path: str, setting: str, data_dir: str
) -> None:
    """Verify preflight refuses an existing k3s whose data layout it cannot manage.

    Validates: layout overrides in units/environment files and installs without a known
    datastore or agent directory classify as unknown, even if an empty server dir exists.
    Unit and environment file bodies are searched together, so each setting form is tried
    in a unit and one environment file proves the ``k3s*.service.env`` glob.
    Code: ansible/roles/preflight/tasks/main.yml ("Refuse an unclassifiable existing k3s
        installation")

    Assertion: nonzero exit with the unknown-layout message, nothing changed, witnesses intact.
    Proceeding would replace a unit whose datastore this module never found.
    """
    unit = path if path.startswith("/") else f"/etc/systemd/system/{path}"
    if setting:
        # Known default-path evidence makes the override itself cause the refusal.
        data_dir = f"{K3S}/server/db/etcd"
    body = (
        "[Unit]\nDescription=legacy k3s\n[Service]\n"
        f"ExecStart=/usr/local/bin/k3s server {setting}\n"
    )
    setup = write(unit, body) + (f" && mkdir -p {data_dir}" if data_dir else "")
    host = systemd_host.host
    agent = path == "k3s-agent.service"
    with arranged(host, setup, f"rm -f {unit}; rm -rf /var/lib/rancher"):
        before = host.run(f"sha256sum {unit}").stdout
        result = preflight(
            systemd_host,
            head=head_host if agent else None,
            node_type="agent" if agent else "initial",
        )
        after = host.run(f"sha256sum {unit}").stdout

    assert_refused(result, "layout is unknown")
    assert before == after


def test_retries_own_unfinished_first_install(systemd_host: SystemdHost) -> None:
    """Verify preflight lets a failed first install retry, without printing its unit token.

    Regression: a failed first converge (a join included) leaves the module's own unit
    (header line) and possibly the server directory k3s created, but no datastore, no CA and
    no completion marker; classifying that as unknown refused every retry. Decoding a legacy
    unit into a set_fact also exposed its inline token at -v.
    Code: ansible/roles/preflight/tasks/main.yml ("Recognise the module's own unfinished
        first install", unit-body collection)

    Assertion: preflight passes at -vvv with the module's own unit (carrying an inline
    ``--token``) and an empty server directory, and the token is never printed. The same
    layout under a foreign unit is refused by ``test_rejects_unknown_layout_install``.
    """
    unit = "/etc/systemd/system/k3s.service"
    sentinel = "preflight-inline-token-sentinel"
    body = (
        "# Managed by k3s-bootstrap\n[Unit]\nDescription=k3s\n[Service]\n"
        f"ExecStart=/usr/local/bin/k3s server --token {sentinel}\n"
    )
    with arranged(
        systemd_host.host,
        f"rm -f {MARKER} && {write(unit, body)} && : > {unit}.env && mkdir -p {K3S}/server",
        f"rm -f {unit} {unit}.env; rm -rf /var/lib/rancher",
    ):
        result = run_playbook(
            systemd_host, playbook_vars(), extra_args=("--tags", "preflight", "-vvv")
        )

    assert sentinel not in result.stdout + result.stderr
    assert_accepted(result)


OWN_UNIT_BODY = (
    "# Managed by k3s-bootstrap\n[Unit]\nDescription=k3s\n"
    "[Service]\nExecStart=/usr/local/bin/k3s server\n"
)


@pytest.mark.parametrize(
    ("path", "body", "refused"),
    [
        ("/etc/rancher/k3s/config.yaml", "data-dir: /srv/k3s\n", True),
        ("/etc/rancher/k3s/config.yaml", "d: /srv/k3s\n", True),
        ("/etc/rancher/k3s/config.yaml", '"data-dir+": /srv/k3s\n', True),
        (
            "/etc/rancher/k3s/config.yaml.d/10-data.yaml",
            "datastore-endpoint: sqlite:///x.db\n",
            True,
        ),
        (
            "/etc/rancher/k3s/config.yaml",
            "disable: [traefik]\ntls-san:\n  - data-dir\nflannel-backend: wireguard-native\n",
            False,
        ),
    ],
    ids=["data-dir", "short-d", "append", "datastore-in-config.d", "lookalikes"],
)
def test_data_layout_keys_in_k3s_config_files(
    systemd_host: SystemdHost, path: str, body: str, refused: bool
) -> None:
    """Verify a data-dir or datastore-endpoint key in a k3s config file is an unmanaged layout.

    Regression: with the module's own unit, no marker and both default dirs absent, a live
    install whose data lives elsewhere (set in config.yaml or config.yaml.d) classified as
    ``none`` and was treated as a first install, so it could be rebooted.
    Reference: D6
    Code: ansible/roles/preflight/tasks/main.yml ("Find data-dir or datastore-endpoint keys in
        the k3s config files")

    Assertion: the key forms (plain, short, append, config.yaml.d) are refused before any
    change; similar-looking content (``disable``, a ``data-dir`` list item, a key ending in
    ``d`` such as ``flannel-backend``) is accepted.
    """
    unit = "/etc/systemd/system/k3s.service"
    setup = f"rm -f {MARKER} && {write(unit, OWN_UNIT_BODY)} && {write(path, body)}"
    with arranged(systemd_host.host, setup, f"rm -rf {unit} /etc/rancher/k3s"):
        result = preflight(systemd_host)

    if refused:
        assert_refused(result, "layout is unknown")
    else:
        assert_accepted(result)


def test_running_service_without_data_is_refused_or_never_rebooted(
    systemd_host: SystemdHost,
) -> None:
    """Verify an active k3s service with no marker and no known datastore is never rebooted.

    Regression: such a node classified as a first install, and the host role could reboot it.
    The k3s-agent unit takes the same path: the live probe lists both unit names together and
    neither the classification nor the reboot gate reads the unit name.
    Reference: D6, D8-D13 review
    Code: ansible/roles/preflight/tasks/main.yml ("Classify the existing k3s installation",
        "Decide whether the host is untouched by k3s"); ansible/roles/host/tasks/main.yml

    Assertion: a foreign running unit is refused as an unknown layout; the module's own running
    unit is retried as an unfinished first install, but with a host reboot request present the
    host role does not request the reboot. A rebooted live node would drop a cluster member.

    Method:
    1. Arrange: a unit (foreign, then the module's own) running ``sleep``, no data dirs, and
       ``/run/reboot-required``.
    2. Act: preflight for the foreign unit; preflight, access and host for the own unit.
    3. Assert: the refusal, then success with the reboot request task skipped.
    """
    unit = "/etc/systemd/system/k3s.service"
    own = OWN_UNIT_BODY.replace("/usr/local/bin/k3s server", "/bin/sleep infinity")
    foreign = own.replace("# Managed by k3s-bootstrap\n", "")
    key = systemd_host.key_path.with_suffix(".pub").read_text().strip()
    host = systemd_host.host
    with arranged(
        host,
        f"rm -f {MARKER} && {write(unit, foreign)} && systemctl daemon-reload"
        " && systemctl start k3s && touch /run/reboot-required",
        f"systemctl stop k3s; rm -f {unit} /run/reboot-required; systemctl daemon-reload",
    ):
        refused = preflight(systemd_host)
        assert host.run(f"{write(unit, own)} && systemctl daemon-reload").rc == 0
        retried = run_playbook(
            systemd_host,
            playbook_vars(ssh_user="preflightlive", authorized_keys=[key]),
            extra_args=("--tags", "preflight,access,host"),
        )

    assert_refused(refused, "layout is unknown")
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert re.search(
        r"TASK \[host : Request a reboot before k3s is installed\][^\n]*\n"
        r"(?:(?!TASK \[)[\s\S])*?skipping:",
        retried.stdout,
    ), retried.stdout


def test_managed_agent_without_marker_keeps_its_cluster_identity(
    systemd_host: SystemdHost, head_host: SystemdHost
) -> None:
    """Existing agent CA data must override the managed-unit first-install shortcut.

    Regression: a managed agent unit without a marker skipped the head identity check.
    Code: ansible/roles/preflight/tasks/main.yml (unfinished first-install recognition)
    Assertion: the different head CA is refused before any change.
    """
    unit = "/etc/systemd/system/k3s-agent.service"
    body = "# Managed by k3s-bootstrap\n[Service]\nExecStart=/usr/local/bin/k3s agent\n"
    setup = f"rm -f {MARKER} && {write(unit, body)}"
    setup += f" && {write(f'{K3S}/agent/server-ca.crt', CA_B)}"
    with (
        head_state(head_host, CA_A, True),
        arranged(systemd_host.host, setup, f"rm -f {unit}; rm -rf /var/lib/rancher"),
    ):
        result = preflight(systemd_host, head=head_host, node_type="agent")

    assert_refused(result, "belongs to a different cluster")


@pytest.mark.parametrize(
    ("node_type", "units", "data_dir"),
    [
        ("agent", ["k3s"], "agent"),
        ("server", ["k3s-agent"], "server/db/etcd"),
        ("server", [], "agent"),
    ],
    ids=["agent-over-server-unit", "server-over-agent-unit", "server-over-orphan-agent-data"],
)
def test_rejects_server_agent_collision(
    systemd_host: SystemdHost,
    head_host: SystemdHost,
    node_type: str,
    units: list[str],
    data_dir: str,
) -> None:
    """Refuse a role collision even when datastore or unit evidence disagrees.

    Regression: trusting an agent unit before server data, or ignoring an orphan agent
    directory, allowed the opposite role to be installed alongside an existing installation.
    Code: ansible/roles/preflight/tasks/main.yml (role/datastore classification)
    Assertion: refusal before any change, unchanged units. ``initial`` and ``server`` share
    the ``node_type != 'agent'`` unit assert, so one case per assert and one for the class
    check on agent data without a unit.
    """
    paths = [f"/etc/systemd/system/{unit}.service" for unit in units]
    with arranged(
        systemd_host.host,
        f"mkdir -p {K3S}/{data_dir} && touch {' '.join(paths) or '/dev/null'}",
        f"rm -f {' '.join(paths)}; rm -rf /var/lib/rancher",
    ):
        result = preflight(
            systemd_host, head=None if node_type == "initial" else head_host, node_type=node_type
        )

    assert_refused(result, "would change its role")


def test_refuses_a_sqlite_datastore(systemd_host: SystemdHost) -> None:
    """Verify an existing SQLite datastore is refused, not converted or adopted.

    Validates: CON-005 (etcd only), FR-003 (refusal before any change).
    Code: ansible/roles/preflight/tasks/main.yml ("Refuse an existing SQLite datastore")

    Assertion: the message names SQLite and OOS-1, nothing changed, the datastore file stays.
    """
    db = f"{K3S}/server/db/state.db"
    with arranged(
        systemd_host.host, f"mkdir -p {K3S}/server/db && touch {db}", "rm -rf /var/lib/rancher"
    ):
        result = preflight(systemd_host)
        kept = systemd_host.host.file(db).exists

    assert_refused(result, "SQLite")
    assert "OOS-1" in result.stdout
    assert kept


def test_refuses_a_node_type_change_recorded_in_the_marker(systemd_host: SystemdHost) -> None:
    """Verify the marker's recorded node type must equal the requested one.

    Validates: FR-003 (etcd cannot tell initial from server; the marker does).
    Code: ansible/roles/preflight/tasks/main.yml ("Reject a node type change recorded in the
        marker")

    Assertion: a marker value that differs from the requested node type is refused. The
    check is one equality, so one pair needs no head.
    """
    with arranged(systemd_host.host, write(MARKER, "server\n"), f"rm -f {MARKER}"):
        result = preflight(systemd_host)

    assert_refused(result, "records node type 'server'")


def test_accepts_the_marker_of_the_same_node_type(systemd_host: SystemdHost) -> None:
    """Verify a marker equal to the requested node type does not block a re-run."""
    with arranged(systemd_host.host, write(MARKER, "initial\n"), f"rm -f {MARKER}"):
        result = preflight(systemd_host)

    assert_accepted(result)


@pytest.mark.parametrize(
    ("installed", "message"),
    [("v1.40.0+k3s1", "would downgrade"), ("not a version", "Cannot parse")],
    ids=["downgrade", "unparsable"],
)
def test_rejects_downgrade_and_unreadable_version(
    systemd_host: SystemdHost, installed: str, message: str
) -> None:
    """Verify an older pin or an unreadable installed version is refused before any change.

    Validates: FR-003 (downgrade is refused, never decided blind).
    Code: ansible/roles/preflight/tasks/main.yml ("Reject a k3s downgrade")

    Method: an etcd layout (a known class) with a stand-in ``k3s`` that prints ``installed``.
    """
    script = f"#!/bin/sh\necho 'k3s version {installed} (abc)'\n"
    with arranged(
        systemd_host.host,
        f"mkdir -p {K3S}/server/db/etcd && {write('/usr/local/bin/k3s', script)} "
        "&& chmod 755 /usr/local/bin/k3s",
        "rm -rf /var/lib/rancher /usr/local/bin/k3s",
    ):
        result = preflight(systemd_host)

    assert_refused(result, message)


def test_head_must_be_reachable(systemd_host: SystemdHost) -> None:
    """Verify an unreachable head fails the run before any change.

    Validates: FR-034 (the head must be reachable).
    Code: ansible/roles/preflight/tasks/head.yml ("Require a reachable head")
    """
    result = run_playbook(
        systemd_host,
        playbook_vars(node_type="server", head=DEAD_HEAD),
        extra_args=("--tags", "preflight"),
    )

    assert_refused(result, "is unreachable over SSH")


@pytest.mark.parametrize(
    ("ca", "token"),
    [(CA_A, False), (None, True)],
    ids=["no-token-file", "no-ca"],
)
def test_head_must_be_an_initialised_server(
    systemd_host: SystemdHost, head_host: SystemdHost, ca: str | None, token: bool
) -> None:
    """Verify a head without the cluster CA or without the server token file is refused.

    Validates: FR-034 (the head is initialised as a server; an agent has neither file).
    Code: ansible/roles/preflight/tasks/head.yml ("Require an initialised k3s server as the head")

    Assertion: the refusal names the missing initialisation, changes nothing and never prints
    the token the head holds. A head with neither file fails both conditions of the same
    assert, so each condition is tried alone.
    """
    with head_state(head_host, ca, token):
        result = preflight(systemd_host, head=head_host, node_type="agent")

    assert_refused(result, "is not an initialised k3s server")
    assert "secret-token-marker" not in result.stdout + result.stderr


def test_first_join_checks_only_the_head(systemd_host: SystemdHost, head_host: SystemdHost) -> None:
    """Verify a bare agent passes against a valid head (the CA is compared only when installed).

    Validates: FR-034. The head is the other container: the CA exists on it alone, so the
        run fails if the head resolved to the node. A server differs only in the path of
        the local CA, which a first join does not have.
    Code: ansible/roles/preflight/tasks/head.yml
    """
    with head_state(head_host, CA_A, True):
        result = preflight(systemd_host, head=head_host, node_type="agent")

    assert_accepted(result)
    assert "secret-token-marker" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("node_type", "local_ca", "refusal"),
    [
        ("server", CA_A, None),
        ("server", CA_B, "belongs to a different cluster"),
        ("agent", CA_B, "belongs to a different cluster"),
        ("server", None, "Cannot verify the cluster identity"),
    ],
    ids=["match-server", "mismatch-server", "mismatch-agent", "missing-server"],
)
def test_installed_node_ca_is_compared_with_the_head(
    systemd_host: SystemdHost,
    head_host: SystemdHost,
    node_type: str,
    local_ca: str | None,
    refusal: str | None,
) -> None:
    """Verify an installed node must hold the same cluster CA as its head.

    Validates: FR-034 (identity through the head), replacing the unauthenticated /cacerts probe.
    Code: ansible/roles/preflight/tasks/head.yml ("Reject a head from a different cluster")

    Assertion: equal CA passes; a different CA and a missing local CA are refused (fail closed);
    every case leaves the node untouched. The server cases read the CA under ``server/tls``
    and the agent case the one under ``agent/``, so both paths are exercised.
    """
    # A server holds the CA under server/tls, an agent under agent/ (its own data directory).
    ca_path, layout = (
        (f"{K3S}/server/tls/server-ca.crt", f"{K3S}/server/db/etcd")
        if node_type == "server"
        else (f"{K3S}/agent/server-ca.crt", f"{K3S}/agent")
    )
    setup = f"mkdir -p {layout}" + (f" && {write(ca_path, local_ca)}" if local_ca else "")
    with (
        head_state(head_host, CA_A, True),
        arranged(systemd_host.host, setup, "rm -rf /var/lib/rancher"),
    ):
        result = preflight(systemd_host, head=head_host, node_type=node_type)

    if refusal is None:
        assert_accepted(result)
    else:
        assert_refused(result, refusal)
