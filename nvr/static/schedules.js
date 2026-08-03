// Per-camera schedule editor (Settings page). Each camera's <details> block
// holds a list of existing rules and a small add form. The server renders the
// initial list; this handles add/enable/delete against the API.
(function () {
  const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const ACTION_LABELS = { record: 'Record', light: 'Spotlight', nightvision: 'Night vision' };

  function toMinutes(value) {
    // "HH:MM" -> minutes past midnight.
    const [h, m] = (value || '').split(':').map(n => parseInt(n, 10));
    if (Number.isNaN(h) || Number.isNaN(m)) return null;
    return h * 60 + m;
  }

  function fmt(min) {
    const h = Math.floor(min / 60), m = min % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
  }

  function renderRow(camId, s) {
    const row = document.createElement('div');
    row.className = 'sched-row';
    row.dataset.sched = s.id;
    const days = DAY_NAMES.map((name, i) =>
      `<span class="${(s.days & (1 << i)) ? 'on' : 'off'}">${name}</span>`).join('');
    const nv = s.action === 'nightvision' ? `<span class="muted small">${s.value}</span>` : '';
    row.innerHTML = `
      <span class="sched-badge sched-${s.action}">${ACTION_LABELS[s.action] || s.action}</span>
      <span class="sched-when mono">${fmt(s.start_min)}–${fmt(s.end_min)}</span>
      <span class="sched-days">${days}</span>
      ${nv}
      <label class="checkbox sched-enabled">
        <input type="checkbox" data-sched-enabled ${s.enabled ? 'checked' : ''}>
        <span>On</span>
      </label>
      <button class="remove-x" data-sched-del title="Remove schedule" aria-label="Remove">&times;</button>`;
    wireRow(camId, row);
    return row;
  }

  function wireRow(camId, row) {
    const sid = row.dataset.sched;
    const toggle = row.querySelector('[data-sched-enabled]');
    if (toggle) {
      toggle.addEventListener('change', async () => {
        toggle.disabled = true;
        try {
          const r = await fetch(`/api/cameras/${encodeURIComponent(camId)}/schedules/${sid}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: toggle.checked }),
          });
          if (!r.ok) toggle.checked = !toggle.checked;
        } catch { toggle.checked = !toggle.checked; }
        finally { toggle.disabled = false; }
      });
    }
    const del = row.querySelector('[data-sched-del]');
    if (del) {
      del.addEventListener('click', async () => {
        if (!confirm('Remove this schedule?')) return;
        del.disabled = true;
        try {
          const r = await fetch(`/api/cameras/${encodeURIComponent(camId)}/schedules/${sid}`, {
            method: 'DELETE',
          });
          if (r.ok) row.remove();
          else del.disabled = false;
        } catch { del.disabled = false; }
      });
    }
  }

  document.querySelectorAll('[data-sched-camera]').forEach(block => {
    const camId = block.dataset.schedCamera;

    // Wire the rules the server already rendered.
    block.querySelectorAll('.sched-row').forEach(row => wireRow(camId, row));

    const form = block.querySelector('[data-sched-add]');
    if (!form) return;
    const actionSel = form.querySelector('[data-field="action"]');
    const nvGroup = form.querySelector('.sched-nv');

    // The night-vision "mode" picker only makes sense for nightvision rules.
    const syncNv = () => { nvGroup.hidden = actionSel.value !== 'nightvision'; };
    actionSel.addEventListener('change', syncNv);
    syncNv();

    form.addEventListener('submit', async e => {
      e.preventDefault();
      const action = actionSel.value;
      const start = toMinutes(form.querySelector('[data-field="start"]').value);
      const end = toMinutes(form.querySelector('[data-field="end"]').value);
      if (start === null || end === null) { alert('Enter a start and end time.'); return; }
      if (start === end) { alert('Start and end must differ.'); return; }

      let days = 0;
      form.querySelectorAll('[data-day]').forEach(cb => {
        if (cb.checked) days |= (1 << parseInt(cb.dataset.day, 10));
      });
      if (days === 0) { alert('Select at least one day.'); return; }

      const body = { action, days, start_min: start, end_min: end };
      if (action === 'nightvision') body.value = form.querySelector('[data-field="value"]').value;

      const submit = form.querySelector('button[type="submit"]');
      submit.disabled = true;
      try {
        const r = await fetch(`/api/cameras/${encodeURIComponent(camId)}/schedules`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) { alert(data.error || 'Could not add schedule.'); return; }
        const list = block.querySelector('[data-sched-list]');
        const empty = list.querySelector('.sched-empty');
        if (empty) empty.remove();
        list.appendChild(renderRow(camId, data));
      } catch { alert('Network error.'); }
      finally { submit.disabled = false; }
    });
  });
})();
