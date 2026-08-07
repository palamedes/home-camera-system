/*
 * Push-to-talk (two-way audio).
 *
 * Hold the "🎤 Talk" button to capture the admin's microphone and push it to
 * the camera's speaker through go2rtc's RTSP/ONVIF backchannel. This is the
 * send-only mirror of the live view: instead of receiving the camera's audio,
 * the browser *sends* a microphone track, and go2rtc routes it to the camera's
 * talk-back channel.
 *
 * Signalling reuses the exact same authenticated path as live video — a single
 * POST of a WebRTC offer to /go2rtc/api/webrtc?src=<stream> — so there is no
 * second channel to secure. The offer carries one sendonly audio transceiver
 * (the mic) and nothing else; go2rtc answers with a receiver wired to the
 * backchannel. This mirrors go2rtc's own microphone button
 * (addTransceiver(track, {direction: 'sendonly'})).
 *
 * The talk session uses a SEPARATE RTCPeerConnection and a SEPARATE go2rtc
 * stream (<camera>_talk) from the live view, so establishing or tearing down
 * the backchannel can never disturb live playback.
 *
 * Cameras without a backchannel: the session either fails to connect or
 * connects but carries no audio destination. We surface a clear inline message
 * and, either way, always release the microphone.
 */
(function () {
  const btn = document.getElementById('talk-btn');
  if (!btn) return;   // not an admin — the button isn't rendered

  const note = document.getElementById('talk-note');
  // Server-rendered stream name is authoritative; fall back to the page's
  // CAMERA_ID (defined by the inline script) if the attribute is ever missing.
  const stream = btn.dataset.stream
    || (typeof CAMERA_ID !== 'undefined' ? CAMERA_ID + '_talk' : null);

  const IDLE_LABEL = '🎤 Talk';

  let pc = null;
  let micStream = null;
  // A monotonically increasing token: every start()/stop() pair bumps it so a
  // slow getUserMedia/offer that resolves after the user already let go can
  // tell it lost and bail (and never leave the mic hot).
  let epoch = 0;
  let live = false;

  function setNote(message, kind) {
    if (!note) return;
    note.textContent = message || '';
    note.classList.toggle('talk-error', kind === 'error');
    note.classList.toggle('muted', kind !== 'error');
  }

  async function waitForIce(peer, timeoutMs = 2500) {
    if (peer.iceGatheringState === 'complete') return;
    await new Promise(resolve => {
      const done = () => {
        peer.removeEventListener('icegatheringstatechange', check);
        clearTimeout(timer);
        resolve();
      };
      const check = () => { if (peer.iceGatheringState === 'complete') done(); };
      const timer = setTimeout(done, timeoutMs);   // ship whatever we have
      peer.addEventListener('icegatheringstatechange', check);
    });
  }

  // Answer SDP with the camera's talk-back rejected (port 0) or marked
  // inactive means go2rtc found no backchannel to hand our microphone to.
  function backchannelRejected(sdp) {
    const audio = /\r?\nm=audio (\d+)[^\r\n]*/.exec('\n' + sdp);
    if (audio && audio[1] === '0') return true;      // m=audio 0 ...
    return /\r?\na=inactive/.test(sdp) && !/\r?\na=(recvonly|sendrecv)/.test(sdp);
  }

  function stopMic() {
    if (micStream) {
      micStream.getTracks().forEach(t => { try { t.stop(); } catch (_) {} });
      micStream = null;
    }
  }

  function teardown() {
    stopMic();
    if (pc) { try { pc.close(); } catch (_) {} pc = null; }
    live = false;
  }

  function resetButton() {
    btn.classList.remove('talk-live');
    btn.setAttribute('aria-pressed', 'false');
    btn.textContent = IDLE_LABEL;
  }

  // Pin the mic transceiver to G.711 (PCMU/PCMA). Camera backchannels (Reolink
  // and most RTSP cameras) only speak G.711, not Opus. Left to itself the
  // browser offers Opus first, go2rtc answers Opus, and — since go2rtc does not
  // transcode the send direction — the camera receives nothing (silent talk).
  // Forcing G.711 makes the whole path PCMU/PCMA end to end, no transcode.
  // Degrades silently on browsers without setCodecPreferences.
  function preferG711(transceiver) {
    try {
      if (!transceiver || !transceiver.setCodecPreferences) return;
      const caps = RTCRtpSender.getCapabilities('audio');
      if (!caps || !caps.codecs) return;
      const g711 = caps.codecs.filter(c =>
        c.mimeType === 'audio/PCMU' || c.mimeType === 'audio/PCMA');
      // Keep DTMF/comfort-noise so negotiation stays well-formed; drop Opus.
      const extras = caps.codecs.filter(c =>
        c.mimeType === 'audio/telephone-event' || c.mimeType === 'audio/CN');
      if (g711.length) transceiver.setCodecPreferences([...g711, ...extras]);
    } catch (_) { /* older browser — fall back to default negotiation */ }
  }

  async function start() {
    if (live || !stream) return;
    const mine = ++epoch;
    live = true;
    btn.classList.add('talk-live');
    btn.setAttribute('aria-pressed', 'true');
    btn.textContent = '🎤 Connecting…';
    setNote('');

    // 1) Microphone. Requires a secure context (HTTPS or localhost).
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      if (mine !== epoch) { stopMic(); return; }   // already released
      teardown();
      resetButton();
      const secure = window.isSecureContext;
      setNote(
        secure ? 'Microphone blocked — allow access and try again.'
               : 'Two-way talk needs HTTPS (or localhost) to use the microphone.',
        'error',
      );
      return;
    }
    // Released during the permission prompt — don't open a session.
    if (mine !== epoch) { stopMic(); return; }

    // 2) WebRTC offer: one sendonly mic track, POSTed through the proxy.
    try {
      pc = new RTCPeerConnection({ iceServers: [], bundlePolicy: 'max-bundle' });
      // go2rtc only bridges the mic into the camera's RTSP backchannel when the
      // mic rides on a connection that is ALSO consuming the stream — exactly
      // what go2rtc's own web client does (video,audio,microphone on one peer
      // connection). A mic-only sendonly connection makes go2rtc open the
      // backchannel but never attach our audio to it (the stream shows
      // consumers: null). So consume the stream's video+audio (recvonly, which
      // we just discard) alongside the sendonly mic. Our <camera>_talk stream is
      // the sub stream + #backchannel=1, so it carries all three.
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });
      micStream.getTracks().forEach(track => {
        const transceiver = pc.addTransceiver(track, { direction: 'sendonly' });
        preferG711(transceiver);
      });

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitForIce(pc);
      if (mine !== epoch) { teardown(); return; }

      const response = await fetch(
        `/go2rtc/api/webrtc?src=${encodeURIComponent(stream)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: 'offer', sdp: pc.localDescription.sdp }),
        },
      );
      if (mine !== epoch) { teardown(); return; }
      if (!response.ok) throw new Error(`signalling failed: ${response.status}`);

      const answer = await response.json();
      if (!answer || !answer.sdp) throw new Error('no answer from stream backend');
      if (backchannelRejected(answer.sdp)) {
        teardown();
        resetButton();
        setNote('This camera does not support two-way audio.', 'error');
        return;
      }
      await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp });
      if (mine !== epoch) { teardown(); return; }

      btn.textContent = '🎤 Talking…';
      setNote('On air — hold to keep talking.', 'info');

      pc.addEventListener('connectionstatechange', () => {
        if (mine !== epoch) return;
        if (pc.connectionState === 'connected') {
          setNote('On air — hold to keep talking.', 'info');
        } else if (['failed', 'closed'].includes(pc.connectionState)) {
          if (live) {   // dropped while still held
            teardown();
            resetButton();
            setNote('Talk connection lost.', 'error');
          }
        }
      });
    } catch (err) {
      if (mine !== epoch) { teardown(); return; }
      console.warn('talk session failed:', err);
      teardown();
      resetButton();
      setNote('Could not start two-way audio for this camera.', 'error');
    }
  }

  function stop() {
    // Bump the epoch first so any in-flight start() bails and never revives the
    // mic. Then release everything.
    epoch++;
    const wasLive = live;
    teardown();
    resetButton();
    if (wasLive && note && !note.classList.contains('talk-error')) setNote('');
  }

  // ---- push-to-talk wiring -------------------------------------------------
  // Pointer events cover mouse, touch and pen with one path. Capturing the
  // pointer means the release still fires even if the finger slides off the
  // button, so the mic can't get stuck on.
  btn.addEventListener('pointerdown', event => {
    event.preventDefault();
    try { btn.setPointerCapture(event.pointerId); } catch (_) {}
    start();
  });
  const release = () => stop();
  btn.addEventListener('pointerup', release);
  btn.addEventListener('pointercancel', release);
  // Long-press context menu (mobile) would strand a hot mic — suppress it.
  btn.addEventListener('contextmenu', e => e.preventDefault());

  // Keyboard accessibility: hold Space/Enter to talk, release to stop. Buttons
  // synthesise a click on keydown, which would otherwise toggle nothing useful
  // for push-to-talk, so we drive it off keydown/keyup directly.
  btn.addEventListener('keydown', event => {
    if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) {
      event.preventDefault();
      start();
    }
  });
  btn.addEventListener('keyup', event => {
    if (event.key === ' ' || event.key === 'Enter') {
      event.preventDefault();
      stop();
    }
  });
  btn.addEventListener('click', event => event.preventDefault());

  // Never leave the mic live if focus/window/page goes away mid-press.
  btn.addEventListener('blur', stop);
  window.addEventListener('blur', stop);
  window.addEventListener('pagehide', stop);
})();
