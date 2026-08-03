/*
 * Camera device controls: spotlight and night vision.
 *
 * Admin-only toolbar widgets on the camera page. On load we GET the current
 * state from /api/cameras/<id>/controls and reveal only the controls the
 * camera actually reports (a null control stays hidden). Each change POSTs to
 * the matching endpoint and reflects success/failure inline, then re-reads the
 * true state so the UI never drifts from the camera.
 *
 * Self-initialising: reads the camera id from #cam-controls[data-camera-id],
 * so it doesn't depend on the inline script's CAMERA_ID having run yet.
 */

(function () {
  const root = document.getElementById('cam-controls');
  if (!root) return;                       // not admin / not rendered
  const cameraId = root.dataset.cameraId;

  const lightBtn = document.getElementById('light-btn');
  const nvGroup = document.getElementById('nv-group');
  const nvMode = document.getElementById('nv-mode');
  const nvIr = document.getElementById('nv-ir');
  const statusEl = document.getElementById('cam-controls-status');

  let lightOn = null;
  let statusTimer = null;

  function setStatus(text, kind) {
    statusEl.textContent = text || '';
    statusEl.classList.toggle('is-error', kind === 'error');
    if (statusTimer) clearTimeout(statusTimer);
    if (text && kind !== 'error') {
      statusTimer = setTimeout(() => { statusEl.textContent = ''; }, 2500);
    }
  }

  function paintLight() {
    if (lightOn === null) return;
    lightBtn.setAttribute('aria-pressed', String(lightOn));
    lightBtn.classList.toggle('btn-primary', lightOn);
    lightBtn.textContent = lightOn ? '💡 Light on' : '💡 Light off';
  }

  function paintMode(mode) {
    [...nvMode.children].forEach(b =>
      b.classList.toggle('active', b.dataset.mode === mode));
  }

  // Apply a controls payload {light, night_vision:{mode, ir}} to the UI.
  function apply(state) {
    let anything = false;

    if (state && typeof state.light === 'boolean') {
      lightOn = state.light;
      lightBtn.hidden = false;
      paintLight();
      anything = true;
    }

    const nv = state && state.night_vision;
    if (nv && (nv.mode || nv.ir)) {
      nvGroup.hidden = false;
      anything = true;
      // A mode/ir the camera doesn't report leaves that sub-control blank
      // rather than lying about a value.
      nvMode.hidden = !nv.mode;
      if (nv.mode) paintMode(nv.mode);
      nvIr.hidden = !nv.ir;
      if (nv.ir) nvIr.value = nv.ir;
    }

    // Nothing supported: keep the whole cluster hidden.
    root.hidden = !anything;
    return anything;
  }

  async function loadState() {
    try {
      const r = await fetch(`/api/cameras/${cameraId}/controls`);
      if (!r.ok) { root.hidden = true; return; }
      apply(await r.json());
    } catch (_) {
      root.hidden = true;
    }
  }

  async function post(path, body, busyEls, pending) {
    busyEls.forEach(el => { el.disabled = true; });
    setStatus(pending, 'info');
    try {
      const r = await fetch(`/api/cameras/${cameraId}/${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        setStatus(data.error || 'Failed', 'error');
        return false;
      }
      setStatus('Done', 'ok');
      return true;
    } catch (_) {
      setStatus('Network error', 'error');
      return false;
    } finally {
      busyEls.forEach(el => { el.disabled = false; });
      // Re-read: the camera is the source of truth, and a partial failure
      // shouldn't leave the buttons showing a state that didn't take.
      loadState();
    }
  }

  lightBtn.addEventListener('click', () => {
    const next = !lightOn;
    post('light', { on: next }, [lightBtn], next ? 'Turning on…' : 'Turning off…');
  });

  nvMode.addEventListener('click', e => {
    const b = e.target.closest('[data-mode]');
    if (!b) return;
    post('nightvision', { mode: b.dataset.mode }, [...nvMode.children], 'Setting mode…');
  });

  nvIr.addEventListener('change', () => {
    post('nightvision', { ir: nvIr.value }, [nvIr], 'Setting IR…');
  });

  loadState();
})();
