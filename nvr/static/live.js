/*
 * Live video.
 *
 * WebRTC first: it is the only transport that gets sub-second latency out of
 * go2rtc without transcoding, which matters because the whole point of the
 * live view is watching something happen now.
 *
 * The offer is sent in one shot after ICE gathering finishes rather than
 * trickling candidates, because that turns the whole negotiation into a single
 * POST — which is what lets live video ride through the same authenticated
 * proxy as everything else, with no second channel to secure.
 *
 * MJPEG is the fallback. It is heavier on the wire and has no audio, but it
 * works on anything with an <img> tag, including browsers that refuse the
 * camera's codec outright.
 */

async function waitForIce(pc, timeoutMs = 2500) {
  if (pc.iceGatheringState === 'complete') return;
  await new Promise(resolve => {
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check);
      clearTimeout(timer);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === 'complete') done(); };
    // Some networks never reach 'complete'; ship whatever we have by then.
    const timer = setTimeout(done, timeoutMs);
    pc.addEventListener('icegatheringstatechange', check);
  });
}

async function tryWebRTC({ stream, video, audio = true }) {
  const pc = new RTCPeerConnection({ iceServers: [], bundlePolicy: 'max-bundle' });

  const media = new MediaStream();
  pc.addTransceiver('video', { direction: 'recvonly' });
  // Grid tiles are silent, so they skip the audio transceiver entirely.
  if (audio) pc.addTransceiver('audio', { direction: 'recvonly' });
  pc.addEventListener('track', event => {
    media.addTrack(event.track);
    video.srcObject = media;
  });

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await waitForIce(pc);

  const response = await fetch(`/go2rtc/api/webrtc?src=${encodeURIComponent(stream)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'offer', sdp: pc.localDescription.sdp }),
  });
  if (!response.ok) {
    pc.close();
    throw new Error(`signalling failed: ${response.status}`);
  }

  const answer = await response.json();
  if (!answer || !answer.sdp) {
    pc.close();
    throw new Error('no answer from stream backend');
  }
  await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp });

  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('connection timed out')), 12000);
    pc.addEventListener('connectionstatechange', () => {
      if (pc.connectionState === 'connected') { clearTimeout(timer); resolve(); }
      if (['failed', 'closed'].includes(pc.connectionState)) {
        clearTimeout(timer);
        reject(new Error(`connection ${pc.connectionState}`));
      }
    });
  });

  status.classList.add('hidden');
  return pc;
}

function useMjpeg({ stream, video, fallback, status, modeLabel }) {
  video.hidden = true;
  fallback.hidden = false;
  fallback.src = `/go2rtc/api/stream.mjpeg?src=${encodeURIComponent(stream)}`;
  fallback.addEventListener('load', () => status.classList.add('hidden'), { once: true });
  fallback.addEventListener('error', () => {
    status.classList.remove('hidden');
    status.textContent = 'Stream unavailable. The camera may be offline.';
  });
  if (modeLabel) modeLabel.textContent = 'MJPEG fallback';
}

async function startLive(options, attempt = 0) {
  const { status, modeLabel } = options;
  try {
    const pc = await tryWebRTC(options);
    if (modeLabel) modeLabel.textContent = 'WebRTC';

    // A stream that drops mid-session should recover on its own — cameras on
    // WiFi do this routinely and nobody is around to hit reload.
    pc.addEventListener('connectionstatechange', () => {
      if (['failed', 'disconnected'].includes(pc.connectionState)) {
        status.classList.remove('hidden');
        status.innerHTML = '<span class="spinner"></span>';
        pc.close();
        setTimeout(() => startLive(options), 2000);
      }
    });
  } catch (error) {
    // go2rtc connects to the camera on demand, so the very first offer after a
    // page load can arrive before the producer is warm and fail once. Retry a
    // couple of times before falling back — this is what a manual reload was
    // doing by hand.
    if (attempt < 2) {
      console.warn(`WebRTC attempt ${attempt + 1} failed (${error.message}); retrying`);
      status.classList.remove('hidden');
      status.innerHTML = '<span class="spinner"></span>';
      setTimeout(() => startLive(options, attempt + 1), 1500);
      return;
    }
    console.warn('WebRTC unavailable, falling back to MJPEG:', error.message);
    useMjpeg(options);
  }
}

/*
 * Live grid tile.
 *
 * A muted, low-res WebRTC view for dashboard/overview tiles. Uses the
 * substream so a wall of cameras stays cheap — the sub is a fraction of the
 * main stream's pixels and rides through as H.264 with no server transcode.
 *
 * The still snapshot stays as the poster and as the visible state whenever the
 * connection is down, so a tile degrades to the old behaviour rather than
 * going black. It self-heals: a dropped tile keeps retrying quietly.
 */
function initLiveTile(video, { stream, poster }) {
  video.muted = true;
  video.playsInline = true;
  video.autoplay = true;
  if (poster) video.poster = poster;

  let disposed = false;
  let timer = null;

  async function connect() {
    if (disposed) return;
    try {
      const pc = await tryWebRTC({ stream, video, audio: false });
      video.classList.add('tile-live');
      pc.addEventListener('connectionstatechange', () => {
        if (['failed', 'disconnected', 'closed'].includes(pc.connectionState)) {
          video.classList.remove('tile-live');
          pc.close();
          if (!disposed) timer = setTimeout(connect, 4000);
        }
      });
    } catch (error) {
      // Stay on the poster and try again later; go2rtc may be warming the sub.
      if (!disposed) timer = setTimeout(connect, 6000);
    }
  }

  connect();

  // Pause tiles while the tab is hidden so we are not decoding video nobody is
  // looking at; resume on return.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      disposed = true;
      clearTimeout(timer);
    } else if (disposed) {
      disposed = false;
      connect();
    }
  });
}

function initLiveTiles(root = document) {
  root.querySelectorAll('video[data-live-tile]').forEach(video => {
    initLiveTile(video, {
      stream: video.dataset.stream,
      poster: video.dataset.poster || undefined,
    });
  });
}
