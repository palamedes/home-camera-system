/*
 * Instant replay: an in-browser rolling buffer of the live stream, so you can
 * drag back through the last few minutes on the live view without leaving it.
 *
 * Why not the server recording: the recorder only indexes footage after a
 * segment closes (a minute or two behind live), so it can't show the last few
 * seconds — exactly the "wait, what was that?" window. MediaRecorder captures
 * the stream the browser is already receiving, right up to the live edge, and
 * the live view uses the ~256 kbps substream so a few minutes is only a few MB.
 *
 * Seekability: one continuous MediaRecorder blob can't be decoded from the
 * middle without its header, so we cut the buffer into short self-contained
 * segments (a fresh recorder every SEG_SECONDS) and keep the last few minutes.
 * Scrubbing seeks within a segment (instant) and hops across boundaries.
 */

function initReplay(opts) {
  const { video, scrub, label, container } = opts;
  const SEG_SECONDS = 12;
  const KEEP_SECONDS = 210;   // retain ~3.5 min of segments
  const WINDOW = Number(scrub && scrub.max) || 180;

  const supported = typeof MediaRecorder !== 'undefined' && scrub && label;
  if (!supported) { if (container) container.hidden = true; return null; }

  // The player has a fixed CSS height (.player-fixed), so swapping the <video>
  // source during replay can't collapse the box — no runtime size juggling.
  const mime = ['video/webm;codecs=vp8', 'video/webm', 'video/mp4']
    .find(m => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) || '';

  let segments = [];      // { start, end, url }
  let liveStream = null;
  let currentRec = null;
  let recStream = null;
  let segStartedAt = 0;
  let mode = 'live';      // 'live' | 'replay'
  let current = null;     // segment being played in replay

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
    } else if (now() - segStartedAt >= SEG_SECONDS) {
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
    if (segments.length && t < segments[0].start) return segments[0];  // clamp old
    return null;
  }

  function replayAt(t) {
    const seg = segmentAt(t);
    if (!seg) { goLive(); return; }
    mode = 'replay';
    label.classList.add('rewound');
    if (current !== seg) {
      current = seg;
      video.srcObject = null;
      video.src = seg.url;
      const seek = () => {
        video.currentTime = Math.max(0, Math.min(seg.end - seg.start - 0.05, t - seg.start));
        video.play().catch(() => {});
        video.removeEventListener('loadedmetadata', seek);
      };
      video.addEventListener('loadedmetadata', seek);
      try { video.load(); } catch (_) {}
    } else {
      try { video.currentTime = Math.max(0, t - seg.start); } catch (_) {}
    }
  }

  scrub.addEventListener('input', () => {
    const behind = WINDOW - Number(scrub.value);
    label.textContent = fmt(behind);
    if (behind < 1) goLive();
    else replayAt(now() - behind);
  });

  // Play forward across segment boundaries during replay; catching up to live
  // returns to the live stream and snaps the scrubber back to the right.
  video.addEventListener('ended', () => {
    if (mode !== 'replay') return;
    const next = current ? segmentAt(current.end + 0.1) : null;
    if (next && next !== current) {
      replayAt(next.start);
    } else {
      goLive();
      scrub.value = WINDOW;
    }
  });

  // Keep the scrubber label reflecting playback position while replaying.
  video.addEventListener('timeupdate', () => {
    if (mode !== 'replay' || !current) return;
    const behind = now() - (current.start + video.currentTime);
    scrub.value = Math.max(0, WINDOW - behind);
    label.textContent = fmt(behind);
  });

  const beat = setInterval(heartbeat, 1000);
  heartbeat();
  return { goLive, stop() { clearInterval(beat); stopSegment(); } };
}

window.initReplay = initReplay;
