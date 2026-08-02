"""Run the server using host/port from config.yaml.

    python -m nvr

Exists so the systemd unit does not hardcode a port that config.yaml also
claims to control — one place to change it, not two.
"""

from __future__ import annotations

import socket

import uvicorn

from . import config as config_module


PRIVILEGED_PORT_HELP = """
Cannot bind port {port}: ports below {limit} are privileged.

Grant unprivileged access once, and it survives reboots:

    sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
    echo 'net.ipv4.ip_unprivileged_port_start=80' | \\
      sudo tee /etc/sysctl.d/50-unprivileged-ports.conf

Or set a high port in config/config.yaml (server.port: 8080).
"""


def _unprivileged_port_start() -> int:
    try:
        with open("/proc/sys/net/ipv4/ip_unprivileged_port_start") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return 1024


DUAL_STACK_HOSTS = {"::", "*", "dual", ""}


def _make_socket(host: str, port: int) -> socket.socket:
    """Bind a listening socket, dual-stack where asked for.

    uvicorn cannot do this itself. asyncio sets IPV6_V6ONLY unconditionally on
    every IPv6 socket it opens, so handing it "::" yields an IPv6-only server
    and IPv4 clients get connection-refused.

    That matters because avahi publishes both an A and an AAAA record for
    <host>.local. Which one a client picks is up to its resolver, so a
    single-family listener works for some machines on the LAN and not others —
    and the ones it fails for see a name that resolves but never connects.
    Creating the socket ourselves with IPV6_V6ONLY off serves both families
    from one listener.
    """
    if host in DUAL_STACK_HOSTS:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            # Kernel refuses mapped addresses; fall back to IPv4, which every
            # device on a home LAN can reach.
            sock.close()
            return _make_socket("0.0.0.0", port)
        bind_host = "::"
    else:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_host = host

    try:
        sock.bind((bind_host, port))
    except PermissionError:
        sock.close()
        raise SystemExit(
            PRIVILEGED_PORT_HELP.format(port=port, limit=_unprivileged_port_start())
        )
    except OSError as exc:
        sock.close()
        raise SystemExit(f"\nCannot bind {bind_host}:{port} — {exc}\n")

    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


def main() -> None:
    cfg = config_module.load()
    sock = _make_socket(cfg.server.host, cfg.server.port)

    server = uvicorn.Server(
        uvicorn.Config(
            "nvr.main:app",
            log_level="info",
            access_log=False,
            # Recording state lives in this process; a reload or a second
            # worker would mean two ffmpeg fleets writing the same paths.
            workers=1,
        )
    )
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
