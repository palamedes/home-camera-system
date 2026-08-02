# Sentry

A self-hosted camera system for a home server. Finds cameras on your network,
streams them live in a browser, and keeps a rolling buffer of everything they
saw so you can scrub back in time.

Runs entirely on your LAN. No cloud, no account, no subscription.

## What it does

- **Dashboard** — the landing page: camera status, storage, retention, and
  system health at a glance. Built as a card grid so new panels can be dropped
  in without touching layout.
- **Discovery** — sweeps the local subnet for ONVIF and RTSP devices, and
  fingerprints vendors (Reolink's native API, ONVIF, MAC OUI) so a camera shows
  up as "Reolink FE-P" rather than "something on port 554".
- **Live view** — WebRTC with sub-second latency, no transcoding. Falls back to
  MJPEG on browsers or codecs that refuse it.
- **Continuous recording** — one-minute MP4 segments written straight from the
  camera's own stream. No re-encoding, so it costs almost no CPU.
- **Timeline scrubbing** — click any point in the last N days and play from
  there. Gaps in coverage are drawn as gaps.
- **Ring-buffer retention** — oldest footage is pruned automatically against a
  size budget, an age limit, and a hard free-space floor.
- **Clip export** — download any window as a normal MP4.

## Requirements

- Linux with `ffmpeg` and `ffprobe` (6.0+)
- Python 3.11+
- An Intel iGPU is strongly recommended — QuickSync makes H.265 playback
  effectively free. Without it, playback of H.265 footage falls back to
  software encoding.

## Install

```bash
git clone git@github.com:palamedes/home-camera-system.git ~/Cameras
cd ~/Cameras
./scripts/setup.sh

# Port 80 is privileged. Allow it for unprivileged users, once:
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
echo 'net.ipv4.ip_unprivileged_port_start=80' | \
  sudo tee /etc/sysctl.d/50-unprivileged-ports.conf

systemctl --user enable --now sentry-nvr
sudo loginctl enable-linger "$USER"    # keeps it running when logged out
```

Open `http://<your-host>` and create the admin account on first visit.

### Firewall

If a firewall is running (`ufw status`), two ports must be open to the LAN:

```bash
sudo ufw allow 80/tcp    comment 'Sentry NVR web'
sudo ufw allow 8555      comment 'Sentry NVR WebRTC'   # both TCP and UDP
```

8555 is easy to overlook. WebRTC media does not travel over the HTTP
connection — the browser opens a separate flow straight to go2rtc, mostly over
UDP. Leave it closed and the page loads, the stream negotiates, and then you
get a black player with no error, which looks like a camera fault rather than
a firewall one.

Do **not** open 8554 (RTSP) or 1984 (go2rtc's API). Both bind to loopback by
design: the API can rewrite go2rtc's own configuration and add arbitrary
stream sources, so it is reachable only through this app's authenticated,
allowlisted proxy.

Symptom worth recognising: ufw's default policy is DROP, not REJECT, so a
blocked port makes the browser hang until it times out rather than failing
immediately. A page that spins forever while `curl` on the box itself answers
instantly is a firewall, not the app.

### About that port

Serving on 80 means no port to remember, but a process cannot bind it as an
ordinary user. The sysctl above lets *any* user process bind ports 80 and up,
which is fine on a single-user box.

If you would rather scope the privilege to just this service, install it as a
system unit instead of a user one — add `User=<you>`, `Group=<you>`, and
`AmbientCapabilities=CAP_NET_BIND_SERVICE` to
`systemd/sentry-nvr.service`, drop it in `/etc/systemd/system/`, and replace
`%h` with your home directory. That also starts it at boot without needing
`enable-linger`.

Either way, setting `server.port: 8080` in `config/config.yaml` avoids the
question entirely.

## Forgot your password

Recovery needs shell access to the box — there is no email reset, because
there is no email, and a LAN appliance that can be recovered remotely can be
taken over remotely.

```bash
cd ~/Cameras
.venv/bin/python -m nvr.admin reset-password
```

It prompts for the new password (never taken as an argument, so it stays out
of shell history and out of `/proc` where other users could read it) and signs
out every existing session, on the assumption that a password being reset is a
password no longer trusted.

Other commands:

```bash
.venv/bin/python -m nvr.admin list-users     # accounts and active sessions
.venv/bin/python -m nvr.admin add-user       # a second account
.venv/bin/python -m nvr.admin delete-user bob
```

Deleting the last remaining account is refused — that would lock you out of
the UI entirely.

**Locked out of a camera instead?** That is the camera's own password, not
Sentry's, and Sentry cannot recover it. Modern Reolink firmware ships with no
default password and offers no reset path, so the only way back in is the
physical reset button (hold ~10s), which returns the camera to factory
settings and clears its configuration. Once it is set up again, update the
stored credentials in Sentry under Settings.

## Configuration

System settings live in `config/config.yaml` (copied from
`config.example.yaml` on first setup). Cameras are **not** configured there —
they are added through the UI and stored in `data/nvr.db`, because credentials
change and the UI has to be able to write them.

The settings you are most likely to touch:

```yaml
storage:
  max_usage: 80%       # share of the disk recordings may occupy
  max_age_days: 7      # hard age limit, whichever hits first
  segment_seconds: 60
```

## How it fits together

```
  camera ──RTSP──> go2rtc ──┬──RTSP(loopback)──> ffmpeg ──> 1-min MP4 segments
                            │                                      │
                            └──WebRTC/MJPEG──> browser         SQLite index
                                    ▲                               │
                                    │                               ▼
                              this app (auth, proxy, UI) <── timeline + playback
```

`go2rtc` maintains the camera connections and re-serves them as something a
browser can play. It binds to loopback only — every request for live video is
proxied through this app, so streams sit behind the same login as everything
else, and only a small allowlist of its endpoints is reachable.

Recording pulls from go2rtc's local RTSP rather than opening a second
connection to the camera, which keeps one connection per camera regardless of
how many people are watching.

## Design notes

**Segments are stored as the camera sent them.** Re-encoding 24/7 would pin the
CPU for no benefit. The cost is that H.265 footage needs converting at playback
time, which is what the iGPU is for.

**Segment timestamps come from file mtime minus duration**, not from the
filename. Filenames are local time, which is ambiguous for one hour every
autumn when clocks go back; mtime is an epoch and never is.

**Playback renders windows, not files.** You ask for "five minutes starting
here" and ffmpeg stitches whatever segments cover it. Seeking is a new request
rather than a seek through hours of video.

**Auth is default-deny.** Routes are protected unless explicitly listed as
public, so a new endpoint is private by mistake rather than exposed by mistake.

## Security

This is built for a trusted LAN. Before exposing it to the internet:

- Set `server.secure_cookies: true` and put HTTPS in front of it.
- Prefer Tailscale or a VPN over port-forwarding. Internet-exposed NVRs are a
  well-known target class.
- Camera credentials are stored in plaintext in `data/nvr.db` (mode 0600), as
  they must be — the recorder has to reconnect unattended. Anyone with root on
  this box can read them.
- TLS verification is disabled when talking to cameras, which ship self-signed
  certificates. That is acceptable on a LAN and not otherwise.

## Layout

```
nvr/
  main.py        routes and wiring
  auth.py        sessions, password hashing, default-deny middleware
  db.py          SQLite: users, cameras, segment index
  discovery.py   subnet sweep + vendor fingerprinting
  onvif.py       minimal ONVIF SOAP client
  reolink.py     Reolink HTTP API
  streams.py     go2rtc supervision and config generation
  recorder.py    ffmpeg segment recording + indexing
  retention.py   ring-buffer pruning
  playback.py    timeline coverage, window rendering, clip export
  proxy.py       authenticated reverse proxy to go2rtc
```

## Licence

MIT. Bundles [go2rtc](https://github.com/AlexxIT/go2rtc) (MIT) as a binary,
fetched at setup time.
