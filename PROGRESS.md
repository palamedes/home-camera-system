# Sentry NVR — Progress & Roadmap

Self-hosted network video recorder for a home LAN. No cloud. Cameras → go2rtc
(on-demand RTSP) → WebRTC/MJPEG live view + continuous `-c copy` recording →
QSV-accelerated playback. FastAPI + Jinja + vanilla JS, SQLite (WAL).

_Last updated: 2026-08-06 · `main` @ `4f37d96` (pushed to GitHub)_

---

## Architecture at a glance

| Layer | What it is |
|---|---|
| **Streaming** | go2rtc v1.9.14, loopback only (API 1984 / RTSP 8554 / WebRTC 8555); browser reaches it only via the authenticated `/go2rtc/` proxy |
| **Recording** | one ffmpeg per camera pulling loopback RTSP into 60s MP4 segments (`-c copy`); segments indexed after they close, **absolute paths** in the DB |
| **Playback** | ffmpeg concat + QSV (`/dev/dri/renderD128`) transcode, served in fixed chunks |
| **Storage** | ordered **pool of volumes** (overflow); retention prunes oldest across the pool + per-volume free-space floors |
| **Config** | `config.yaml` is a **seed/fallback**; most settings are edited in-app and stored in the `app_settings` table, which overrides the file on boot |
| **Process** | launched manually (`python -m nvr`), single uvicorn worker (recording state lives in-process) |

Key modules: `main.py` (routes/wiring), `recorder.py`, `retention.py`,
`streams.py` (go2rtc), `playback.py`, `weather.py`, `alerts.py`, `events.py`,
`scheduler.py`, `appsettings.py` (settings overlay), `storage_migrate.py`,
`config.py`, `db.py`.

---

## ✅ Done

### Core NVR (earlier work)
- Config/DB scaffolding, auth (sessions, roles: admin/viewer), user-management UI.
- Camera discovery (ONVIF + Reolink), go2rtc supervisor with API health-check.
- Continuous recorder + segment indexer; playback API with QSV transcode.
- Retention pruner (see storage below for the current model).
- Live grids with WebRTC preview tiles; fullscreen **Wall** view.
- Fisheye/360 dewarp + saved **virtual PTZ cameras** (browser dewarp), incl.
  virtual-camera history playback.
- History timeline: zoom, scrub, region-select **export**, instant-replay scrubber.
- **Saved clips** library (capture rendered playback incl. audio → box).
- Per-camera controls: spotlight, night vision (color/IR/auto), record
  resolution/stream, two-way talk, WebRTC audio.
- **Camera schedules** (record / light / night-vision by time-of-day).
- Per-camera retention override, viewer visibility, on-grid toggle,
  enable/disable, rename.
- Account dropdown with change-password.

### Weather + river-level dashboard card
- `WeatherService` caches **Open-Meteo** current conditions and the **NWS/NWPS**
  gauge `ORLN7` (Neuse River at Oriental); served via `/api/weather`, refreshed
  on a background timer (browser never hits the internet directly).
- Card shows temp, wind **merged with gusts** (`8 / 14 mph SSE`), humidity, dew
  point, precip, **pressure in inHg**, UV index, today's high/low, and river
  level + **rising/falling/steady** trend with a sparkline.
- Location settable by **place-name search** (Open-Meteo geocoding).

### Smart events + notifications
- `EventService` polls **Reolink onboard AI** (person/vehicle/animal, optional
  motion), edge-triggered (0→1) into an `events` table.
- Events render as **clickable colored markers** on the history timeline
  (click → jump to ~2s before), with a legend.
- `AlertService` records every event and **POSTs JSON to a webhook** with a
  per-(camera, kind) **cooldown**. "Send test alert" button in Settings.
- **River flood alerts**: notify when the gauge crosses a level threshold or NWS
  moves to any flood stage past normal (edge-tracked, re-arms after it drops).

### Everything editable in the app (config.yaml → seed/fallback)
- `appsettings` overlay: edits stored in `app_settings` table, replayed over
  `config.yaml` on boot (DB wins).
- **Tabbed Settings** page: Cameras · Weather · Alerts & events · Network ·
  Users · Storage. Choice remembered (URL hash + localStorage).
- **Per-field help**: a `?` icon on every setting → hover tooltip + click modal.
- Editable live: **Weather** (location/units/gauge/flood thresholds),
  **Alerts** (webhook/detect classes/cooldown/poll), **Storage limits**
  (max age, segment length), **Network** (host/port, go2rtc ports, discovery
  subnets/timeouts, playback transcode/QSV, session length, secure cookies).
- **Lock-out protection** for networking: host/port apply on restart; `__main__`
  applies DB overrides before binding and **auto-falls back to `config.yaml`**
  if a value can't bind; `SENTRY_IGNORE_DB_NETWORK=1` escape hatch.

### Multi-volume storage pool (NAS-ready)
- Recordings are an **ordered list of volumes**, each with its own cap
  (`80%` or `400G`). New footage lands on the **first available volume under
  cap** and **overflows** to the next when full (recorder rebuilds at a segment
  boundary, ~5s gap).
- **Pool-aware retention**: prune oldest across the whole pool once genuinely
  full, plus a **per-volume 5 GB free-space floor**. Indexer, camera-delete, and
  empty-dir pruning all span every volume.
- **Boot-safe**: an unmounted volume stays in the list and is simply skipped
  (availability = mounted+writable, checked at write/prune time). fstab model.
- **Volumes editor** in Settings (path · cap · live used/free · add/remove/
  reorder), with a live per-volume usage readout.
- Relocatable **clips** dir; background **migration** consolidates footage
  stranded on a de-listed drive back onto the primary volume. Existing footage
  stays playable across volumes (absolute paths).

### UI polish
- **Light-mode** toggle in the account menu (persisted, no flash-of-dark).
- Dashboard stats strip moved to the Cameras page; **Wall-view button** on the
  dashboard Cameras panel; gap between weather card and grid.
- Cache-busted static assets (`asset()` → `?v=<mtime>`).

### Testing / housekeeping
- **192 tests passing** (weather, alerts, events, settings, storage
  pool/migration, pages, retention, reachability, etc.).
- MIT `LICENSE`, `config/config.example.yaml`, `.gitignore` (secrets:
  `config.yaml`, `data/`, `.claude/` are ignored — none pushed).

---

## ⏳ To do / open threads

### Next features
- [ ] **Per-camera → volume targeting** (phase 2 of the storage pool): let each
  camera pick which volume(s) it records to / overflows through. Model:
  per-camera ordered volume preference, default = global order.
- [ ] **SD-card backfill** (`#21`): fill timeline gaps (NVR downtime) from a
  camera's local SD card. Per-brand; Reolink `Search`/`Download` API. Needs
  high-endurance microSD cards in the cameras first.
- [ ] **Hot-removable drives**: current pool assumes fstab-mounted volumes.
  True unplug/replug support would need drive-availability probing everywhere +
  retention that protects footage on absent volumes (deferred by choice).

### Operations
- [ ] **systemd service**: app is currently launched manually; restarts are a
  fragile kill-and-relaunch. A unit would make restarts clean, auto-start on
  boot, and (with `After=`/`RequiresMountsFor=`) guarantee a NAS is mounted
  before Sentry starts. **Recommended next.**
- [ ] When the NAS/USB arrives: add it to `/etc/fstab` so it remounts on boot,
  then add it as a volume in Settings → Storage.
- [ ] Buy **high-endurance microSD** cards for the cameras (cheap insurance;
  prerequisite for backfill).

### Needs real-device verification (built, not yet confirmed on hardware)
- [ ] Reolink **AI event polling** (`GetAiState`) — enable alerts and confirm
  events fire on the real camera (`fe-p` @ 192.168.1.53).
- [ ] **Two-way talk** (Reolink backchannel) — wired, device support uncertain.
- [ ] Spotlight / night-vision / encoder command shapes on the real camera.
- [ ] Light-mode contrast pass on the pages you use most (Settings, History).

---

## How to run

```bash
# start (manual, detached)
cd ~/Cameras
setsid .venv/bin/python -m nvr </dev/null >data/sentry.log 2>&1 &

# tests
.venv/bin/python -m pytest -q

# dashboard: http://<host>.local  (port 80)
```

Settings that used to require editing `config.yaml` are now in the UI. The file
remains the seed/fallback; only `go2rtc.binary` path and `data_dir` are still
file-only (boot-critical / chicken-and-egg).
