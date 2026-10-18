FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

# Test fixture image: mirrors the module's supported host (Ubuntu 26.04
# LTS) with systemd as PID 1, an SSH server, sudo and python3.
# nftables lets the existing-firewall test reach that refusal, not the missing-nft guard.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        systemd systemd-sysv openssh-server sudo python3 kmod nftables \
    && rm -rf /var/lib/apt/lists/*

# Start sshd directly on port 22, not via socket activation.
RUN systemctl enable ssh.service \
    && (systemctl disable ssh.socket || true) \
    && mkdir -p /run/sshd

EXPOSE 22

STOPSIGNAL SIGRTMIN+3

ENTRYPOINT ["/sbin/init"]
