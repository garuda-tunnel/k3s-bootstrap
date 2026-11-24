"""Exercise a rendered firewall only after creating a disposable network namespace."""

import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import time
from typing import Any


def run(*args: str) -> str:
    """Run a bounded namespace command and return its output."""
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10).stdout


def checksum(data: bytes) -> int:
    """Return the IPv4 header checksum."""
    if len(data) % 2:
        data += b"\0"
    value: int = sum(struct.unpack(f"!{len(data) // 2}H", data))
    value = (value >> 16) + (value & 65535)
    value += value >> 16
    return (~value) & 65535


def counters() -> dict[str, int]:
    """Return counts for the exact current probe before and after the firewall."""
    data: Any = json.loads(run("nft", "-j", "list", "table", "inet", "probe"))
    return {
        str(obj["counter"]["name"]): int(obj["counter"]["packets"])
        for obj in data["nftables"]
        if "counter" in obj and "name" in obj["counter"]
    }


def watch(address: str, destination: str, protocol: str, source_port: int, port: int) -> None:
    """Match a single five-tuple on both sides of the production chain."""
    family = "ip6" if ":" in address else "ip"
    for chain in ("before", "after"):
        run("nft", "flush", "chain", "inet", "probe", chain)
        run(
            "nft",
            "add",
            "rule",
            "inet",
            "probe",
            chain,
            family,
            "saddr",
            address,
            family,
            "daddr",
            destination,
            protocol,
            "sport",
            str(source_port),
            protocol,
            "dport",
            str(port),
            "counter",
            "name",
            chain,
        )
        run("nft", "reset", "counter", "inet", "probe", chain)


def verdict() -> int:
    """Require injection and wait briefly for an exact matching accepted packet."""
    deadline = time.monotonic() + 0.3
    seen: dict[str, int] = {}
    while time.monotonic() < deadline:
        seen = counters()
        if seen.get("after", 0):
            break
        time.sleep(0.01)
    if not seen.get("before", 0):
        raise RuntimeError("probe packet did not reach the firewall")
    return int(seen.get("after", 0) > 0)


def sandbox(rules: str) -> None:
    """Set up and probe rules; called only in a newly created network namespace."""
    run("ip", "link", "add", "veth0", "type", "veth", "peer", "name", "veth1")
    for interface in ("veth0", "veth1", "lo"):
        run("ip", "link", "set", interface, "up")
    run("ip", "addr", "add", "198.51.100.1/24", "dev", "veth1")
    run("ip", "-6", "addr", "add", "2001:db8:ffff::1/64", "dev", "veth1", "nodad")
    run("ip", "route", "add", "default", "dev", "veth1")
    run("nft", "-f", rules)
    run("nft", "add", "table", "inet", "probe")
    for chain, priority in (("before", -10), ("after", 10)):
        run("nft", "add", "counter", "inet", "probe", chain)
        run(
            "nft",
            "add",
            "chain",
            "inet",
            "probe",
            chain,
            f"{{ type filter hook input priority {priority}; policy accept; }}",
        )
    macs = [
        bytes.fromhex(
            json.loads(run("ip", "-j", "link", "show", interface))[0]["address"].replace(":", "")
        )
        for interface in ("veth1", "veth0")
    ]
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as sock:
        sock.bind(("veth0", 0))
        for address in (
            "192.0.2.10",
            "203.0.113.10",
            "10.42.1.1",
            "198.18.0.1",
            "2001:db8:1::1",
            "2001:db8:2::1",
            "fd00:42::1",
            "2001:db8:3::1",
        ):
            ip = ipaddress.ip_address(address)
            destination = ipaddress.ip_address(
                "198.51.100.1" if ip.version == 4 else "2001:db8:ffff::1"
            )
            results: list[int] = []
            for proto, port in ((6, 2379), (6, 2380), (6, 6443), (6, 10250), (17, 8472), (6, 80)):
                transport = (
                    struct.pack("!HHHH", 12345, port, 8, 0)
                    if proto == 17
                    else struct.pack("!HHIIHHHH", 12345, port, 1, 0, 0x5002, 8192, 0, 0)
                )
                if ip.version == 4:
                    header = struct.pack(
                        "!BBHHHBBH4s4s",
                        0x45,
                        0,
                        20 + len(transport),
                        1,
                        0,
                        64,
                        proto,
                        0,
                        ip.packed,
                        destination.packed,
                    )
                    header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
                    ether = 0x0800
                else:
                    header = struct.pack(
                        "!IHBB16s16s",
                        6 << 28,
                        len(transport),
                        proto,
                        64,
                        ip.packed,
                        destination.packed,
                    )
                    ether = 0x86DD
                watch(address, str(destination), "udp" if proto == 17 else "tcp", 12345, port)
                sock.send(macs[0] + macs[1] + struct.pack("!H", ether) + header + transport)
                results.append(verdict())
            print(address, *results, flush=True)
    loopback_probes()


def loopback_probes() -> None:
    """Check exact local TCP and UDP probes on both IP families."""
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        for port in (2379, 2380, 6443, 10250, 8472):
            kind = socket.SOCK_DGRAM if port == 8472 else socket.SOCK_STREAM
            with socket.socket(family, kind) as sock:
                sock.bind((address, 0))
                watch(
                    address, address, "udp" if port == 8472 else "tcp", sock.getsockname()[1], port
                )
                if kind == socket.SOCK_DGRAM:
                    sock.sendto(b"probe", (address, port))
                else:
                    sock.settimeout(1)
                    sock.connect_ex((address, port))
                print(f"loop-{address}-{port}", verdict(), flush=True)


def main() -> None:
    """Always create a fresh network namespace before executing any network operation."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: firewall_sandbox.py RULESET|--check")
    flags = "-n" if os.geteuid() == 0 else "-Urn"
    code = (
        "import runpy,sys; m=runpy.run_path(sys.argv[1]); "
        "m['run']('nft','list','ruleset') if sys.argv[2]=='--check' else m['sandbox'](sys.argv[2])"
    )
    os.execvp("unshare", ["unshare", flags, sys.executable, "-c", code, __file__, sys.argv[1]])


if __name__ == "__main__":
    main()
