/*
 * Crop ("digital-zoom") virtual cameras for ordinary, non-fisheye cameras.
 * A crop virtual is just a normalised {x,y,w,h} sub-rectangle of the parent
 * frame — no dewarping, so it renders with a plain 2D canvas (much lighter than
 * the fisheye WebGL path). We never record virtuals; the parent's real stream
 * is what's on disk. This module has two jobs:
 *
 *   initCropTile(video, canvas, crop)  — continuously paint the sub-rectangle
 *     of a playing <video> into a <canvas>. Used for grid/wall tiles and for
 *     the full live view of a crop virtual.
 *   initCropEditor({cameraId, snapshotUrl}) — a modal that lets an admin draw a
 *     box on a still snapshot and save it as a new crop virtual.
 */
(function () {
  const clamp01 = (n) => Math.max(0, Math.min(1, Number(n) || 0));

  function initCropTile(video, canvas, crop) {
    const ctx = canvas.getContext('2d', { alpha: false });
    crop = crop || {};
    const cx = clamp01(crop.x), cy = clamp01(crop.y);
    const cw = clamp01(crop.w) || 1, ch = clamp01(crop.h) || 1;
    let raf = 0, stopped = false;

    function draw() {
      if (stopped) return;
      const vw = video.videoWidth, vh = video.videoHeight;
      if (vw && vh) {
        const dw = canvas.clientWidth || vw, dh = canvas.clientHeight || vh;
        if (canvas.width !== dw || canvas.height !== dh) {
          canvas.width = dw; canvas.height = dh;
        }
        const sx = cx * vw, sy = cy * vh;
        const sw = Math.max(1, cw * vw), sh = Math.max(1, ch * vh);
        try { ctx.drawImage(video, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height); }
        catch (_) { /* video not yet decodable */ }
      }
      raf = requestAnimationFrame(draw);
    }
    canvas.hidden = false;
    raf = requestAnimationFrame(draw);
    return { stop() { stopped = true; cancelAnimationFrame(raf); } };
  }

  // --- creation modal: draw a box on a snapshot, save as a crop virtual ------
  function initCropEditor(opts) {
    const cameraId = opts.cameraId;
    const snapshotUrl = opts.snapshotUrl
      || `/api/cameras/${encodeURIComponent(cameraId)}/snapshot.jpg`;

    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML =
      '<div class="modal crop-modal">'
      + '<div class="modal-head"><h2>New cropped view</h2>'
      + '<button class="modal-close" type="button" aria-label="Close">&times;</button></div>'
      + '<div class="modal-body">'
      + '<p class="muted small" style="margin:0 0 10px">Drag a box over the area you want as its own camera. The full camera is still what gets recorded.</p>'
      + '<div class="crop-stage"><img alt="camera snapshot" draggable="false">'
      + '<div class="crop-sel" hidden></div></div>'
      + '<div class="row" style="gap:10px;margin-top:12px;align-items:center;flex-wrap:wrap">'
      + '<input type="text" class="rec-select" placeholder="Name (e.g. Front door)" style="flex:1;min-width:160px" maxlength="60">'
      + '<span class="small muted crop-hint">Draw a box to begin</span>'
      + '<button class="btn" data-cancel type="button">Cancel</button>'
      + '<button class="btn btn-primary" data-save type="button" disabled>Save view</button>'
      + '</div></div></div>';
    document.body.appendChild(backdrop);

    const img = backdrop.querySelector('img');
    const stage = backdrop.querySelector('.crop-stage');
    const selBox = backdrop.querySelector('.crop-sel');
    const nameInput = backdrop.querySelector('input');
    const saveBtn = backdrop.querySelector('[data-save]');
    const hint = backdrop.querySelector('.crop-hint');
    // Cache-bust so we get a fresh frame each time the editor opens.
    img.src = snapshotUrl + (snapshotUrl.includes('?') ? '&' : '?') + 't=' + Date.now();

    let rect = null;                 // normalised {x,y,w,h}
    let drag = null;                 // { x0, y0 } in stage px

    const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    backdrop.querySelector('.modal-close').addEventListener('click', close);
    backdrop.querySelector('[data-cancel]').addEventListener('click', close);
    backdrop.addEventListener('pointerdown', (e) => { if (e.target === backdrop) close(); });

    // The <img> shows the whole frame with no object-fit trickery, so its
    // bounding box maps straight to [0,1] of the source — exact coordinates.
    function paint() {
      if (!rect) { selBox.hidden = true; return; }
      const b = img.getBoundingClientRect();
      const s = stage.getBoundingClientRect();
      selBox.hidden = false;
      selBox.style.left = (b.left - s.left + rect.x * b.width) + 'px';
      selBox.style.top = (b.top - s.top + rect.y * b.height) + 'px';
      selBox.style.width = (rect.w * b.width) + 'px';
      selBox.style.height = (rect.h * b.height) + 'px';
    }

    function norm(clientX, clientY) {
      const b = img.getBoundingClientRect();
      return {
        x: clamp01((clientX - b.left) / b.width),
        y: clamp01((clientY - b.top) / b.height),
      };
    }

    img.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      const p = norm(e.clientX, e.clientY);
      drag = { x0: p.x, y0: p.y };
      try { img.setPointerCapture(e.pointerId); } catch (_) {}
    });
    img.addEventListener('pointermove', (e) => {
      if (!drag) return;
      const p = norm(e.clientX, e.clientY);
      rect = {
        x: Math.min(drag.x0, p.x), y: Math.min(drag.y0, p.y),
        w: Math.abs(p.x - drag.x0), h: Math.abs(p.y - drag.y0),
      };
      paint();
    });
    img.addEventListener('pointerup', () => {
      drag = null;
      const ok = rect && rect.w > 0.02 && rect.h > 0.02;
      saveBtn.disabled = !ok;
      hint.textContent = ok ? 'Looks good — name it and save' : 'Box too small — draw again';
      if (!ok) { rect = null; paint(); }
    });
    window.addEventListener('resize', paint);
    img.addEventListener('load', paint);

    saveBtn.addEventListener('click', async () => {
      if (!rect) return;
      const name = (nameInput.value || '').trim();
      if (!name) { nameInput.focus(); hint.textContent = 'Give it a name first'; return; }
      saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
      try {
        const r = await fetch(`/api/cameras/${encodeURIComponent(cameraId)}/virtual`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name, mode: 'crop',
            calib: { x: rect.x, y: rect.y, w: rect.w, h: rect.h },
          }),
        });
        if (!r.ok) throw new Error('save failed');
        const data = await r.json();
        // Jump straight to the new view so you see it immediately.
        location.href = `/cameras/${encodeURIComponent(cameraId)}?vcam=${data.id}`;
      } catch (_) {
        saveBtn.disabled = false; saveBtn.textContent = 'Save view';
        hint.textContent = 'Save failed — try again';
      }
    });
  }

  window.initCropTile = initCropTile;
  window.initCropEditor = initCropEditor;
})();
