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
  const downloadBtn = document.getElementById('download-btn');

  const MIN_SPAN = 300;        // 5 minutes, the tightest zoom
  const MAX_SPAN = 7 * 86400;  // 7 days
  const MAX_EXPORT = 2 * 3600; // clip.mp4 caps here

  let windowEnd = bounds.end;
  let windowSpan = parseInt(spanSelect.value, 10);
  let ranges = [];
  let chunkStart = null;      // epoch seconds of the loaded chunk
  let hoverX = null;
  let selection = null;       // { start, end } epoch seconds, for export

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

    // Recorded coverage.
    ctx.fillStyle = '#2563eb';
    for (const range of ranges) {
      const x0 = timeToX(range.start);
      const x1 = timeToX(range.end);
      ctx.fillRect(x0, 6, Math.max(1, x1 - x0), h - 24);
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
      if (data.bounds) bounds = data.bounds;
      draw();
    } catch (error) {
      console.warn('timeline fetch failed', error);
    }
  }

  function hasFootageAt(ts) {
    return ranges.some(r => ts >= r.start && ts < r.end);
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
    downloadBtn.disabled = false;
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
  // for export. Holding Ctrl (or Cmd) instead turns the drag into a scrub —
  // the video follows the cursor so you can sweep through footage to find a
  // moment.
  const DRAG_THRESHOLD = 4;
  let press = null;  // { x, time, dragging, scrub }

  const localX = event => event.clientX - canvas.getBoundingClientRect().left;
  // Ctrl or Cmd triggers scrub. On macOS Ctrl+click is delivered as a
  // right-click (button 2), so treat the secondary button as scrub too, and
  // suppress the context menu that would otherwise eat the drag.
  const isScrubKey = event =>
    event.ctrlKey || event.metaKey || event.button === 2 || event.buttons === 2;
  canvas.addEventListener('contextmenu', e => e.preventDefault());

  // Scrubbing that stays inside the loaded 5-min chunk seeks instantly; moving
  // outside it loads the chunk under the cursor, throttled so a fast drag
  // doesn't fire a request every pixel.
  let lastScrubLoad = 0;
  function scrubTo(t) {
    t = Math.max(bounds.start, Math.min(bounds.end, t));
    if (chunkStart !== null && t >= chunkStart && t < chunkStart + CHUNK_SECONDS
        && video.readyState >= 1) {
      video.currentTime = Math.max(0, t - chunkStart);
    } else {
      const now = (window.performance && performance.now()) || Date.now();
      if (now - lastScrubLoad > 180) {
        lastScrubLoad = now;
        playFrom(Math.floor(t), { announce: false });
      }
    }
    playheadLabel.textContent = `Scrubbing ${fmtFull(t)}`;
    draw();
  }

  canvas.addEventListener('pointerdown', event => {
    press = {
      x: localX(event), time: xToTime(localX(event)),
      dragging: false, scrub: isScrubKey(event),
    };
    // Capture so the drag keeps tracking even if the pointer leaves the canvas.
    try { canvas.setPointerCapture(event.pointerId); } catch (_) {}
    if (press.scrub) { event.preventDefault(); scrubTo(press.time); }
  });

  canvas.addEventListener('pointermove', event => {
    const x = localX(event);
    hoverX = x;
    canvas.title = fmtTime(xToTime(x));
    canvas.style.cursor =
      (press && press.scrub) || event.ctrlKey || event.metaKey ? 'ew-resize' : 'pointer';
    if (press) {
      if (Math.abs(x - press.x) > DRAG_THRESHOLD) press.dragging = true;
      if (press.scrub) scrubTo(xToTime(x));
      else if (press.dragging) selection = { start: press.time, end: xToTime(x) };
    }
    draw();
  });

  canvas.addEventListener('pointerup', event => {
    if (!press) return;
    if (press.scrub) {
      playFrom(Math.floor(xToTime(localX(event))));   // settle on release point
    } else if (press.dragging && selection) {
      // Normalise and clamp the selected span, then reveal the export bar.
      let [a, b] = [selection.start, selection.end].sort((x, y) => x - y);
      if (b - a > MAX_EXPORT) b = a + MAX_EXPORT;
      selection = { start: a, end: b };
      showSelection();
    } else {
      playFrom(Math.floor(press.time));               // a plain click seeks
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
  async function captureClip(download) {
    if (!selection) return;
    const el = document.getElementById('dewarp') || video;
    const grab = el.captureStream ? el.captureStream.bind(el)
      : el.mozCaptureStream ? el.mozCaptureStream.bind(el) : null;
    if (!grab || typeof MediaRecorder === 'undefined') {
      alert('This browser cannot capture clips.');
      return;
    }
    const start = Math.floor(selection.start);
    const secs = Math.max(1, Math.round(selection.end - selection.start));
    const mime = ['video/webm;codecs=vp8', 'video/webm', 'video/mp4']
      .find(m => MediaRecorder.isTypeSupported(m)) || '';

    const rec = new MediaRecorder(grab(25), mime ? { mimeType: mime } : undefined);
    const chunks = [];
    rec.ondataavailable = e => e.data && e.data.size && chunks.push(e.data);
    const finished = new Promise(res => { rec.onstop = res; });

    const label = document.getElementById('selection-label');
    const restore = label.textContent;
    let left = secs;
    label.textContent = `● Recording clip… ${left}s`;
    const ticker = setInterval(() => {
      left -= 1; label.textContent = `● Recording clip… ${Math.max(0, left)}s`;
    }, 1000);

    playFrom(start, { announce: false });
    await new Promise(r => setTimeout(r, 500));   // let playback settle
    rec.start();
    setTimeout(() => { if (rec.state !== 'inactive') rec.stop(); }, secs * 1000);
    await finished;

    clearInterval(ticker);
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

  document.getElementById('selection-save').addEventListener('click', () => captureClip(false));
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

  downloadBtn.addEventListener('click', () => {
    if (chunkStart === null) return;
    const at = Math.floor(chunkStart + (video.currentTime || 0));
    window.location = `/api/cameras/${encodeURIComponent(cameraId)}/clip.mp4`
      + `?start=${at}&duration=60`;
  });

  window.addEventListener('resize', resize);

  // Keep the window edge tracking newly recorded footage while idle.
  setInterval(() => {
    if (chunkStart === null) windowEnd = Math.max(windowEnd, Date.now() / 1000);
    loadCoverage();
  }, 30000);

  resize();
  loadCoverage();
}
