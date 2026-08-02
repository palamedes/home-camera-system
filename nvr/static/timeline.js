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

function initHistory({ cameraId, bounds }) {
  const canvas = document.getElementById('timeline');
  const ctx = canvas.getContext('2d');
  const video = document.getElementById('video');
  const status = document.getElementById('status');
  const playheadLabel = document.getElementById('playhead-label');
  const spanSelect = document.getElementById('span');
  const downloadBtn = document.getElementById('download-btn');

  let windowEnd = bounds.end;
  let windowSpan = parseInt(spanSelect.value, 10);
  let ranges = [];
  let chunkStart = null;      // epoch seconds of the loaded chunk
  let hoverX = null;

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

  canvas.addEventListener('click', event => {
    const rect = canvas.getBoundingClientRect();
    playFrom(Math.floor(xToTime(event.clientX - rect.left)));
  });

  canvas.addEventListener('mousemove', event => {
    const rect = canvas.getBoundingClientRect();
    hoverX = event.clientX - rect.left;
    canvas.title = fmtTime(xToTime(hoverX));
    draw();
  });

  canvas.addEventListener('mouseleave', () => { hoverX = null; draw(); });

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
