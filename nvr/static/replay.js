/*
* Instant replay: drag back through the last few minutes on the live view
 * without leaving it. Hybrid of two sources so it works the instant the page
 * loads AND catches the last few seconds:
 *
 *   - Browser buffer (MediaRecorder): captures the live substream right up to
 *     the live edge, so it can replay the "wait, what was that?" moment the
 *     server recording can't. But it needs ~10s after page load to warm up.
 *   - Server recording (playback.mp4): available immediately for footage older
 *     than the recorder's ~60-70s indexing lag, so a fresh-loaded page can drag
 *     back a minute or two right away.
 *
 * replayAt() picks the buffer when it has the moment, else the server; between
 * them the drag bar is useful from the first second. The live view uses the
 * ~256 kbps substream, so a few minutes of buffer is only a few MB.
 *
 * Seekability: one continuous MediaRecorder blob can't be decoded from the
 * middle without its header, so we cut the buffer into short self-contained
 * segments (a fresh recorder every SEG_SECONDS) and keep the last few minutes.
 * Scrubbing seeks within a segment (instant) and hops across boundaries.
 */

function initReplay(opts) {
  const { video, scrub, label, container, cameraId } = opts;
  const SEG_SECONDS = 4;      // short segments -> the buffer's live edge is only
                              // ~4s behind, and it warms in ~4s after page load
  const KEEP_SECONDS = 210;   // retain ~3.5 min of segments
  const WINDOW = Number(scrub && scrub.max) || 180;
  const REVEAL_SPAN = 5;      // only show the bar once this many seconds of
                              // buffer exist, so a drag-back actually replays

  const supported = typeof MediaRecorder !== 'undefined' && scrub && label;
  if (!supported) { if (container) container.hidden = true; return null; }

  // Hide the scrubber until the buffer has actually recorded something (~2s
  // after the stream connects). Grabbing it before then does nothing, which
  // reads as broken; revealed on the first segment.
  if (container) container.hidden = true;
  let revealed = false;
  function reveal() {
    if (!revealed && container) { revealed = true; container.hidden = false; }
  }
  // Reveal only once there's enough buffered footage that dragging the bar back
  // lands on real video — showing it on the first ~1.5s segment let the user
  // grab a bar that still just sat on live.
  function maybeReveal() {
    if (revealed || !segments.length) return;
    if (segments[segments.length - 1].end - segments[0].start >= REVEAL_SPAN) reveal();
  }

  // The player has a fixed CSS height (.player-fixed), so swapping the <video>
  // source during replay can't collapse the box — no runtime size juggling.
  const mime = ['video/webm;codecs=vp8', 'video/webm', 'video/mp4']
    .find(m => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) || '';

  let segments = [];      // { start, end, url }
  let liveStream = null;
  let currentRec = null;
  let recStream = null;
  let segStartedAt = 0;
  let firstSeg = true;    // cut the very first segment fast so the buffer is
                          // scrubbable within ~2s of the stream connecting
  let mode = 'live';      // 'live' | 'replay' (browser buffer) | 'server'
  let current = null;     // buffer segment being played in replay
  let desired = null;     // latest requested { seg, offset }, applied when ready
  // Server-history playback, for footage older than the browser buffer holds.
  let serverStart = null; // epoch that server playback's currentTime 0 maps to
  let serverTarget = null;// latest drag target awaiting a (debounced) fetch
  let serverTimer = null;

  const now = () => Date.now() / 1000;

  function prune() {
    const cutoff = now() - KEEP_SECONDS;
    while (segments.length && segments[0].end < cutoff) {
      URL.revokeObjectURL(segments.shift().url);
    }
  }

  function stopSegment() {
    if (currentRec && currentRec.state !== 'inactive') {
      try { currentRec.stop(); } catch (_) {}
    }
  }

  function startSegment(stream) {
    let rec;
    try {
      rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    } catch (_) { if (container) container.hidden = true; return; }
    const start = now();
    const chunks = [];
    rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      if (chunks.length) {
        const blob = new Blob(chunks, { type: rec.mimeType || mime });
        segments.push({ start, end: now(), url: URL.createObjectURL(blob) });
        prune();
        maybeReveal();   // show the bar once there's enough to actually replay
      }
    };
    try { rec.start(); } catch (_) { if (container) container.hidden = true; return; }
    currentRec = rec; recStream = stream; segStartedAt = now();
  }

  // A 1-second heartbeat keeps one recorder running on the freshest live
  // stream, cutting a self-contained seekable segment every SEG_SECONDS. Driven
  // by a timer rather than chaining recorder.onstop so it self-heals when the
  // stream changes — toggling audio rebuilds the WebRTC session with a new
  // stream — or the recorder dies. It buffers even during replay: the live
  // tracks stay up on the WebRTC connection, so returning to live has no gap.
  function heartbeat() {
    if (mode === 'live') {
      const s = video.srcObject;
      if (s && s.getTracks && s.getTracks().length) liveStream = s;
    }
    if (!liveStream || !liveStream.getTracks().length) return;
    if (!currentRec || currentRec.state === 'inactive' || recStream !== liveStream) {
      stopSegment();
      startSegment(liveStream);
    } else if (now() - segStartedAt >= (firstSeg ? 1.5 : SEG_SECONDS)) {
      firstSeg = false;
      stopSegment();
      startSegment(liveStream);
    }
  }

  function fmt(behind) {
    if (behind < 1) return 'LIVE';
    const m = Math.floor(behind / 60), s = Math.floor(behind % 60);
    return `-${m}:${String(s).padStart(2, '0')}`;
  }

  function goLive() {
    mode = 'live';
    current = null;
    serverStart = null;
    serverTarget = null;
    if (serverTimer) { clearTimeout(serverTimer); serverTimer = null; }
    video.removeAttribute('src');
    try { video.load(); } catch (_) {}
    if (liveStream) { try { video.srcObject = liveStream; } catch (_) {} }
    // Don't force-mute: leave whatever the Audio button set, so returning from
    // a rewind keeps live audio if it was on.
    video.play().catch(() => {});
    label.textContent = 'LIVE';
    label.classList.remove('rewound');
  }

  function segmentAt(t) {
    for (const seg of segments) if (t >= seg.start && t < seg.end) return seg;
    return null;  // not in the browser buffer — replayAt() falls back to server
  }

  // Load a segment's blob into the player, but only when it actually changes.
  function ensureSegment(seg) {
    if (current === seg) return;
    current = seg;
    video.srcObject = null;
    video.src = seg.url;
    try { video.load(); } catch (_) {}
  }

  // Seek to the LATEST requested position, once the right segment is decodable.
  // A single source of truth (`desired`) plus one persistent 'loadedmetadata'
  // listener replaces the old per-call seek listeners: dragging fast across
  // segment boundaries used to pile up listeners, and a stale one (holding an
  // earlier position) could fire last and win — that was the flaky rewind.
  function applyDesired() {
    if (mode !== 'replay' || !desired) return;
    if (current !== desired.seg || video.readyState < 1) return;  // not ready yet
    try { video.currentTime = desired.offset; } catch (_) {}
    video.play().catch(() => {});
  }

  function replayAt(t) {
    // Never ask for footage newer than the buffer's newest closed segment: that
    // moment is still being recorded (no blob yet) and the server lags further,
    // so it would just hang on a black frame. Clamp to what's actually there.
    if (segments.length) {
      const newest = segments[segments.length - 1].end;
      if (t > newest) t = newest - 0.05;
    }
    const seg = segmentAt(t);
    if (seg) {
      // In the browser buffer: precise, instant seeking of the recent window.
      mode = 'replay';
      label.classList.add('rewound');
      if (serverTimer) { clearTimeout(serverTimer); serverTimer = null; }
      desired = { seg, offset: Math.max(0, Math.min(seg.end - seg.start - 0.05, t - seg.start)) };
      ensureSegment(seg);
      applyDesired();
      return;
    }
    if (!cameraId) { goLive(); return; }
    // Older than the browser buffer (or the buffer is still warming right after
    // page load): fall back to the server recording. It lags ~60-70s behind
    // live, so a very-recent t may have neither buffer nor server footage — the
    // request 404s and the error handler shows "no footage".
    mode = 'server';
    label.classList.add('rewound');
    serverTarget = t;
    // Debounce: a drag fires input on every pixel, but each server fetch spawns
    // an ffmpeg transcode. Only load once the drag settles, at wherever it lands.
    if (serverTimer) clearTimeout(serverTimer);
    serverTimer = setTimeout(loadServerChunk, 220);
  }

  function loadServerChunk() {
    serverTimer = null;
    if (mode !== 'server' || serverTarget == null) return;
    const t = serverTarget;
    const dur = Math.min(WINDOW + 30, Math.max(30, Math.ceil(now() - t + 5)));
    serverStart = t;
    current = null;
    desired = null;
    video.srcObject = null;
    video.src = `/api/cameras/${encodeURIComponent(cameraId)}/playback.mp4`
      + `?start=${Math.floor(t)}&duration=${dur}`;
    // Server footage is transcoded on demand (4K H.265 -> H.264 can take a
    // couple seconds to start), so show it's working instead of a mystery black
    // frame. timeupdate replaces this the moment footage actually plays.
    label.textContent = '⏳';
    try { video.load(); } catch (_) {}
    video.play().catch(() => {});
  }

  video.addEventListener('loadedmetadata', applyDesired);

  // A server chunk with no footage (target inside the recorder's lag window, or
  // older than retention) errors instead of ever delivering a frame. Don't hang
  // on black — snap back to live.
  video.addEventListener('error', () => {
    if (mode === 'server') { goLive(); scrub.value = WINDOW; }
  });

  scrub.addEventListener('input', () => {
    const behind = WINDOW - Number(scrub.value);
    label.textContent = fmt(behind);
    if (behind < 1) goLive();
    else replayAt(now() - behind);
  });

  // Play forward across segment boundaries during replay; catching up to live
  // returns to the live stream and snaps the scrubber back to the right.
  video.addEventListener('ended', () => {
    if (mode === 'replay') {
      const next = current ? segmentAt(current.end + 0.1) : null;
      if (next && next !== current) { replayAt(next.start); return; }
    }
    if (mode === 'replay' || mode === 'server') {
      // Caught up to the end of the buffer/server window — snap back to live.
      goLive();
      scrub.value = WINDOW;
    }
  });

  // Keep the scrubber and label reflecting the current playback position, for
  // either playback source.
  video.addEventListener('timeupdate', () => {
    let playedAt = null;
    if (mode === 'replay' && current) playedAt = current.start + video.currentTime;
    else if (mode === 'server' && serverStart != null) playedAt = serverStart + video.currentTime;
    if (playedAt == null) return;
    const behind = now() - playedAt;
    scrub.value = Math.max(0, WINDOW - behind);
    label.textContent = fmt(behind);
  });

  const beat = setInterval(heartbeat, 500);
  heartbeat();
  return { goLive, stop() { clearInterval(beat); stopSegment(); } };
}

window.initReplay = initReplay;
