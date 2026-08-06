# Sentry

A self-hosted camera system for a home server. Finds cameras on your network,
streams them live in a browser, and keeps a rolling buffer of everything they
saw so you can scrub back in time.

Runs entirely on your LAN. No cloud, no account, no subscription.

## What it does

- **Dashboard** — the landing page: cameras, storage, and system health at a
  glance, as a card grid.
- **Discovery** — sweeps the local subnet for ONVIF and RTSP devices, and
  fingerprints vendors (Reolink's native API, ONVIF, MAC OUI) so a camera shows
  up as "Reolink FE-P" rather than "something on port 554". Cameras are added
  from Settings.
- **Live view** — WebRTC with sub-second latency, no transcoding. Falls back to
  MJPEG on browsers or codecs that refuse it. Optional on-demand audio (Opus,
  transcoded only while someone is listening).
- **Instant replay** — a scrubber on the live view rewinds the last few minutes
  from an in-browser buffer, right up to the live edge.
- **Continuous recording** — 60-second MP4 segments written straight from the
  camera's own stream. No re-encoding, so it costs almost no CPU.
- **History** — scrub the timeline (down to a 10-minute zoom), drag to select
  and export a clip, or Ctrl-drag to sweep the playhead.
- **Ring-buffer retention** — oldest footage pruned automatically against a size
  budget, an age limit (global or per-camera), and a hard free-space floor.
- **360 / fisheye** — auto-detected fisheye cameras can be dewarped in the
  browser (single / dual / quad / panorama, WebGL). Aim a view and save it as a
  **virtual camera** that appears like a real one on the dashboard, cameras page,
  and wall — with its own live view and dewarped history — all from the single
  360 recording (no extra storage).
- **Wall view** — every camera and virtual camera tiled edge-to-edge, chromeless.
- **Users & roles** — an admin manages accounts; viewers can watch but not
  change anything, and per-camera visibility controls what viewers see.
- **Viewer counts** — see how many people are watching (and listening to) each
  camera.

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

## Running it as a service

Sentry ships as a **systemd user service** (`systemd/sentry-nvr.service`) so it
starts at boot, restarts on crash, and takes go2rtc and the ffmpeg fleet down
with it on stop rather than orphaning them. Install it once:

```bash
cp systemd/sentry-nvr.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sentry-nvr
sudo loginctl enable-linger "$USER"   # start at boot without a login session
```

`enable-linger` is the step people miss. A *user* service normally runs only
while you are logged in; lingering tells systemd to start your user manager at
boot with no login — which is exactly what a headless, wall-mounted box needs.
Skip it and the service is "enabled" but never actually starts after a reboot.

One gotcha on some setups: the packaged unit lists
`SupplementaryGroups=render video` so ffmpeg can reach the QuickSync render
node. A *user* manager cannot grant a group your account is not already in, so
if you are not in `render` the service fails to start with `status=216/GROUP`.
Two fixes: add yourself (`sudo usermod -aG render "$USER"`, then re-login), or —
if `/dev/dri/renderD128` is already world-accessible (`crw-rw-rw-`, as it is on
many systems) — just comment that line out of your installed copy; QuickSync
still works. Installing it as a *system* unit instead (see "About that port")
sidesteps this entirely, since the system manager can grant any group.

Day to day:

```bash
systemctl --user status sentry-nvr      # is it healthy?
systemctl --user restart sentry-nvr     # clean restart (takes go2rtc + ffmpeg with it)
systemctl --user stop sentry-nvr        # stop it
journalctl --user -u sentry-nvr -f      # follow the logs live
journalctl --user -u sentry-nvr -n 200  # last 200 log lines
```

Because systemd owns the process group, `restart` cleanly reclaims port 80 —
no more hunting an orphaned process that kept holding the socket after a manual
kill.

### HTTPS (needed for the microphone / two-way Talk)

Browsers only grant microphone access in a *secure context* — HTTPS or
localhost. Over plain `http://home.local` the **Talk** button is blocked by the
browser (nothing Sentry can change). Listening to camera audio and everything
else work fine over HTTP; only pushing your mic to a camera needs HTTPS.

`home.local` is an mDNS name, so a public CA (Let's Encrypt) cannot issue a
certificate for it. The fix is a small [Caddy](https://caddyserver.com) reverse
proxy with its own local CA, added *alongside* Sentry — Sentry keeps serving
plain HTTP on `:80` unchanged, Caddy adds HTTPS on `:443`:

```bash
sudo pacman -S caddy                       # or your distro's package
cp systemd/sentry-tls.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sentry-tls
```

The proxy config is `deploy/Caddyfile`. `:443` is privileged, but the same
`ip_unprivileged_port_start=80` sysctl that lets Sentry bind `:80` covers it,
so Caddy runs as a user service too. `http://home.local` keeps working; migrate
to `https://home.local` when you're ready.

**Trust the CA once per device.** Caddy signs `home.local` with a local CA
whose root is at `~/.local/share/caddy/pki/authorities/local/root.crt`. Until a
device trusts it, `https://home.local` shows a warning and the mic still won't
work. Copy that file to each device and install it:

- **iPhone/iPad** — get the `.crt` onto the device (email it to yourself or
  AirDrop from a Mac), then Settings → General → **VPN & Device Management** →
  install the profile → **also** Settings → General → About → **Certificate
  Trust Settings** → enable full trust for "Caddy Local Authority". (Both steps
  are required — installing without enabling full trust is the usual gotcha.)
- **Mac** — double-click the `.crt`, add it to the **System** keychain, then in
  Keychain Access set it to **Always Trust**.

After that `https://home.local` shows a valid lock and Talk works.

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

## Tests

Backend tests (auth, roles, database, clips, virtual cameras, retention) run
against a throwaway SQLite database in a temp directory — they never touch your
real `data/` or start go2rtc:

```bash
pip install -r requirements-dev.txt
pytest
```

The browser-side pieces (WebGL dewarping, MediaRecorder replay/clip capture,
the timeline canvas) are verified by hand rather than automated.

## Layout

```
nvr/
  main.py        routes and wiring
  auth.py        sessions, password hashing, roles, default-deny middleware
  admin.py       CLI for account recovery (reset-password, add/delete user)
  db.py          SQLite: users, cameras, virtual cameras, segment index
  discovery.py   subnet sweep + vendor fingerprinting
  onvif.py       minimal ONVIF SOAP client
  reolink.py     Reolink HTTP API
  streams.py     go2rtc supervision and config generation
  recorder.py    ffmpeg segment recording + indexing
  retention.py   ring-buffer pruning
  playback.py    timeline coverage, window rendering, clip export
  proxy.py       authenticated reverse proxy to go2rtc
  static/
    live.js      WebRTC live view + grid tiles
    fisheye.js   WebGL fisheye dewarping + virtual cameras
    replay.js    in-browser instant-replay buffer
    timeline.js  history timeline (scrub, zoom, select, export)
tests/           pytest suite (backend: auth, roles, db, clips, retention)
```

## Licence

MIT — see [LICENSE](LICENSE). Bundles
[go2rtc](https://github.com/AlexxIT/go2rtc) (MIT) as a binary, fetched at setup
time.
