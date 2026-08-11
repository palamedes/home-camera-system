/*
 * History playback.
 *
 * The server does not expose recordings as files — it renders arbitrary
 * windows of time on demand. So the timeline is the real interface: you pick
 * an instant, and we ask for a chunk starting there.
 *
 * Playback runs in fixed chunks rather than one endless stream. A chunk is a
 * normal MP4 the browser can buffer and play; when it ends we request the
 * next. That keeps seeking cheap (a new request, not a seek through hours of
 * video) and means a decode error costs one chunk instead of the session.
 */

const CHUNK_SECONDS = 300;

function initHistory({ cameraId, bounds, vcamId = null }) {
  const canvas = document.getElementById('timeline');
  const ctx = canvas.getContext('2d');
  const video = document.getElementById('video');
  const status = document.getElementById('status');
  const playheadLabel = document.getElementById('playhead-label');
  const spanSelect = document.getElementById('span');

  const MIN_SPAN = 300;        // 5 minutes, the tightest zoom
  const MAX_SPAN = 7 * 86400;  // 7 days
  const MAX_EXPORT = 2 * 3600; // clip.mp4 caps here

  let windowEnd = bounds.end;
  let windowSpan = parseInt(spanSelect.value, 10);
  let ranges = [];
  let events = [];

  // Marker colour by detection kind.
  const EVENT_COLORS = {
    person: '#ef4444', vehicle: '#3b82f6', animal: '#22c55e',
    motion: '#f59e0b', flood: '#06b6d4', _default: '#a855f7',
  };
  let chunkStart = null;      // epoch seconds of the loaded chunk
  let hoverX = null;
  let selection = null;       // { start, end } epoch seconds, for export
  let savingClip = false;     // lock the UI while a server-side clip export runs

  const windowStart = () => windowEnd - windowSpan;

  const timeToX = t =>
    ((t - windowStart()) / windowSpan) * canvas.clientWidth;
  const xToTime = x =>
    windowStart() + (x / canvas.clientWidth) * windowSpan;

  const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString([], {
    hour: 'numeric', minute: '2-digit', second: '2-digit',
  });
  const fmtFull = ts => new Date(ts * 1000).toLocaleString([], {
    weekday: 'short', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', second: '2-digit',
  });

  // ---- drawing ----------------------------------------------------------

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    canvas.width = canvas.clientWidth * ratio;
    canvas.height = canvas.clientHeight * ratio;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw();
  }

  function niceTickInterval(span) {
    // Aim for roughly one label every ~120px.
    const target = span / (canvas.clientWidth / 120);
    const steps = [60, 300, 600, 1800, 3600, 10800, 21600, 43200, 86400];
    return steps.find(s => s >= target) || 86400;
  }

  function draw() {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const start = windowStart();
    ctx.clearRect(0, 0, w, h);

    // Gaps read as absence, so the base layer is "no footage".
    ctx.fillStyle = '#1c232d';
    ctx.fillRect(0, 0, w, h);

    // Hour/day gridlines.
    const interval = niceTickInterval(windowSpan);
    ctx.strokeStyle = '#262d38';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#8b949e';
    ctx.font = '10px ui-sans-serif, system-ui, sans-serif';
    ctx.textAlign = 'center';
    const first = Math.ceil(start / interval) * interval;
    for (let t = first; t <= windowEnd; t += interval) {
      const x = Math.round(timeToX(t)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
      const d = new Date(t * 1000);
      const label = interval >= 86400
        ? d.toLocaleDateString([], { month: 'short', day: 'numeric' })
        : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      ctx.fillText(label, x, h - 5);
    }

    // Recorded coverage — neutral slate so it reads as "footage present"
    // background, leaving the vivid colours for event markers (the old blue
    // #2563eb was nearly identical to the vehicle marker, so the whole timeline
    // looked like vehicle events).
    ctx.fillStyle = '#475569';
    for (const range of ranges) {
      const x0 = timeToX(range.start);
      const x1 = timeToX(range.end);
      ctx.fillRect(x0, 6, Math.max(1, x1 - x0), h - 24);
    }

    // Event markers: a downward pip at the top plus a faint full-height line,
    // coloured by kind. Drawn before the playhead so the playhead stays on top.
    for (const ev of events) {
      const x = timeToX(ev.ts);
      if (x < -4 || x > w + 4) continue;
      const color = EVENT_COLORS[ev.type] || EVENT_COLORS._default;
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.45;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, 8);
      ctx.lineTo(Math.round(x) + 0.5, h - 16);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(x - 4, 0);
      ctx.lineTo(x + 4, 0);
      ctx.lineTo(x, 7);
      ctx.closePath();
      ctx.fill();
    }

    // Playhead.
    if (chunkStart !== null) {
      const now = chunkStart + (video.currentTime || 0);
      const x = timeToX(now);
      if (x >= 0 && x <= w) {
        ctx.strokeStyle = '#f59e0b';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h - 16);
        ctx.stroke();
        ctx.fillStyle = '#f59e0b';
        ctx.beginPath();
        ctx.arc(x, 5, 4, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // Selected region (for export).
    if (selection) {
      const sx0 = timeToX(Math.min(selection.start, selection.end));
      const sx1 = timeToX(Math.max(selection.start, selection.end));
      ctx.fillStyle = 'rgba(245, 158, 11, 0.22)';
      ctx.fillRect(sx0, 0, sx1 - sx0, h - 16);
      ctx.strokeStyle = '#f59e0b';
      ctx.lineWidth = 1;
      ctx.strokeRect(sx0 + 0.5, 0.5, sx1 - sx0 - 1, h - 17);
    }

    // Hover guide.
    if (hoverX !== null) {
      ctx.strokeStyle = 'rgba(230,237,243,0.35)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(hoverX + 0.5, 0);
      ctx.lineTo(hoverX + 0.5, h - 16);
      ctx.stroke();
    }

    document.getElementById('label-start').textContent = fmtFull(start);
    document.getElementById('label-mid').textContent =
      fmtFull(start + windowSpan / 2);
    document.getElementById('label-end').textContent = fmtFull(windowEnd);
  }

  // ---- data -------------------------------------------------------------

  async function loadCoverage() {
    const url = `/api/cameras/${encodeURIComponent(cameraId)}/timeline`
      + `?start=${windowStart()}&end=${windowEnd}`;
    try {
      const response = await fetch(url);
      if (!response.ok) return;
      const data = await response.json();
      ranges = data.ranges || [];
      events = data.events || [];
      if (data.bounds) bounds = data.bounds;
      draw();
    } catch (error) {
      console.warn('timeline fetch failed', error);
    }
  }

  function hasFootageAt(ts) {
    return ranges.some(r => ts >= r.start && ts < r.end);
  }

  // Nearest event marker to a canvas x, within a few px — for hover labels and
  // click-to-jump.
  function eventAtX(x) {
    let best = null, bestDx = 7;
    for (const ev of events) {
      const dx = Math.abs(timeToX(ev.ts) - x);
      if (dx < bestDx) { bestDx = dx; best = ev; }
    }
    return best;
  }

  // ---- playback ---------------------------------------------------------

  function playFrom(ts, { announce = true } = {}) {
    if (ts < bounds.start) ts = bounds.start;
    if (ts > bounds.end) ts = Math.max(bounds.start, bounds.end - 5);

    chunkStart = ts;
    status.classList.remove('hidden');
    status.innerHTML = '<span class="spinner"></span>';
    video.src = `/api/cameras/${encodeURIComponent(cameraId)}/playback.mp4`
      + `?start=${ts}&duration=${CHUNK_SECONDS}`;
    video.play().catch(() => { /* autoplay refusal is fine; controls are shown */ });
    if (announce) playheadLabel.textContent = `Playing from ${fmtFull(ts)}`;
    draw();
  }

  video.addEventListener('loadeddata', () => status.classList.add('hidden'));

  video.addEventListener('error', () => {
    status.classList.remove('hidden');
    status.textContent = hasFootageAt(chunkStart)
      ? 'Could not decode that segment.'
      : 'No footage recorded at that time.';
  });

  video.addEventListener('timeupdate', () => {
    if (chunkStart === null) return;
    draw();
    playheadLabel.textContent =
      `Playing ${fmtFull(chunkStart + video.currentTime)}`;
  });

  // Roll straight into the following chunk so long stretches play unattended.
  video.addEventListener('ended', () => {
    if (chunkStart === null) return;
    const next = chunkStart + CHUNK_SECONDS;
    if (next < bounds.end) playFrom(next, { announce: false });
    else {
      status.classList.remove('hidden');
      status.textContent = 'Reached the end of recorded footage.';
    }
  });

  // ---- interaction ------------------------------------------------------

  // A plain press: click to seek, or drag past a threshold to select a region
  // for export. Holding Ctrl (or Cmd) instead grabs the timeline and pans it
  // left/right, so you can slide the visible window through time by hand — the
  // same range you can reach with the scroll-to-zoom and the span presets.
  const DRAG_THRESHOLD = 4;
  let press = null;  // { x, time, dragging, pan, panEnd }

  const localX = event => event.clientX - canvas.getBoundingClientRect().left;
  // Ctrl or Cmd starts a pan. On macOS Ctrl+click is delivered as a right-click
  // (button 2), so treat the secondary button as pan too, and suppress the
  // context menu that would otherwise eat the drag.
  const isPanKey = event =>
    event.ctrlKey || event.metaKey || event.button === 2 || event.buttons === 2;
  canvas.addEventListener('contextmenu', e => e.preventDefault());

  // Keep the visible window overlapping real footage: a pan can't push it
  // entirely past the newest clip or before the oldest.
  function clampEnd(end) {
    const minEnd = bounds.start + windowSpan;   // windowStart >= bounds.start
    const maxEnd = bounds.end;                   // don't scroll past newest
    if (minEnd > maxEnd) return maxEnd;          // window wider than data: pin
    return Math.max(minEnd, Math.min(maxEnd, end));
  }

  // Panning reveals time that may not be covered yet; refresh coverage as we
  // go, throttled so a fast drag doesn't fetch every pixel.
  let lastPanLoad = 0;
  function panCoverage() {
    const now = (window.performance && performance.now()) || Date.now();
    if (now - lastPanLoad > 200) { lastPanLoad = now; loadCoverage(); }
  }

  canvas.addEventListener('pointerdown', event => {
    if (savingClip) return;   // don't seek/re-select while a clip export is running
    press = {
      x: localX(event), time: xToTime(localX(event)),
      dragging: false, pan: isPanKey(event), panEnd: windowEnd,
    };
    // Capture so the drag keeps tracking even if the pointer leaves the canvas.
    try { canvas.setPointerCapture(event.pointerId); } catch (_) {}
    if (press.pan) event.preventDefault();
  });

  canvas.addEventListener('pointermove', event => {
    const x = localX(event);
    hoverX = x;
    const hoverEv = eventAtX(x);
    canvas.title = hoverEv
      ? `${hoverEv.label || hoverEv.type} — ${fmtTime(hoverEv.ts)}`
      : fmtTime(xToTime(x));
    const panMode = (press && press.pan) || event.ctrlKey || event.metaKey;
    canvas.style.cursor = panMode
      ? (press && press.pan ? 'grabbing' : 'grab')
      : 'pointer';
    if (press) {
      if (Math.abs(x - press.x) > DRAG_THRESHOLD) press.dragging = true;
      if (press.pan) {
        // Grab-and-slide: dragging right pulls the timeline right, revealing
        // earlier time (the window moves back), like dragging a map.
        const dxTime = ((x - press.x) / canvas.clientWidth) * windowSpan;
        windowEnd = clampEnd(press.panEnd - dxTime);
        panCoverage();
      } else if (press.dragging) selection = { start: press.time, end: xToTime(x) };
    }
    draw();
  });

  canvas.addEventListener('pointerup', event => {
    if (!press) return;
    if (press.pan) {
      loadCoverage();   // settle: pull coverage/events for the final window
    } else if (press.dragging && selection) {
      // Normalise and clamp the selected span, then reveal the export bar.
      let [a, b] = [selection.start, selection.end].sort((x, y) => x - y);
      if (b - a > MAX_EXPORT) b = a + MAX_EXPORT;
      selection = { start: a, end: b };
      showSelection();
    } else {
      // A plain click seeks — but clicking on an event marker jumps to a couple
      // of seconds before that event so you catch the lead-up.
      const ev = eventAtX(localX(event));
      playFrom(Math.floor(ev ? ev.ts - 2 : press.time));
    }
    press = null;
    draw();
  });

  canvas.addEventListener('pointercancel', () => { press = null; draw(); });
  canvas.addEventListener('pointerleave', () => { if (!press) { hoverX = null; draw(); } });

  // Scroll to zoom, centered on the cursor so the moment under the pointer
  // stays put.
  canvas.addEventListener('wheel', event => {
    event.preventDefault();
    const x = localX(event);
    const pivot = xToTime(x);
    const factor = event.deltaY > 0 ? 1.25 : 0.8;
    const newSpan = Math.max(MIN_SPAN, Math.min(MAX_SPAN, windowSpan * factor));
    // Keep `pivot` at the same x: windowEnd = pivot + (fraction to the right)*span
    const fracRight = 1 - x / canvas.clientWidth;
    windowSpan = newSpan;
    windowEnd = pivot + fracRight * newSpan;
    spanSelect.value = nearestPreset(newSpan);
    loadCoverage();
    draw();
  }, { passive: false });

  function nearestPreset(span) {
    const opts = [...spanSelect.options].map(o => parseInt(o.value, 10));
    return String(opts.reduce((a, b) => (Math.abs(b - span) < Math.abs(a - span) ? b : a)));
  }

  // ---- region export ----------------------------------------------------

  const selectionBar = document.getElementById('selection-bar');
  const selectionLabel = document.getElementById('selection-label');

  function showSelection() {
    if (!selection) return;
    const secs = Math.round(selection.end - selection.start);
    const mins = (secs / 60).toFixed(secs < 600 ? 1 : 0);
    selectionLabel.textContent =
      `${fmtFull(selection.start)} → ${fmtFull(selection.end)} (${mins} min)`;
    selectionBar.hidden = false;
  }

  function clearSelection() {
    selection = null;
    selectionBar.hidden = true;
    draw();
  }

  document.getElementById('selection-clear').addEventListener('click', clearSelection);

  // Capture the *rendered* playback in real time. For a virtual camera this is
  // the dewarp canvas, so what you get is exactly the dewarped view — no
  // server-side reprojection to get wrong. Raw cameras capture the video. The
  // clip is then downloaded or saved to the box.
  // Tap the history <video>'s audio into a MediaStream via Web Audio — the
  // reliable cross-browser way to capture a media element's audio for a
  // recording. Built once (createMediaElementSource throws if called twice on
  // the same element) and also wired to the speakers so playback stays audible.
  let audioCtx = null, audioDest = null;
  function playbackAudioTrack() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try {
      if (!audioDest) {
        audioCtx = new AC();
        const src = audioCtx.createMediaElementSource(video);
        audioDest = audioCtx.createMediaStreamDestination();
        src.connect(audioDest);          // tap for the recorder
        src.connect(audioCtx.destination); // keep it audible
      }
      if (audioCtx.state === 'suspended') audioCtx.resume();
      const tracks = audioDest.stream.getAudioTracks();
      return tracks.length ? tracks[0] : null;
    } catch (_) {
      return null;
    }
  }

  async function captureClip(download) {
    if (!selection) return;
    const el = document.getElementById('dewarp') || video;
    const grab = (node, fps) => node.captureStream ? node.captureStream(fps)
      : node.mozCaptureStream ? node.mozCaptureStream(fps) : null;
    if (!grab(el) || typeof MediaRecorder === 'undefined') {
      alert('This browser cannot capture clips.');
      return;
    }
    const start = Math.floor(selection.start);
    const secs = Math.max(1, Math.round(selection.end - selection.start));
    // Prefer a codec string that carries audio, so dewarped clips keep sound.
    const mime = ['video/webm;codecs=vp8,opus', 'video/webm;codecs=vp9,opus',
                  'video/webm', 'video/mp4']
      .find(m => MediaRecorder.isTypeSupported(m)) || '';

    const label = document.getElementById('selection-label');
    const restore = label.textContent;
    let left = secs;
    label.textContent = `● Recording clip… ${left}s`;
    const ticker = setInterval(() => {
      left -= 1; label.textContent = `● Recording clip… ${Math.max(0, left)}s`;
    }, 1000);

    // Start playback first so both the rendered frames and the audio track are
    // live before we capture them, then unmute so the captured audio carries
    // sound (the history <video> is muted for local output on a virtual camera).
    const wasMuted = video.muted;
    video.muted = false;
    playFrom(start, { announce: false });
    await new Promise(r => setTimeout(r, 500));   // let playback settle

    // Recording stream: rendered frames (the dewarp canvas for a virtual
    // camera, else the video) + the playback audio tapped via Web Audio.
    // HTMLMediaElement.captureStream() audio is unreliable across browsers, so
    // don't depend on it — playbackAudioTrack() taps deterministically.
    const vtracks = grab(el, 25).getVideoTracks();
    const atrack = playbackAudioTrack();
    const stream = new MediaStream(atrack ? [...vtracks, atrack] : vtracks);

    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    rec.ondataavailable = e => e.data && e.data.size && chunks.push(e.data);
    const finished = new Promise(res => { rec.onstop = res; });

    rec.start();
    setTimeout(() => { if (rec.state !== 'inactive') rec.stop(); }, secs * 1000);
    await finished;

    clearInterval(ticker);
    video.muted = wasMuted;
    label.textContent = restore;
    const blob = new Blob(chunks, { type: rec.mimeType || mime || 'video/webm' });

    const stamp = new Date(start * 1000).toLocaleString([], {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit',
    });
    if (download) {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${cameraId}-${start}.webm`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    } else {
      const fd = new FormData();
      fd.append('file', blob, 'clip.webm');
      fd.append('camera_id', cameraId);
      fd.append('name', stamp);
      if (vcamId != null) fd.append('vcam_id', String(vcamId));
      fd.append('start', String(start));
      fd.append('duration', String(secs));
      const r = await fetch('/api/clips', { method: 'POST', body: fd });
      if (r.ok) { if (confirm('Clip saved. View your clips now?')) location.href = '/clips'; }
      else alert('Could not save the clip.');
    }
  }

  document.getElementById('selection-save').addEventListener('click', async () => {
    if (!selection || savingClip) return;
    // Dewarped virtual camera: only the browser has the rendered frames, so
    // capture there. A normal camera saves an exact server-side ffmpeg cut —
    // no real-time re-record (which dropped frames, ran short, desynced audio).
    if (vcamId != null) { captureClip(false); return; }
    const start = Math.floor(selection.start);
    const duration = Math.max(1, Math.round(selection.end - selection.start));
    const stamp = new Date(start * 1000).toLocaleString([], {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit',
    });
    const label = document.getElementById('selection-label');
    const buttons = selectionBar.querySelectorAll('button');
    const restore = label.textContent;
    // Lock the selection bar (Clear / Save / Export) and the timeline while the
    // export runs, so nothing can be changed mid-save.
    savingClip = true;
    buttons.forEach(b => { b.disabled = true; });
    label.textContent = '● Saving clip…';
    try {
      const r = await fetch(`/api/cameras/${encodeURIComponent(cameraId)}/save-clip`
        + `?start=${start}&duration=${duration}&name=${encodeURIComponent(stamp)}`,
        { method: 'POST' });
      if (r.ok) {
        if (confirm('Clip saved. View your clips now?')) location.href = '/clips';
      } else {
        const d = await r.json().catch(() => ({}));
        alert(d.error || 'Could not save the clip.');
      }
    } catch { alert('Could not save the clip.'); }
    finally {
      savingClip = false;
      buttons.forEach(b => { b.disabled = false; });
      label.textContent = restore;
    }
  });
  document.getElementById('selection-export').addEventListener('click', () => {
    if (!selection) return;
    if (vcamId != null) {
      // Dewarped view — capture the rendered canvas instead of the raw fisheye.
      captureClip(true);
      return;
    }
    const start = Math.floor(selection.start);
    const duration = Math.max(1, Math.round(selection.end - selection.start));
    window.location = `/api/cameras/${encodeURIComponent(cameraId)}/clip.mp4`
      + `?start=${start}&duration=${duration}`;
  });

  document.querySelectorAll('[data-jump]').forEach(button => {
    button.addEventListener('click', () => {
      const delta = parseInt(button.dataset.jump, 10);
      const from = chunkStart !== null
        ? chunkStart + (video.currentTime || 0)
        : windowEnd;
      playFrom(Math.floor(from + delta));
    });
  });

  spanSelect.addEventListener('change', () => {
    windowSpan = parseInt(spanSelect.value, 10);
    loadCoverage();
    draw();
  });

  window.addEventListener('resize', resize);

  // Keep the window edge tracking newly recorded footage while idle. Runs often
  // so freshly-indexed footage shows up promptly — the timeline already trails
  // live by however long a segment takes to close, and a slow refresh stacked
  // on top of that. The window edge only advances when nothing is playing, so
  // this never yanks the view out from under a rewind.
  setInterval(() => {
    if (chunkStart === null) windowEnd = Math.max(windowEnd, Date.now() / 1000);
    loadCoverage();
  }, 10000);

  resize();
  loadCoverage();
}
