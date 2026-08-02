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


def _check_bindable(host: str, port: int) -> None:
    """Fail fast with a useful message if we cannot bind.

    Checked up front rather than caught later: uvicorn logs a one-line errno
    and exits 0, so a systemd unit would restart-loop forever with nothing in
    the journal explaining why. This also avoids starting go2rtc and the
    recorder fleet only to tear them straight back down.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
    except PermissionError:
        limit = _unprivileged_port_start()
        raise SystemExit(PRIVILEGED_PORT_HELP.format(port=port, limit=limit))
    except OSError as exc:
        raise SystemExit(f"\nCannot bind {host}:{port} — {exc}\n")
    finally:
        probe.close()


def main() -> None:
    cfg = config_module.load()
    _check_bindable(cfg.server.host, cfg.server.port)
    uvicorn.run(
        "nvr.main:app",
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
        access_log=False,
        # Recording state lives in this process; a reload or a second worker
        # would mean two ffmpeg fleets writing the same segment paths.
        workers=1,
    )


if __name__ == "__main__":
    main()
