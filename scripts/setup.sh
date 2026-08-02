#!/usr/bin/env bash
# Provision Sentry on a fresh box. Safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GO2RTC_VERSION="v1.9.14"

say() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$1"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$1" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 is required"
command -v ffmpeg  >/dev/null || die "ffmpeg is required (pacman -S ffmpeg)"
command -v ffprobe >/dev/null || die "ffprobe is required (ships with ffmpeg)"

say "Creating virtualenv"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

say "Fetching go2rtc ${GO2RTC_VERSION}"
mkdir -p bin
case "$(uname -m)" in
  x86_64)  ASSET="go2rtc_linux_amd64" ;;
  aarch64) ASSET="go2rtc_linux_arm64" ;;
  armv7l)  ASSET="go2rtc_linux_arm"   ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac
if [ ! -x bin/go2rtc ]; then
  curl -fsSL -o bin/go2rtc \
    "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/${ASSET}"
  chmod +x bin/go2rtc
fi
./bin/go2rtc --version

say "Preparing config"
mkdir -p config data recordings
[ -f config/config.yaml ] || cp config/config.example.yaml config/config.yaml

if [ -e /dev/dri/renderD128 ]; then
  say "QuickSync present — playback transcoding will use the iGPU"
else
  warn "No /dev/dri/renderD128; playback of H.265 footage will use the CPU"
fi

say "Installing systemd user service"
mkdir -p "${HOME}/.config/systemd/user"
cp systemd/sentry-nvr.service "${HOME}/.config/systemd/user/"
systemctl --user daemon-reload

cat <<'EOF'

Setup complete. To start it:

    systemctl --user enable --now sentry-nvr

So it keeps running when you are not logged in (needs sudo, once):

    sudo loginctl enable-linger "$USER"

Then open http://home.local:8080 and create your admin account.

EOF
