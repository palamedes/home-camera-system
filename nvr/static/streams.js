// Per-camera recording-stream control on the settings page.
//
// Two jobs, both progressive-enhancement over the server-rendered row:
//   1. Persist the Main/Sub record-stream choice (PATCH record_stream).
//   2. Asynchronously label each option with its real resolution, fetched from
//      GET /api/cameras/<id>/streams — and, for Reolink cameras that advertise
//      encoder options, render a small resolution/bitrate control that writes
//      back via POST /api/cameras/<id>/encoder.
//
// Anything that fails degrades quietly: a stream whose resolution can't be
// probed simply keeps its plain "Main"/"Sub" label.
(function () {
  const times = '×'; // ×

  function resLabel(info) {
    return info && info.w && info.h ? `${info.w}${times}${info.h}` : null;
  }

  document.querySelectorAll('.list-cameras [data-camera]').forEach((row) => {
    const id = row.dataset.camera;
    const select = row.querySelector('[data-record-stream]');
    const meta = row.querySelector('[data-encoder]');
    const box = row.querySelector('[data-streams]');
    if (!select) return;

    // (1) Persist the record-stream choice; revert the picker on failure.
    let original = select.value;
    select.addEventListener('change', async () => {
      select.disabled = true;
      try {
        const r = await fetch(`/api/cameras/${encodeURIComponent(id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ record_stream: select.value }),
        });
        if (r.ok) original = select.value;
        else select.value = original;
      } catch {
        select.value = original;
      } finally {
        select.disabled = false;
      }
    });

    // (2) Fill labels / encoder UI (best-effort, non-blocking).
    fillStreams(id, select, meta, box && box.dataset.brand);
  });

  async function fillStreams(id, select, meta, brand) {
    let data;
    try {
      const r = await fetch(`/api/cameras/${encodeURIComponent(id)}/streams`);
      if (!r.ok) return;
      data = await r.json();
    } catch {
      return;
    }

    for (const opt of select.options) {
      const res = resLabel(data[opt.value]);
      const base = opt.value === 'sub' ? 'Sub' : 'Main';
      opt.textContent = res ? `${base} (${res})` : base;
    }

    if (meta && brand === 'reolink' && data.encoder && data.encoder.options) {
      buildEncoder(id, meta, data);
    } else if (meta) {
      // Non-Reolink: show the sub resolution inline so both are visible at once.
      const sub = resLabel(data.sub);
      if (sub) meta.textContent = `Sub ${sub}`;
    }
  }

  // ---- Reolink encoder control ------------------------------------------

  function buildEncoder(id, meta, data) {
    const options = data.encoder.options; // { main:{sizes,bitrates}, sub:{...} }
    const streamsWithOpts = ['main', 'sub'].filter(
      (s) => options[s] && ((options[s].sizes || []).length || (options[s].bitrates || []).length)
    );
    if (!streamsWithOpts.length) return;

    const details = document.createElement('details');
    details.className = 'encoder-ctl';
    const summary = document.createElement('summary');
    summary.textContent = 'Camera encoder';
    details.appendChild(summary);

    for (const stream of streamsWithOpts) {
      details.appendChild(streamControls(id, stream, options[stream], data[stream] || {}));
    }
    meta.appendChild(details);
  }

  function streamControls(id, stream, opts, current) {
    const wrap = document.createElement('div');
    wrap.className = 'encoder-row';

    const label = document.createElement('span');
    label.className = 'encoder-label';
    label.textContent = stream === 'sub' ? 'Sub' : 'Main';
    wrap.appendChild(label);

    const curSize = current.w && current.h ? `${current.w}*${current.h}` : '';
    const resSel = buildSelect(
      (opts.sizes || []).map((s) => [s, s.replace('*', times)]),
      curSize
    );
    const brSel = buildSelect(
      (opts.bitrates || []).map((b) => [String(b), `${b} kbps`]),
      current.bitrate != null ? String(current.bitrate) : ''
    );

    if (resSel) wrap.appendChild(resSel);
    if (brSel) wrap.appendChild(brSel);

    const apply = document.createElement('button');
    apply.type = 'button';
    apply.className = 'btn btn-sm';
    apply.textContent = 'Apply';
    apply.addEventListener('click', async () => {
      const body = { stream };
      if (resSel && resSel.value) body.resolution = resSel.value;
      if (brSel && brSel.value) body.bitrate = parseInt(brSel.value, 10);
      if (!body.resolution && !body.bitrate) return;
      apply.disabled = true;
      const prev = apply.textContent;
      apply.textContent = 'Applying…';
      try {
        const r = await fetch(`/api/cameras/${encodeURIComponent(id)}/encoder`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (r.ok) {
          apply.textContent = 'Applied';
          setTimeout(() => { apply.textContent = prev; }, 1500);
        } else {
          const d = await r.json().catch(() => ({}));
          alert(d.error || 'Could not change the encoder.');
          apply.textContent = prev;
        }
      } catch {
        alert('Network error changing the encoder.');
        apply.textContent = prev;
      } finally {
        apply.disabled = false;
      }
    });
    wrap.appendChild(apply);
    return wrap;
  }

  // A <select> from [value, text] pairs, or null when there are no options.
  function buildSelect(pairs, selected) {
    if (!pairs.length) return null;
    const sel = document.createElement('select');
    sel.className = 'rec-select';
    for (const [value, text] of pairs) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = text;
      if (value === selected) opt.selected = true;
      sel.appendChild(opt);
    }
    // If the current value wasn't among the advertised options, surface it so
    // the admin isn't misled into thinking a different value is active.
    if (selected && !pairs.some(([v]) => v === selected)) {
      const opt = document.createElement('option');
      opt.value = selected;
      opt.textContent = `${selected.replace('*', times)} (current)`;
      opt.selected = true;
      sel.insertBefore(opt, sel.firstChild);
    }
    return sel;
  }
})();
