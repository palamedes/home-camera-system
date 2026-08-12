// Settings page: tabbed navigation + live-editable Weather/Alerts forms.
//
// The forms PATCH /api/settings/<section>; the server validates, applies to the
// running services in place, and persists to the DB (overriding config.yaml).
// No reload needed — weather re-fetches and the event poller starts/stops on
// the server side.
(function () {
  // ---- tabs -------------------------------------------------------------
  const tabButtons = [...document.querySelectorAll('[data-tab-btn]')];
  const panels = [...document.querySelectorAll('[data-tab]')];
  const KNOWN = new Set(tabButtons.map((b) => b.dataset.tabBtn));
  const STORE = 'sentry-settings-tab';

  function activate(name) {
    if (!KNOWN.has(name)) name = 'cameras';
    panels.forEach((p) => { p.hidden = p.dataset.tab !== name; });
    tabButtons.forEach((b) =>
      b.classList.toggle('active', b.dataset.tabBtn === name));
    try { localStorage.setItem(STORE, name); } catch (e) {}
    if (location.hash.slice(1) !== name) {
      history.replaceState(null, '', '#' + name);
    }
  }

  if (tabButtons.length) {
    tabButtons.forEach((b) =>
      b.addEventListener('click', () => activate(b.dataset.tabBtn)));
    let initial = location.hash.slice(1);
    if (!KNOWN.has(initial)) {
      try { initial = localStorage.getItem(STORE); } catch (e) { initial = null; }
    }
    activate(KNOWN.has(initial) ? initial : 'cameras');
  }

  // ---- settings forms ---------------------------------------------------

  function collect(form) {
    const body = {};
    form.querySelectorAll('[data-field]').forEach((el) => {
      const key = el.dataset.field;
      const type = el.dataset.type || 'str';
      if (type === 'bool') body[key] = el.checked;
      else if (type === 'int') body[key] = parseInt(el.value, 10);
      else if (type === 'float') body[key] = parseFloat(el.value);
      else body[key] = el.value;
    });
    const detect = form.querySelectorAll('[data-detect]');
    if (detect.length) {
      body.detect = [...detect].filter((d) => d.checked).map((d) => d.value);
    }
    return body;
  }

  function wireForm(form) {
    const section = form.dataset.settingsForm;
    const result = document.querySelector(`[data-form-result="${section}"]`);
    const setResult = (text, ok) => {
      if (!result) return;
      result.textContent = text;
      result.style.color = ok ? 'var(--ok)' : 'var(--bad)';
      if (ok) setTimeout(() => { result.textContent = ''; }, 2500);
    };

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      setResult('Saving…', true);
      try {
        const r = await fetch(`/api/settings/${section}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(collect(form)),
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok) {
          if (d.restart_required && d.restart_required.length) {
            // Don't auto-clear: this needs the operator to act.
            if (result) {
              result.textContent = '✓ Saved — restart required to apply';
              result.style.color = 'var(--warn)';
            }
          } else {
            setResult('✓ Saved', true);
          }
        } else setResult(d.error || 'Save failed', false);
      } catch (_) {
        setResult('Network error', false);
      }
    });
  }

  document.querySelectorAll('[data-settings-form]').forEach(wireForm);

  // ---- location search (geocoding) -------------------------------------
  const geoInput = document.getElementById('geo-search');
  const geoBtn = document.getElementById('geo-search-btn');
  const geoResults = document.getElementById('geo-results');

  function setField(name, value) {
    const el = document.querySelector(`#weather-form [data-field="${name}"]`);
    if (el) el.value = value;
  }

  async function runGeocode() {
    const q = (geoInput.value || '').trim();
    if (q.length < 2 || !geoResults) return;
    geoResults.hidden = false;
    geoResults.innerHTML = '<div class="geo-item muted">Searching…</div>';
    try {
      const r = await fetch('/api/settings/geocode?q=' + encodeURIComponent(q));
      const d = await r.json();
      const rows = d.results || [];
      if (!rows.length) {
        geoResults.innerHTML = '<div class="geo-item muted">No matches.</div>';
        return;
      }
      geoResults.innerHTML = '';
      rows.forEach((row) => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'geo-item';
        item.textContent = `${row.label}  (${row.latitude.toFixed(3)}, ${row.longitude.toFixed(3)})`;
        item.addEventListener('click', () => {
          setField('latitude', row.latitude);
          setField('longitude', row.longitude);
          setField('label', row.label);
          geoResults.hidden = true;
          geoInput.value = '';
        });
        geoResults.appendChild(item);
      });
    } catch (_) {
      geoResults.innerHTML = '<div class="geo-item muted">Search failed.</div>';
    }
  }

  if (geoBtn) geoBtn.addEventListener('click', runGeocode);
  if (geoInput) {
    geoInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); runGeocode(); }
    });
  }

  // ---- per-field help (? icon -> tooltip + click-through modal) ---------
  // [name, short tooltip, full explanation], keyed by "<section>.<field>".
  const HELP = {
    'weather.enabled': ['Weather card', 'Show or hide the dashboard weather card', 'When on, the dashboard shows current conditions and the river level, and the server fetches weather on a timer. Off hides the card and stops fetching.'],
    'weather.location': ['Location search', 'Search a place to fill coordinates', 'Type a town or city and pick a result to set latitude, longitude, and label automatically — no need to look up coordinates yourself.'],
    'weather.label': ['Location label', 'Name shown on the card', 'The place name displayed on the weather card. Cosmetic only.'],
    'weather.latitude': ['Latitude', 'Latitude for the forecast', 'Decimal latitude (−90 to 90) used to fetch conditions. Easiest to set with the search box above.'],
    'weather.longitude': ['Longitude', 'Longitude for the forecast', 'Decimal longitude (−180 to 180) used to fetch conditions. Easiest to set with the search box above.'],
    'weather.temperature_unit': ['Temperature unit', 'Fahrenheit or Celsius', 'Unit for temperature, feels-like, dew point, and the daily high/low.'],
    'weather.wind_unit': ['Wind unit', 'mph, km/h, m/s or knots', 'Unit for wind speed and gusts.'],
    'weather.precipitation_unit': ['Precipitation unit', 'Inches or millimetres', 'Unit for precipitation amounts.'],
    'weather.refresh_seconds': ['Refresh interval', 'How often weather is re-fetched', 'Seconds between server-side weather refreshes (minimum 60). These feeds update every few minutes, so ~600 (10 min) is plenty.'],
    'weather.water_gauge': ['River gauge', 'NWS gauge id, e.g. ORLN7', 'The NOAA/NWS gauge whose water level is shown. Find ids at water.noaa.gov. Blank hides the river panel.'],
    'weather.water_label': ['River label', 'Name shown for the river', 'Cosmetic name for the river/gauge on the card.'],
    'weather.water_alert_level': ['Flood alert level', 'Notify above this stage (ft)', 'If above 0, you get an alert when the gauge rises to or above this height in feet. 0 disables the threshold.'],
    'weather.water_alert_on_action': ['NWS flood-stage alerts', 'Alert when NWS flags flooding', 'Sends an alert whenever the National Weather Service moves the gauge into any flood category past normal (Action, Minor, Moderate, Major).'],

    'alerts.enabled': ['Alerts', 'Master switch for smart events', 'On: cameras are polled for AI detections, events are recorded on the timeline, and (if a webhook is set) notifications are sent. Off: no detection or notification.'],
    'alerts.webhook_url': ['Webhook URL', 'Where notifications are POSTed', 'Sentry POSTs a JSON payload here for each alert — point it at Home Assistant, a Discord/Slack relay, ntfy, or your own script. Blank records events without sending anything.'],
    'alerts.detect': ['Detections', 'Which objects raise events', 'Which Reolink onboard-AI classes create events: person, vehicle, animal, and (noisier) plain motion.'],
    'alerts.cooldown_seconds': ['Cooldown', 'Minimum gap between repeats', "The same camera + kind won't notify again within this many seconds, so a lingering person is one alert, not fifty."],
    'alerts.poll_seconds': ['Poll interval', 'How often cameras are checked', "Seconds between checks of each Reolink camera's AI state. Lower = faster alerts but more camera load."],

    'network.host': ['Listen host', 'Which interface to bind (restart)', "The address the web server binds to. '::' = all interfaces (IPv4+IPv6), or a specific IP. Applies after a restart."],
    'network.port': ['Listen port', 'Web server port (restart)', 'The port the dashboard is served on (80 = plain http://host.local). Applies after a restart; a bad value falls back to config.yaml so you can’t get locked out.'],
    'network.session_days': ['Session length', 'How long logins last', 'Days a browser stays signed in before needing to log in again.'],
    'network.secure_cookies': ['Secure cookies', 'Set only when using HTTPS', 'Marks the session cookie Secure so it’s only sent over HTTPS. Leave off on a plain-http LAN; turn on when served over HTTPS (e.g. Tailscale Serve).'],
    'network.go2rtc_api_port': ['go2rtc API port', 'Internal streaming API port (restart)', 'Loopback port for go2rtc’s API. Only change it if it clashes with something else. Restart to apply.'],
    'network.go2rtc_rtsp_port': ['go2rtc RTSP port', 'Internal RTSP port (restart)', 'Loopback RTSP port the recorder pulls from. Restart to apply.'],
    'network.go2rtc_webrtc_port': ['go2rtc WebRTC port', 'Live-view WebRTC port (restart)', 'Port used for live WebRTC video to your browser; must be reachable on the LAN. Restart to apply.'],
    'network.discovery_subnets': ['Discovery subnets', 'Where to scan for cameras', 'CIDR ranges the network scan sweeps, comma-separated (e.g. 192.168.1.0/24). Blank = auto-detect from this machine’s interfaces.'],
    'network.discovery_timeout': ['Discovery timeout', 'Per-host scan timeout', 'Seconds to wait for each host during a scan. Higher finds slow devices; lower scans faster.'],
    'network.onvif_wait': ['ONVIF wait', 'ONVIF discovery wait', 'Seconds to listen for ONVIF camera announcements during a scan.'],
    'network.always_transcode': ['Always transcode', 'Force re-encode on playback', 'Normally footage that’s already browser-compatible is remuxed (cheap). On forces a full re-encode — rarely needed.'],
    'network.qsv_device': ['QSV device', 'Hardware transcode device', 'Intel QuickSync render device (e.g. /dev/dri/renderD128) used to accelerate playback transcoding. Blank = software encoding.'],

    'devices.overview': ['Devices', 'Relays and smart switches on your LAN', "Non-camera hardware Sentry controls directly over local HTTP — Shelly relays first. Add one by its LAN address (reserve that address on your router so it can't move), press Test to confirm Sentry can reach it, then use On/Off. Because it's local HTTP there's no hub, no cloud and no vendor account. The same automation token that protects the light hook also lets a device — or a wall switch wired into a Shelly's input — call Sentry back, so a paddle press, a schedule, or a camera detection can all drive the same relay."],

    'automation.overview': ['Automation & scene switches', 'Toggle lights from a switch or Home Assistant', "A shared-secret URL that toggles a camera's floodlight/spotlight without a login, so a smart scene switch or Home Assistant can control the light while the camera stays powered. Use state=on, off, or toggle. Keep the token private; regenerate it to revoke anything using the old one. The camera must be always-powered (don't put it behind the light switch)."],

    'storage_limits.max_usage': ['Max usage', 'Disk budget for recordings', 'How much space recordings may use before the oldest are pruned — a percent of the disk (80%) or a size (380G).'],
    'storage_limits.max_age_days': ['Max age', 'Hard delete-after age', 'Footage older than this many days is deleted regardless of free space. 0 = no age cap (space-based pruning only).'],
    'storage_limits.segment_seconds': ['Segment length', 'Length of each recorded file', 'Seconds per recorded file, and the main reason History trails the live view: a segment can only be played once it is finished, so History is always at least this far behind (plus a few seconds to index it). 60 is a good default; drop to 15–20 if you want History to catch up closer to live, at the cost of ~4× as many files. Shorter also means less footage lost to one corrupt file. Recording restarts to apply.'],

    'storage.recordings_dir': ['Recordings folder', 'Where continuous footage is stored', 'Directory continuous recordings are written to — point it at a mounted drive or NAS. New footage goes here immediately; existing footage stays put until you move it.'],
    'storage.clips_dir': ['Clips folder', 'Where saved clips are kept', 'Directory saved clips live in. These are kept permanently and never pruned.'],
    'storage.pool': ['Recordings pool', 'Drives footage is stored across', 'One or more volumes recordings are written to. Footage fills them top to bottom — when one reaches its cap, new recordings overflow to the next. Point volumes at mounted drives or a NAS (mount it at the OS level first). A cap is a percent of that drive (80%) or a size (400G). Existing footage keeps playing wherever it already lives.'],
    'storage.migrate': ['Move stranded footage', 'Consolidate off removed drives', 'Moves footage that still lives on drives you removed from the pool back onto the primary volume so it stays playable. Safe to run anytime — it only touches stranded files.'],

    // ---- cameras tab (per-camera card; keys repeat across cards) ----------
    'cameras.enabled': ['Camera on/off', 'Take the camera online or offline', 'Enables or disables the camera. Off stops all streaming and recording and hides it from live views until you switch it back on — its name, virtual cameras, schedules and existing recordings are all kept.'],
    'cameras.connection': ['Connection', 'Re-point the camera at a new IP', 'Cameras change IP often — a new DHCP lease, a reboot, or moving between wired and WiFi. "Change IP…" re-points this camera at a newly discovered address without losing its name, virtual cameras, schedules or recordings; it just swaps the address in the stream URLs.'],
    'cameras.recording': ['Recording', 'Continuous, off, or a timed window', 'On records continuously. Off stops recording (live view still works). The "For the next…" options record for a set window, then stop on their own.'],
    'cameras.record_stream': ['Record stream', 'Main (HD) or sub (smaller)', 'Which stream is written to disk. Main is full quality and larger; sub is lower-resolution and uses far less space. Live view can use either regardless of this choice.'],
    'cameras.rolling_keep': ['Rolling keep', 'Recent window always kept', "How much recent footage to always keep, anchored to the newest clip. If recording stops, that last window is held (not deleted on a clock) until the 'Delete after' cap. None means only the global/age limits apply."],
    'cameras.retention': ['Delete after', 'Hard age cap for this camera', "Footage from this camera older than this is deleted no matter how much free space there is. Default uses the global max age; Never keeps it until the disk actually needs the space."],
    'cameras.est_storage': ['Estimated storage', 'Rough disk use at current settings', "A rough estimate of disk use for this camera if footage is kept for the whole 'Delete after' window, at the selected stream's bitrate."],
    'cameras.preferred_volume': ['Save to', 'Pin recordings to one drive', "Force this camera's recordings onto a specific drive in the pool — handy for keeping an important camera on its own disk. Default lets it follow the normal pool overflow. If the pinned drive is unmounted or full, recording falls back to the pool so footage is never lost. Only appears when the pool has more than one volume."],
    'cameras.fisheye': ['360° / fisheye', 'Enable dewarp for this camera', 'Marks this as a 360°/fisheye camera so Sentry offers fisheye dewarping and virtual PTZ views for it.'],
    'cameras.show_on_grid': ['On grid', 'Show the raw tile on dashboards', 'When on, the raw camera tile appears on the dashboard and Cameras page. Turn it off to keep only this camera’s virtual (dewarped) views on the grid.'],
    'cameras.viewer_visible': ['Viewer visible', 'Let viewer accounts see this', 'Whether non-admin viewer accounts can see this camera. Off hides it from viewers while admins keep full access.'],
    'cameras.schedules': ['Schedules', 'Automate by time of day', 'Automate recording, the spotlight, or night vision by time of day and weekday, using the server’s local time. An end time earlier than the start wraps past midnight.'],
    'cameras.removed': ['Removed cameras', 'Kept for their footage', 'Cameras you removed with “Keep footage.” They’re no longer streamed or recorded, but their recordings stay viewable in History until normal retention ages them out. Restore brings the camera back; Delete permanently erases its footage now to reclaim space. To keep a specific moment forever, save it as a clip — clips are never pruned.'],

    // ---- users tab --------------------------------------------------------
    'users.overview': ['Users', 'Accounts that can sign in', 'Accounts that can sign into Sentry. Admins have full control — cameras, settings and other users; viewers can only watch the cameras marked viewer-visible. Use "Add user" to create one, "Reset password" to set a new one, and the × to remove one.'],
    'users.role': ['Role', 'Admin or viewer', 'Admin — full control, can manage cameras, settings and users. Viewer — can only watch cameras marked viewer-visible. You can’t change your own role.'],
  };

  function initHelp() {
    // One shared modal, built lazily.
    let modal, mTitle, mBody;
    function ensureModal() {
      if (modal) return;
      modal = document.createElement('div');
      modal.className = 'modal-backdrop';
      modal.hidden = true;
      modal.innerHTML =
        '<div class="modal"><div class="modal-head"><h2></h2>'
        + '<button class="modal-close" type="button" aria-label="Close">&times;</button></div>'
        + '<div class="modal-body"><p style="margin:0;color:var(--text)"></p></div></div>';
      document.body.appendChild(modal);
      mTitle = modal.querySelector('h2');
      mBody = modal.querySelector('p');
      const close = () => { modal.hidden = true; };
      modal.querySelector('.modal-close').addEventListener('click', close);
      modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !modal.hidden) close();
      });
    }
    function open(entry) {
      ensureModal();
      mTitle.textContent = entry[0];
      mBody.textContent = entry[2];
      modal.hidden = false;
    }

    function keyFor(field, section) {
      const df = field.querySelector('[data-field]');
      if (df) return `${section}.${df.dataset.field}`;
      if (field.querySelector('[data-detect]')) return `${section}.detect`;
      const sf = field.querySelector('[data-storage-field]');
      if (sf) return `${section}.${sf.dataset.storageField}`;
      if (field.querySelector('#geo-search')) return 'weather.location';
      return null;
    }
    function sectionFor(field) {
      const form = field.closest('[data-settings-form]');
      if (form) return form.dataset.settingsForm;
      if (field.querySelector('[data-storage-field]')) return 'storage';
      return null;
    }

    function makeHelpBtn(entry) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'help-btn';
      btn.textContent = '?';
      btn.title = entry[1];                       // hover tooltip
      btn.setAttribute('aria-label', `Help: ${entry[0]}`);
      btn.addEventListener('click', (e) => {
        // Inside a <label>, stop the click from toggling the field's control.
        e.preventDefault();
        e.stopPropagation();
        open(entry);
      });
      return btn;
    }

    // Structured forms (weather / alerts / network / storage limits) resolve
    // their section+key from the .set-field markup.
    document.querySelectorAll('.set-field').forEach((field) => {
      const section = sectionFor(field);
      if (!section) return;
      const key = keyFor(field, section);
      const entry = key && HELP[key];
      if (!entry) return;
      const label = field.querySelector('.set-label');
      if (!label || label.querySelector('.help-btn')) return;
      label.appendChild(makeHelpBtn(entry));
    });

    // Cameras / users / storage-pool use their own card markup, so those
    // labels carry an explicit data-help="section.key" instead. Keys repeat
    // across per-camera cards — every occurrence gets its own ? button.
    document.querySelectorAll('[data-help]').forEach((el) => {
      const entry = HELP[el.dataset.help];
      if (!entry || el.querySelector('.help-btn')) return;
      el.appendChild(makeHelpBtn(entry));
    });
  }

  initHelp();

  // ---- storage location -------------------------------------------------
  const storageSave = document.getElementById('storage-save');
  if (storageSave) initStorage();
  initVolumes();

  function humanSize(bytes) {
    if (bytes == null) return '?';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let n = bytes, i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
  }

  function initStorage() {
    const fields = [...document.querySelectorAll('[data-storage-field]')];
    const result = document.querySelector('[data-form-result="storage"]');
    const checkBtn = document.getElementById('storage-check');
    const migrateBtn = document.getElementById('storage-migrate');
    const migrateStatus = document.getElementById('storage-migrate-status');
    const migrateBar = document.getElementById('storage-migrate-bar');

    const setResult = (text, ok) => {
      if (!result) return;
      result.textContent = text;
      result.style.color = ok ? 'var(--ok)' : 'var(--bad)';
      if (ok) setTimeout(() => { result.textContent = ''; }, 3000);
    };
    const body = () => {
      const o = {};
      fields.forEach((f) => { o[f.dataset.storageField] = f.value.trim(); });
      return o;
    };
    const showSpace = (key, info) => {
      const el = document.querySelector(`[data-storage-space="${key}"]`);
      if (!el || !info) return;
      if (info.free != null) {
        el.textContent = `${humanSize(info.free)} free of ${humanSize(info.total)}`
          + (info.ok === false ? ` — ${info.error}` : '');
        el.style.color = info.ok === false ? 'var(--bad)' : '';
      } else if (info.error) {
        el.textContent = info.error; el.style.color = 'var(--bad)';
      }
    };

    // Initial load: show current free space and resume any running migration.
    fetch('/api/settings/storage').then((r) => r.json()).then((d) => {
      Object.entries(d.current || {}).forEach(([k, v]) => showSpace(k, v));
      renderMigrate(d.migrate);
      if (d.migrate && d.migrate.state === 'running') pollMigrate();
    }).catch(() => {});

    if (checkBtn) checkBtn.addEventListener('click', async () => {
      setResult('Checking…', true);
      try {
        const r = await fetch('/api/settings/storage/check', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body()),
        });
        const d = await r.json();
        Object.entries(d.checks || {}).forEach(([k, v]) => showSpace(k, v));
        const bad = Object.values(d.checks || {}).find((c) => c.ok === false);
        setResult(bad ? 'Some paths are not usable' : '✓ Paths look good', !bad);
      } catch (_) { setResult('Check failed', false); }
    });

    storageSave.addEventListener('click', async () => {
      if (!confirm('Switch storage to these paths? New recordings will write '
        + 'there right away; recording restarts briefly.')) return;
      setResult('Saving…', true);
      try {
        const r = await fetch('/api/settings/storage', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body()),
        });
        const d = await r.json();
        if (r.ok) {
          setResult('✓ Storage location updated', true);
          Object.entries(d.current || {}).forEach(([k, v]) => showSpace(k, v));
        } else setResult(d.error || 'Save failed', false);
      } catch (_) { setResult('Network error', false); }
    });

    if (migrateBtn) migrateBtn.addEventListener('click', async () => {
      if (!confirm('Move all existing footage into the current storage location? '
        + 'This runs in the background and can take a while for large libraries.')) return;
      migrateBtn.disabled = true;
      try {
        const r = await fetch('/api/settings/storage/migrate', { method: 'POST' });
        const d = await r.json();
        renderMigrate(d.migrate);
        pollMigrate();
      } catch (_) { migrateBtn.disabled = false; }
    });

    function renderMigrate(m) {
      if (!m || !migrateStatus) return;
      const running = m.state === 'running';
      if (migrateBtn) migrateBtn.disabled = running;
      if (migrateBar) {
        migrateBar.hidden = !(running || m.total);
        const pct = m.total ? Math.round(((m.moved + m.skipped + m.failed) / m.total) * 100) : 0;
        const span = migrateBar.querySelector('span');
        if (span) span.style.width = pct + '%';
      }
      if (m.state === 'idle') return;
      const done = `${m.moved} moved` + (m.skipped ? `, ${m.skipped} skipped` : '')
        + (m.failed ? `, ${m.failed} failed` : '') + ` (${humanSize(m.bytes_moved)})`;
      migrateStatus.textContent =
        running ? `Moving… ${m.moved + m.skipped + m.failed}/${m.total} — ${done}`
        : m.state === 'error' ? `Error: ${m.error}`
        : `Done — ${done}`;
    }

    let poller = null;
    function pollMigrate() {
      if (poller) return;
      poller = setInterval(async () => {
        try {
          const r = await fetch('/api/settings/storage/migrate');
          const m = await r.json();
          renderMigrate(m);
          if (m.state !== 'running') { clearInterval(poller); poller = null; }
        } catch (_) { clearInterval(poller); poller = null; }
      }, 1500);
    }
  }

  // ---- recordings pool (volumes editor) ---------------------------------
  function initVolumes() {
    const list = document.getElementById('volume-list');
    if (!list) return;
    const addBtn = document.getElementById('volume-add');
    const saveBtn = document.getElementById('volume-save');
    const result = document.querySelector('[data-form-result="volumes"]');
    let vols = [];

    const setResult = (text, ok) => {
      if (!result) return;
      result.textContent = text;
      result.style.color = ok ? 'var(--ok)' : 'var(--bad)';
      if (ok) setTimeout(() => { result.textContent = ''; }, 3000);
    };

    // Pull current input values back into the model before a structural change.
    function readInputs() {
      [...list.querySelectorAll('[data-vol-row]')].forEach((row, i) => {
        if (!vols[i]) return;
        vols[i].path = row.querySelector('[data-vol-path]').value;
        vols[i].cap = row.querySelector('[data-vol-cap]').value;
      });
    }

    function usageText(v, primary) {
      const bits = [];
      if (primary) bits.push('Primary');
      if (v.used != null) bits.push(`${humanSize(v.used)} used`);
      if (v.cap_bytes) bits.push(`cap ${humanSize(v.cap_bytes)}`);
      if (v.free != null) bits.push(`${humanSize(v.free)} free`);
      if (v.available === false) bits.push('⚠ not mounted');
      return bits.join(' · ');
    }

    function render() {
      list.innerHTML = '';
      vols.forEach((v, i) => {
        const row = document.createElement('div');
        row.className = 'volume-row';
        row.setAttribute('data-vol-row', '');
        row.innerHTML =
          '<div class="vol-order">'
          + `<button type="button" class="btn btn-sm" data-vol-up ${i === 0 ? 'disabled' : ''}>↑</button>`
          + `<button type="button" class="btn btn-sm" data-vol-down ${i === vols.length - 1 ? 'disabled' : ''}>↓</button>`
          + '</div>'
          + '<input type="text" class="rec-select" data-vol-path placeholder="/mnt/nas/sentry">'
          + '<input type="text" class="rec-select vol-cap" data-vol-cap placeholder="80%">'
          + '<button type="button" class="btn btn-sm btn-danger" data-vol-remove aria-label="Remove volume">✕</button>'
          + '<div class="vol-usage muted small"></div>';
        row.querySelector('[data-vol-path]').value = v.path || '';
        row.querySelector('[data-vol-cap]').value = v.cap || '';
        row.querySelector('.vol-usage').textContent = usageText(v, i === 0);
        row.querySelector('[data-vol-up]').addEventListener('click', () => {
          readInputs();
          if (i > 0) { [vols[i - 1], vols[i]] = [vols[i], vols[i - 1]]; render(); }
        });
        row.querySelector('[data-vol-down]').addEventListener('click', () => {
          readInputs();
          if (i < vols.length - 1) { [vols[i + 1], vols[i]] = [vols[i], vols[i + 1]]; render(); }
        });
        row.querySelector('[data-vol-remove]').addEventListener('click', () => {
          readInputs();
          if (vols.length > 1) { vols.splice(i, 1); render(); }
          else setResult('At least one volume is required', false);
        });
        list.appendChild(row);
      });
    }

    if (addBtn) addBtn.addEventListener('click', () => {
      readInputs(); vols.push({ path: '', cap: '80%' }); render();
    });

    if (saveBtn) saveBtn.addEventListener('click', async () => {
      readInputs();
      const payload = vols
        .map((v) => ({ path: (v.path || '').trim(), cap: (v.cap || '80%').trim() }))
        .filter((v) => v.path);
      if (!payload.length) { setResult('Add at least one volume', false); return; }
      if (!confirm('Save the recordings pool? New footage moves to the first '
        + 'volume with room; recording restarts briefly.')) return;
      setResult('Saving…', true);
      try {
        const r = await fetch('/api/settings/volumes', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ volumes: payload }),
        });
        const d = await r.json();
        if (r.ok) { vols = d.volumes; render(); setResult('✓ Saved', true); }
        else setResult(d.error || 'Save failed', false);
      } catch (_) { setResult('Network error', false); }
    });

    fetch('/api/settings/volumes').then((r) => r.json()).then((d) => {
      vols = (d.volumes && d.volumes.length) ? d.volumes : [{ path: '', cap: '80%' }];
      render();
    }).catch(() => { list.innerHTML = '<div class="muted small">Could not load volumes.</div>'; });
  }
})();
