"""Fixtures for the opt-in Vagrant module integration tier.

Stands come up in two bounded stages — provision (a Vagrant-managed
libvirt VM from the template under tests/vm/vagrant/, one directory per
stand) and bootstrap (one `tofu apply` of the whole module) — each teed
live to its own numbered log under /tmp/k3sb-vm-logs so a broken stand
is visible within minutes. Role behaviour is intentionally covered by Molecule;
only module-level contracts stay here. Run with ``K3SB_VM_TESTS=1 pytest tests/vm``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

# An outer `timeout` sends SIGTERM; raising from the handler unwinds the
# Python stack so fixture `finally:` blocks (stand teardown) still run
# before the outer `timeout -k` hard-kills the process. The flag tells
# teardown to spend its limited grace on the VM only.
INTERRUPTED = False


def interrupted() -> bool:
    """Whether a SIGTERM/KeyboardInterrupt is unwinding the run right now."""
    return INTERRUPTED or isinstance(sys.exc_info()[1], KeyboardInterrupt)


def _on_sigterm(*_: Any) -> None:
    """Turn SIGTERM into KeyboardInterrupt so fixture teardown still runs.

    Sets the INTERRUPTED flag first so teardown spends its limited grace on the VM only.
    """
    # The signal handler has no other channel to the module-level flag.
    global INTERRUPTED  # pylint: disable=global-statement
    INTERRUPTED = True
    raise KeyboardInterrupt


try:
    signal.signal(signal.SIGTERM, _on_sigterm)
except ValueError:  # not the main thread during collection
    pass

FIXTURE_DIR = Path(__file__).parent / "fixture"
VAGRANT_TEMPLATE = Path(__file__).parent / "vagrant" / "Vagrantfile"
LOG_ROOT = Path("/tmp/k3sb-vm-logs")
# Stands sit on the shared vagrant-libvirt management network.
MGMT_NETWORK = "vagrant-libvirt"
# Stage bounds; no subprocess call exceeds its bound.
PROVISION_SECONDS = 600
BOOTSTRAP_SECONDS = 600
DESTROY_SECONDS = 600
SHORT_TIMEOUT = 300
# Teardown budget after an interrupt, which must fit the outer
# `timeout -k 180` grace for the two-stand recovery test: reaping the
# in-flight child group (REAP_SECONDS to SIGTERM + KILL_WAIT_SECONDS to
# SIGKILL = 25 s, also when the interrupt lands inside a `vagrant
# destroy`, which is then retried once) + 2 stands x (45 s + 10 s
# kill-after vagrant destroy; the bookkeeping tofu destroy is skipped)
# = 135 s.
REAP_SECONDS = 15
KILL_WAIT_SECONDS = 10
INTERRUPTED_VM_DESTROY_SECONDS = 45
INTERRUPTED_KILL_AFTER = 10
# Single k3s version pin shared with the molecule scenarios; the module
# itself has no default pin.
K3S_VERSION = (Path(__file__).resolve().parents[1] / "k3s-version.txt").read_text().strip()
# Host paths asserted on by the shared k3s helpers.
CONFIG_PATH = "/etc/rancher/k3s/config.yaml"
DB_PATH = "/var/lib/rancher/k3s/server/db"
# The task that writes an agent's bootstrap token, printed only after the
# head minted it: killing the apply here leaves a minted token behind with
# no run left to delete it, the controller-death case its TTL covers.
TOKEN_MINTED_MARKER = "TASK [k3s : Write the join token file]"
CONVERGE_ADDR = "module.k3s_bootstrap.terraform_data.converge"


def command(
    *args: str,
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a local command, bounded by timeout, without emitting credentials."""
    try:
        return subprocess.run(
            list(args),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args,
            returncode=124,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or "") + f"\ntimed out after {timeout}s",
        )


def killpg(proc: subprocess.Popen[str], sig: int) -> None:
    """Signal the child's whole process group (it leads its own session)."""
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def tail(path: Path, lines: int = 40) -> str:
    """Return the last lines of a log for fail-fast messages."""
    if not path.exists():
        return ""
    return "".join(path.read_text(errors="replace").splitlines(keepends=True)[-lines:])


def ssh_command(
    key_path: Path, target: str, remote: str, *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a bounded, key-only, non-interactive SSH command on ``target`` (``user@host``)."""
    return command(
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
        str(key_path),
        target,
        remote,
        timeout=timeout,
    )


def wait_for_ssh(host: str, key_path: Path, user: str = "ops", secs: int = 180) -> str | None:
    """Wait for an SSH account; returns an error message on timeout."""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        result = ssh_command(key_path, f"{user}@{host}", "true", timeout=30)
        if result.returncode == 0:
            return None
        time.sleep(3)
    return f"{user} SSH did not become available within {secs}s"


def management_cidr() -> str:
    """The management network as a CIDR: the runner and every stand VM are on it.

    The managed firewall must trust it, so the runner reaches the API and the
    joining stands reach the head. The network is vagrant-managed: it appears on
    the first `vagrant up` and disappears with the last stand, so this only runs
    after provision.
    """
    dump = command(
        "virsh", "-c", "qemu:///system", "net-dumpxml", MGMT_NETWORK, timeout=SHORT_TIMEOUT
    )
    match = re.search(r"<ip address='([0-9.]+)' netmask='([0-9.]+)'", dump.stdout)
    assert dump.returncode == 0 and match, dump.stdout + dump.stderr
    prefix = sum(bin(int(octet)).count("1") for octet in match.group(2).split("."))
    return str(ipaddress.ip_network(f"{match.group(1)}/{prefix}", strict=False))


# One record owns a single VM's whole lifecycle state.
@dataclass
class VmStand:  # pylint: disable=too-many-instance-attributes
    """One Vagrant-managed VM plus its isolated OpenTofu state."""

    work_dir: Path
    vagrant_dir: Path
    state: Path
    variables: dict[str, Any]
    env: dict[str, str]
    key_path: Path
    host_name: str = ""
    instance_name: str = ""
    log_dir: Path = field(default_factory=lambda: LOG_ROOT / "unknown")
    stage_seq: int = 0

    # --- staged, logged execution -------------------------------------

    def next_log(self, stage: str) -> Path:
        """Assign the next numbered log path for a stage."""
        self.stage_seq += 1
        return self.log_dir / f"{self.stage_seq:02d}-{stage}.log"

    def record(self, stage: str, start: float, log: Path) -> None:
        """Record a stage duration in timings.txt and on stderr."""
        elapsed = time.monotonic() - start
        with (self.log_dir / "timings.txt").open("a") as timings:
            timings.write(f"{stage}\t{elapsed:.0f}s\t{log.name}\n")
        print(f"[k3sb:{self.name_suffix}] stage {stage} done in {elapsed:.0f}s", file=sys.stderr)

    def _stream(
        self,
        args: list[str],
        log: Path,
        cwd: Path,
        *,
        stop_on: str | None = None,
        watchdog: float | None = None,
    ) -> tuple[int, str, bool]:
        """Tee a child's output live to `log`; the child never outlives this call.

        `stop_on` terminates the child on the first line containing it,
        `watchdog` SIGKILLs it after that many seconds. Whatever raises
        (SIGTERM handler, timeout, ...), the `finally` reaps the child
        and cancels the watchdog.
        """
        output: list[str] = []
        hit = False
        # Not a context manager: the child is streamed below and reaped in `finally`.
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            args,
            cwd=cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        timer = threading.Timer(watchdog, killpg, (proc, signal.SIGKILL)) if watchdog else None
        try:
            if timer:
                timer.start()
            assert proc.stdout is not None
            with log.open("w") as stream:
                for line in proc.stdout:
                    stream.write(line)
                    stream.flush()
                    output.append(line)
                    if stop_on and not hit and stop_on in line:
                        hit = True
                        print(
                            f"[k3sb:{self.name_suffix}] stop marker seen, terminating",
                            file=sys.stderr,
                        )
                        proc.terminate()
            rc = proc.wait(timeout=60)
        finally:
            if timer:
                timer.cancel()
            if proc.poll() is None:
                # Whole group: killing only the `timeout` monitor would
                # orphan tofu, vagrant and ansible.
                killpg(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=REAP_SECONDS)
                except subprocess.TimeoutExpired:
                    killpg(proc, signal.SIGKILL)
                    try:
                        proc.wait(timeout=KILL_WAIT_SECONDS)
                    except subprocess.TimeoutExpired:
                        print(
                            f"[k3sb:{self.name_suffix}] pid {proc.pid} ({args[0]}) "
                            f"survived SIGKILL; leftover processes possible, log: {log}",
                            file=sys.stderr,
                        )
            if proc.stdout:
                proc.stdout.close()
        return rc, "".join(output), hit

    def run_logged(
        self,
        stage: str,
        secs: int,
        args: list[str],
        cwd: Path | None = None,
        kill_after: int = 30,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        """Run a command bounded by `timeout`, teeing output live to its log."""
        log = self.next_log(stage)
        start = time.monotonic()
        # The coreutils wrapper guarantees the bound even when the child
        # stays silent; the line loop keeps the log live, not buffered.
        bounded = ["timeout", f"--kill-after={kill_after}", str(secs), *args]
        rc, output, _ = self._stream(bounded, log, cwd or FIXTURE_DIR)
        self.record(stage, start, log)
        return subprocess.CompletedProcess(args, rc, output, ""), log

    def stage_tofu(self, stage: str, secs: int, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a lifecycle tofu call, failing the test fast on error."""
        result, log = self.run_logged(stage, secs, ["tofu", *args])
        if result.returncode != 0:
            pytest.fail(f"stage {stage} failed after {secs}s; log: {log}\n{tail(log)}")
        return result

    # --- stand operations ---------------------------------------------

    @property
    def name_suffix(self) -> str:
        """Return the unique suffix shared by this stand's names and log directory."""
        return str(self.variables["name_suffix"])

    def tofu(self, *args: str, timeout: float = SHORT_TIMEOUT) -> subprocess.CompletedProcess[str]:
        """Run OpenTofu against this stand's ephemeral state directory."""
        return command("tofu", *args, cwd=FIXTURE_DIR, env=self.env, timeout=timeout)

    def refresh_outputs(self) -> None:
        """Read the applied module outputs used by later real-host probes."""
        result, log = self.run_logged(
            "output", SHORT_TIMEOUT, ["tofu", "output", "-json", f"-state={self.state}"]
        )
        if result.returncode != 0:
            pytest.fail(f"stage output failed after {SHORT_TIMEOUT}s; log: {log}\n{tail(log)}")
        values = json.loads(result.stdout)
        self.host_name = values["host"]["value"]
        self.instance_name = values["instance_name"]["value"]

    def run_apply(
        self, *, secs: int = BOOTSTRAP_SECONDS, **overrides: Any
    ) -> subprocess.CompletedProcess[str]:
        """Apply the module after merging explicit scenario overrides.

        Test-facing API: returns the result instead of failing, so a
        test can also bound an apply. Output is still teed live to the
        stand's numbered logs.
        """
        self.variables.update(overrides)
        variables_file = self.write_variables()
        result, _log = self.run_logged(
            "apply",
            secs,
            [
                "tofu",
                "apply",
                "-auto-approve",
                "-no-color",
                f"-state={self.state}",
                f"-var-file={variables_file}",
            ],
        )
        if result.returncode == 0:
            self.refresh_outputs()
            error = wait_for_ssh(self.host_name, self.key_path, "ops")
            if error:
                pytest.fail(error)
        return result

    def run_apply_killing_converge(
        self, *, secs: int = BOOTSTRAP_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        """Apply and kill tofu as soon as the head minted the agent's token.

        No fixed clock value lands reliably between the mint and the end of
        the run, so the kill fires on the live log marker of the task that
        writes the minted token, with `secs` as the overall bound (a watchdog
        SIGKILL).
        """
        log = self.next_log("apply")
        start = time.monotonic()
        args = [
            "tofu",
            "apply",
            "-auto-approve",
            "-no-color",
            f"-state={self.state}",
            f"-var-file={self.write_variables()}",
        ]
        # No `timeout` wrapper: it would deliver a second SIGTERM to
        # tofu's process group, and a second interrupt aborts tofu
        # without saving state.
        rc, output, seen = self._stream(
            args, log, FIXTURE_DIR, stop_on=TOKEN_MINTED_MARKER, watchdog=secs
        )
        self.record("apply-killed" if seen else "apply", start, log)
        if not seen:
            pytest.fail(
                f"apply did not reach the token mint marker within {secs}s; "
                f"log: {log}\n{tail(log)}"
            )
        return subprocess.CompletedProcess(args, rc, output, "")

    def write_variables(self) -> Path:
        """Write the stand's current inputs to the isolated OpenTofu variable file."""
        variables_file = self.work_dir / "terraform.tfvars.json"
        variables_file.write_text(json.dumps(self.variables))
        return variables_file

    def provision(self) -> None:
        """Stage 1: the Vagrant VM (up, key provisioning, DHCP lease)."""
        result, log = self.run_logged(
            "provision",
            PROVISION_SECONDS,
            ["vagrant", "up", "--no-color", "--provider=libvirt"],
            cwd=self.vagrant_dir,
        )
        if result.returncode != 0:
            pytest.fail(
                f"stage provision failed after {PROVISION_SECONDS}s; log: {log}\n{tail(log)}"
            )
        machine_id = (self.vagrant_dir / ".vagrant/machines/default/libvirt/id").read_text().strip()
        named = command(
            "virsh", "-c", "qemu:///system", "domname", machine_id, timeout=SHORT_TIMEOUT
        )
        assert named.returncode == 0, named.stdout + named.stderr
        self.instance_name = named.stdout.strip()
        self.host_name = self.wait_for_lease(machine_id)
        self.variables["host"] = self.host_name
        self.variables["host_generation"] = self.instance_name
        self.variables["api_trusted_cidrs"] = [management_cidr()]

    def wait_for_lease(self, machine_id: str, secs: int = 120) -> str:
        """Wait for the Vagrant VM DHCP lease so module SSH can target its real address."""
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            leases = command(
                "virsh",
                "-c",
                "qemu:///system",
                "domifaddr",
                machine_id,
                "--source",
                "lease",
                timeout=SHORT_TIMEOUT,
            )
            match = re.search(r"192\.168\.\d+\.\d+/\d+", leases.stdout)
            if match:
                return match.group(0).rsplit("/", 1)[0]
            time.sleep(3)
        raise pytest.fail.Exception(f"{self.instance_name}: no DHCP lease within {secs}s")

    def bootstrap(self) -> None:
        """Stage 2: the full module apply, then the module's ssh_user."""
        self.stage_tofu(
            "bootstrap",
            BOOTSTRAP_SECONDS,
            "apply",
            "-auto-approve",
            "-no-color",
            f"-state={self.state}",
            f"-var-file={self.write_variables()}",
        )
        self.refresh_outputs()
        error = wait_for_ssh(self.host_name, self.key_path, "ops")
        if error:
            pytest.fail(error)

    def destroy_vm(self, failures: list[str], after_interrupt: bool) -> None:
        """`vagrant destroy -f`, with the short budget after an interrupt."""
        result, log = self.run_logged(
            "destroy-vm",
            INTERRUPTED_VM_DESTROY_SECONDS if after_interrupt else SHORT_TIMEOUT,
            ["vagrant", "destroy", "-f", "--no-color"],
            cwd=self.vagrant_dir,
            kill_after=INTERRUPTED_KILL_AFTER if after_interrupt else 30,
        )
        if result.returncode != 0:
            failures.append(f"vagrant destroy; log: {log}")

    def destroy(self) -> None:
        """Tear the stand down; the VM is destroyed whatever happens first.

        The tofu destroy is only bookkeeping (no destroy-time
        provisioners), so after an interrupt it is skipped to keep the
        limited grace for the VM. Interruption is re-evaluated when the
        VM is destroyed: a SIGTERM during the tofu destroy or during
        `vagrant destroy` itself must not leave the long budget in force.
        """
        failures: list[str] = []
        try:
            if self.state.exists() and not interrupted():
                result, log = self.run_logged(
                    "destroy",
                    DESTROY_SECONDS,
                    [
                        "tofu",
                        "destroy",
                        "-auto-approve",
                        "-no-color",
                        f"-state={self.state}",
                        f"-var-file={self.write_variables()}",
                    ],
                )
                if result.returncode != 0:
                    failures.append(f"tofu destroy; log: {log}")
        finally:
            try:
                self.destroy_vm(failures, interrupted())
            except KeyboardInterrupt:
                # Interrupted mid-destroy: one more try on the short
                # budget (the child group was reaped), then propagate.
                self.destroy_vm(failures, True)
                raise
            finally:
                if failures:
                    print(
                        f"[k3sb:{self.name_suffix}] DESTROY FAILED ({'; '.join(failures)}); "
                        f"leftovers possible: domain {self.instance_name or '<unknown>'}",
                        file=sys.stderr,
                    )
        if failures:
            raise AssertionError(f"stand teardown failed; see {self.log_dir}")

    def ssh(
        self, remote: str, *, user: str = "ops", timeout: float = SHORT_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        """Run a bounded SSH probe; never include secret values in diagnostics."""
        return ssh_command(self.key_path, f"{user}@{self.host_name}", remote, timeout=timeout)


def announce(stand: VmStand) -> None:
    """Point a human at the live logs before the first stage runs."""
    stand.log_dir.mkdir(parents=True, exist_ok=True)
    print(f"[k3sb:{stand.name_suffix}] stand logs: {stand.log_dir}", file=sys.stderr)


# --- shared test helpers ------------------------------------------------


def probe(stand: Any, remote: str, *, user: str = "ops") -> str:
    """Assert a bounded host probe succeeds without revealing credentials."""
    result = stand.ssh(remote, user=user)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def assert_converge_interrupted(stand: Any) -> None:
    """Fail closed unless the killed apply left the converge unfinished.

    An interrupted provisioner leaves its resource tainted (or absent);
    an untainted converge means the kill landed after it had finished,
    so a retry would be a no-op and prove nothing. The `state show`
    output is never printed: it carries the module inputs.
    """
    listed = stand.tofu("state", "list", f"-state={stand.state}")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    if CONVERGE_ADDR not in listed.stdout.split():
        return
    shown = stand.tofu("state", "show", "-no-color", f"-state={stand.state}", CONVERGE_ADDR)
    assert shown.returncode == 0, shown.stderr
    if "(tainted)" not in shown.stdout:
        pytest.fail(
            "the kill landed after the converge had finished (resource not tainted): "
            "the retry would be a no-op, so the interrupted-converge contract is unproven"
        )


def snapshot(stand: Any) -> str:
    """Capture config, CA, datastore, binary, service and data markers.

    The configmap marker proves the datastore was not silently reset; the
    CA sha proves the cluster identity survived; the inode, binary mtime
    and ActiveEnterTimestamp prove nothing was reinstalled or restarted.
    """
    return probe(
        stand,
        f"sudo -n sha256sum {CONFIG_PATH} /var/lib/rancher/k3s/server/tls/server-ca.crt; "
        f"sudo -n stat -c '%i' {DB_PATH}/etcd; "
        "stat -c '%y' /usr/local/bin/k3s; "
        "systemctl show k3s -p ActiveEnterTimestamp --value; "
        "sudo -n k3s kubectl get configmap k3sb-safety-marker -o jsonpath='{.data.value}'",
    )


def new_stand(
    tmp_path_factory: pytest.TempPathFactory, key_path: Path | None = None, **overrides: Any
) -> VmStand:
    """Create an isolated stand with its own state and log directory.

    ``key_path`` shares one key between stands (a head and its joining nodes use
    one ``ssh_private_key_file``); without it the stand gets its own key.
    """
    name_suffix = secrets.token_hex(4)
    work_dir = tmp_path_factory.mktemp(f"k3sb-stand-{name_suffix}-")
    if key_path is None:
        key_path = work_dir / "id_ed25519"
        generated = command(
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path), timeout=60
        )
        assert generated.returncode == 0, generated.stderr
    public_key = key_path.with_suffix(".pub").read_text().strip()
    # The Vagrantfile lives at the stand-directory root: vagrant-libvirt
    # derives the libvirt domain from the Vagrantfile directory's name,
    # so this keeps concurrent stands' domains unique (k3sb-stand-*_default).
    vagrant_dir = work_dir
    shutil.copy(VAGRANT_TEMPLATE, work_dir / "Vagrantfile")
    stand = VmStand(
        work_dir=work_dir,
        vagrant_dir=vagrant_dir,
        state=work_dir / "terraform.tfstate",
        variables={
            "name_suffix": name_suffix,
            "host": "",
            "host_generation": "",
            "k3s_version": K3S_VERSION,
            "ssh_private_key_file": str(key_path),
            "authorized_keys": [public_key],
            **overrides,
        },
        env=os.environ
        | {
            "TF_DATA_DIR": str(work_dir / "tofu-data"),
            "TF_INPUT": "0",
            "K3SB_MACHINE_NAME": f"k3sb-{name_suffix}",
            "K3SB_PUBKEY": public_key,
        },
        key_path=key_path,
        log_dir=LOG_ROOT / name_suffix,
    )
    announce(stand)
    initialized, init_log = stand.run_logged(
        "init", SHORT_TIMEOUT, ["tofu", "init", "-backend=false", "-no-color"]
    )
    if initialized.returncode != 0:
        pytest.fail(f"stage init failed after {SHORT_TIMEOUT}s; log: {init_log}\n{tail(init_log)}")
    return stand


def _vm_tests_enabled() -> None:
    """Skip the requesting fixture unless the manual VM tier was opted into.

    Keeps `pytest tests/vm` collectable and cheap without libvirt: no stand is created
    unless K3SB_VM_TESTS=1 is set.
    """
    if os.environ.get("K3SB_VM_TESTS") != "1":
        pytest.skip("set K3SB_VM_TESTS=1 to create the manual local VM test fixture")


@pytest.fixture(scope="module")
def vm_factory(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., Any]:
    """Create isolated stands; VMs reach each other on the management network."""
    _vm_tests_enabled()

    @contextmanager
    def create(
        *, bootstrap: bool = True, key_path: Path | None = None, **overrides: Any
    ) -> Iterator[VmStand]:
        """Always tear down a stand; ``bootstrap=False`` stops after the VM is up."""
        stand = new_stand(tmp_path_factory, key_path, **overrides)
        try:
            stand.provision()
            if bootstrap:
                stand.bootstrap()
            yield stand
        finally:
            stand.destroy()

    return create
