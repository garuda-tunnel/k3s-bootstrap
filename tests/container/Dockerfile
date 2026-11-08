ARG BASE_IMAGE=ubuntu:26.04
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive

# Test fixture image: mirrors a module-supported host (Ubuntu 26.04 or
# Debian 13; both apt/systemd distros with the same package names) with
# systemd as PID 1, an SSH server (`ssh` on both), sudo and python3.
# nftables lets the existing-firewall test reach that refusal, not the missing-nft guard.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        systemd systemd-sysv openssh-server sudo python3 kmod nftables apparmor \
    && rm -rf /var/lib/apt/lists/*

# Parser tests must never load policy into the privileged fixture's shared kernel.
# Start sshd directly on port 22, not via socket activation.
RUN systemctl mask apparmor.service \
    && systemctl enable ssh.service \
    && (systemctl disable ssh.socket || true) \
    && mkdir -p /run/sshd

EXPOSE 22

STOPSIGNAL SIGRTMIN+3

ENTRYPOINT ["/sbin/init"]
