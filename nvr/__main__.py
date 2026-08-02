"""Run the server using host/port from config.yaml.

    python -m nvr

Exists so the systemd unit does not hardcode a port that config.yaml also
claims to control — one place to change it, not two.
"""

from __future__ import annotations

import uvicorn

from . import config as config_module


def main() -> None:
    cfg = config_module.load()
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
