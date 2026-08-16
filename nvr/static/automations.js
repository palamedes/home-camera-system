/*
 * Automations: when something happens, do something.
 *
 * An automation is a trigger (an event Sentry raised, or nothing at all), an
 * optional time window, and a list of actions. Every one gets a URL whether or
 * not it has a trigger — that URL is the generic "poke Sentry" endpoint.
 *
 * Actions are validated by the server when SAVED rather than when they fire,
 * so a typo surfaces here and now instead of at 2am when the driveway camera
 * sees somebody. This file just has to report what the server says.
 *
 * All text via textContent — names are user-typed and errors come back from
 * devices and remote webhooks.
 */
function initAutomations() {
  const listEl = document.getElementById('auto-list');
  if (!listEl) return;

  const alertEl = document.getElementById('auto-alert');
  const modal = document.getElementById('auto-modal');
  const modalBox = modal.querySelector('.modal');
  const modalTitle = document.getElementById('auto-modal-title');
  const modalBody = document.getElementById('auto-modal-body');

  let state = {
    automations: [], devices: [], rooms: [], layers: [],
    cameras: [], event_types: [], token: '',
  };

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  function say(message, bad) {
    alertEl.replaceChildren();
    if (!message) return;
    alertEl.appendChild(el('div', 'alert ' + (bad ? 'alert-error' : 'alert-info'), message));
    if (!bad) setTimeout(() => alertEl.replaceChildren(), 5000);
  }

  async function api(url, options) {
    const r = await fetch(url, options);
    let data = {};
    try { data = await r.json(); } catch { /* empty body is fine */ }
    if (!r.ok) throw new Error(data.error || `request failed (${r.status})`);
    return data;
  }

  const send = (url, method, body) => api(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });

  async function load() {
    try {
      state = await api('/api/automations');
    } catch (err) {
      listEl.replaceChildren(el('div', 'muted small', 'Could not load automations.'));
      return;
    }
    render();
  }

  // --- describing ----------------------------------------------------------

  const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  function fmtTime(minutes) {
    const h = Math.floor(minutes / 60), m = minutes % 60;
    const hour = h % 12 === 0 ? 12 : h % 12;
    return `${hour}:${String(m).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`;
  }

  function describeDays(mask) {
    if (mask === 127) return 'every day';
    if (mask === 0b0011111) return 'weekdays';
    if (mask === 0b1100000) return 'weekends';
    return DAY_NAMES.filter((_, i) => mask & (1 << i)).join(', ') || 'never';
  }

  function nameOfDevice(id) {
    const found = state.devices.find(d => d.id === id);
    return found ? found.name : id;
  }

  function nameOfCamera(id) {
    const found = state.cameras.find(c => c.id === id);
    return found ? found.name : id;
  }

  function layerLabel(value) {
    const found = state.layers.find(l => l.value === value);
    return found ? found.label : value;
  }

  function describeTrigger(a) {
    if (a.trigger_kind === 'hook') return 'Only when its URL is called';
    const type = a.match.event_type || 'anything';
    const where = a.match.camera_id
      ? ` on ${nameOfCamera(a.match.camera_id)}` : ' on any camera';
    return `When ${type} is detected${where}`;
  }

  function describeWindow(a) {
    const bits = [];
    if (a.start_min != null && a.end_min != null) {
      bits.push(`${fmtTime(a.start_min)}–${fmtTime(a.end_min)}`);
    }
    if (a.days !== 127) bits.push(describeDays(a.days));
    return bits.join(' · ');
  }

  function describeAction(action) {
    if (action.kind === 'device') {
      const verb = action.state === 'toggle' ? 'Toggle' :
        (action.state === 'on' ? 'Switch on' : 'Switch off');
      const held = action.for_seconds
        ? ` for ${Math.round(action.for_seconds / 60)} min` : '';
      return `${verb} ${nameOfDevice(action.device_id)}${held}`;
    }
    if (action.kind === 'covering') {
      const what = action.layer ? layerLabel(action.layer) : 'all coverings';
      const room = action.room_id
        ? (state.rooms.find(r => r.id === action.room_id) || {}).name : null;
      const verb = action.position === 0 ? 'Open'
        : action.position === 100 ? 'Close' : `Move to ${action.position}%`;
      return `${verb} ${what}${room ? ' in ' + room : ''}`;
    }
    if (action.kind === 'webhook') return `Call ${action.url}`;
    return action.kind;
  }

  // --- rendering -----------------------------------------------------------

  function render() {
    listEl.replaceChildren();
    const example = document.getElementById('auto-token-example');
    if (example && state.automations.length) {
      example.textContent =
        `${location.origin}${state.automations[0].url}?token=${state.token}`;
    }

    if (!state.automations.length) {
      const panel = el('div', 'panel');
      const pad = el('div', 'panel-pad');
      pad.appendChild(el('h2', null, 'Nothing automated yet'));
      pad.appendChild(el('p', 'muted small',
        'Sentry knows things nothing else on the network knows — that a person '
        + 'is on the driveway, that the river is rising. An automation is how '
        + 'it acts on that: switch a relay, move the blinds, call a URL.'));
      panel.appendChild(pad);
      listEl.appendChild(panel);
      return;
    }
    for (const a of state.automations) listEl.appendChild(card(a));
  }

  function card(a) {
    const panel = el('div', 'panel blinds-room');

    const head = el('div', 'list-item blinds-room-title');
    head.appendChild(el('span', 'dot ' + (a.enabled ? 'dot-ok' : 'dot-off')));
    const grow = el('div', 'grow');
    const title = el('div', 'title', a.name);
    if (!a.enabled) title.appendChild(el('span', 'pill pill-off', 'Paused'));
    grow.appendChild(title);
    grow.appendChild(el('div', 'sub muted small', describeTrigger(a)));
    const window_ = describeWindow(a);
    if (window_) grow.appendChild(el('div', 'sub muted small', window_));
    if (a.last_error) {
      grow.appendChild(el('div', 'sub small error-text', a.last_error));
    } else if (a.run_count) {
      grow.appendChild(el('div', 'sub muted small', `Run ${a.run_count} time(s)`));
    }
    head.appendChild(grow);

    const run = el('button', 'btn btn-sm', 'Run now');
    run.type = 'button';
    run.addEventListener('click', async () => {
      run.disabled = true;
      try {
        const r = await send(`/api/automations/${a.id}/run`, 'POST');
        if (r.ok) say(`${a.name}: ran ${r.ran} action(s).`);
        else say(`${a.name}: ${r.errors.join('; ')}`, true);
        await load();
      } catch (err) { say(err.message, true); }
      finally { run.disabled = false; }
    });
    head.appendChild(run);

    const pause = el('button', 'btn btn-sm', a.enabled ? 'Pause' : 'Resume');
    pause.type = 'button';
    pause.addEventListener('click', async () => {
      pause.disabled = true;
      try {
        await send(`/api/automations/${a.id}`, 'PATCH', { enabled: !a.enabled });
        await load();
      } catch (err) { say(err.message, true); }
      finally { pause.disabled = false; }
    });
    head.appendChild(pause);

    const remove = el('button', 'remove-x', '×');
    remove.title = 'Delete automation';
    remove.setAttribute('aria-label', 'Delete automation');
    remove.addEventListener('click', async () => {
      if (!confirm(`Delete "${a.name}"?`)) return;
      try {
        await api(`/api/automations/${a.id}`, { method: 'DELETE' });
        await load();
      } catch (err) { say(err.message, true); }
    });
    head.appendChild(remove);
    panel.appendChild(head);

    const body = el('div', 'blinds-list');
    for (const action of a.actions) {
      const row = el('div', 'list-item');
      row.appendChild(el('div', 'grow', describeAction(action)));
      body.appendChild(row);
    }
    const urlRow = el('div', 'list-item');
    const urlBox = el('div', 'grow');
    urlBox.appendChild(el('div', 'muted small', 'URL'));
    urlBox.appendChild(el('code', 'auto-url',
      `${location.origin}${a.url}?token=${state.token}`));
    urlRow.appendChild(urlBox);
    const copy = el('button', 'btn btn-sm', 'Copy');
    copy.type = 'button';
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(
          `${location.origin}${a.url}?token=${state.token}`);
        say('URL copied.');
      } catch { say('Could not copy — select it by hand.', true); }
    });
    urlRow.appendChild(copy);
    body.appendChild(urlRow);
    panel.appendChild(body);
    return panel;
  }

  // --- editor --------------------------------------------------------------

  function openModal(title) {
    modalTitle.textContent = title;
    modalBody.replaceChildren();
    modalBox.classList.add('modal-wide');
    modal.hidden = false;
    return modalBody;
  }

  const closeModal = () => { modal.hidden = true; };
  document.getElementById('auto-modal-close').addEventListener('click', closeModal);
  modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });

  function field(labelText, input) {
    const wrap = el('div', 'field');
    wrap.appendChild(el('label', null, labelText));
    wrap.appendChild(input);
    return wrap;
  }

  function select(options, selected) {
    const s = document.createElement('select');
    for (const [value, label] of options) {
      const o = document.createElement('option');
      o.value = value;
      o.textContent = label;
      if (value === selected) o.selected = true;
      s.appendChild(o);
    }
    return s;
  }

  function showEditor() {
    const body = openModal('Add automation');

    const name = document.createElement('input');
    name.type = 'text';
    name.placeholder = 'Porch light when someone is on the drive';
    body.appendChild(field('Name', name));

    const trigger = select([
      ['event', 'When Sentry detects something'],
      ['hook', 'Only when its URL is called'],
    ]);
    body.appendChild(field('Trigger', trigger));

    const eventBits = el('div', 'modal-grid');
    const eventType = select(
      [['', 'Anything'], ...state.event_types.map(t => [t, t])]);
    eventBits.appendChild(field('Detects', eventType));
    const camera = select(
      [['', 'Any camera'], ...state.cameras.map(c => [c.id, c.name])]);
    eventBits.appendChild(field('On', camera));
    body.appendChild(eventBits);
    trigger.addEventListener('change', () => {
      eventBits.hidden = trigger.value !== 'event';
    });

    // Optional window.
    const times = el('div', 'modal-grid');
    const from = document.createElement('input');
    from.type = 'time';
    const to = document.createElement('input');
    to.type = 'time';
    times.appendChild(field('Only after', from));
    times.appendChild(field('Until', to));
    body.appendChild(times);
    body.appendChild(el('p', 'muted small',
      'Leave both blank to run whenever the trigger fires. Setting them is how '
      + '"porch light on" comes to mean "after dark".'));

    const cooldown = document.createElement('input');
    cooldown.type = 'number';
    cooldown.min = '0';
    cooldown.value = '60';
    body.appendChild(field('Wait at least this long between runs (seconds)', cooldown));
    body.appendChild(el('p', 'muted small',
      'A person standing in frame raises a detection every few seconds. Without '
      + 'this the relay would be commanded over and over.'));

    // Actions.
    body.appendChild(el('h3', null, 'Do this'));
    const actionsWrap = el('div', 'auto-actions');
    body.appendChild(actionsWrap);
    const addAction = el('button', 'btn btn-sm', 'Add an action');
    addAction.type = 'button';
    addAction.addEventListener('click', () => actionsWrap.appendChild(actionRow()));
    body.appendChild(addAction);
    actionsWrap.appendChild(actionRow());

    const save = el('button', 'btn btn-primary', 'Add automation');
    save.type = 'button';
    save.addEventListener('click', async () => {
      const payload = {
        name: name.value.trim(),
        trigger_kind: trigger.value,
        match: trigger.value === 'event'
          ? { event_type: eventType.value || null, camera_id: camera.value || null }
          : {},
        cooldown_seconds: Number(cooldown.value || 0),
        start_min: from.value ? toMinutes(from.value) : null,
        end_min: to.value ? toMinutes(to.value) : null,
        actions: [...actionsWrap.children].map(row => row._read()).filter(Boolean),
      };
      save.disabled = true;
      try {
        await send('/api/automations', 'POST', payload);
        closeModal();
        await load();
        say('Automation added.');
      } catch (err) {
        say(err.message, true);
      } finally {
        save.disabled = false;
      }
    });
    body.appendChild(save);
    name.focus();
  }

  function toMinutes(value) {
    const [h, m] = value.split(':').map(Number);
    return h * 60 + m;
  }

  function actionRow() {
    const row = el('div', 'auto-action');
    const kind = select([
      ['device', 'Switch a device'],
      ['covering', 'Move window coverings'],
      ['webhook', 'Call a URL'],
    ]);
    row.appendChild(field('Action', kind));

    const bits = el('div', 'auto-action-bits');
    row.appendChild(bits);

    const remove = el('button', 'btn btn-sm btn-danger', 'Remove');
    remove.type = 'button';
    remove.addEventListener('click', () => row.remove());
    row.appendChild(remove);

    let read = () => null;

    function rebuild() {
      bits.replaceChildren();
      if (kind.value === 'device') {
        const which = select(state.devices.map(d => [d.id, d.name]));
        const what = select([['on', 'On'], ['off', 'Off'], ['toggle', 'Toggle']]);
        const held = document.createElement('input');
        held.type = 'number';
        held.min = '0';
        held.placeholder = 'optional';
        const grid = el('div', 'modal-grid');
        grid.appendChild(field('Device', which));
        grid.appendChild(field('Set to', what));
        bits.appendChild(grid);
        bits.appendChild(field('Then back after (seconds)', held));
        read = () => {
          if (!which.value) return null;
          const out = { kind: 'device', device_id: which.value, state: what.value };
          if (held.value) out.for_seconds = Number(held.value);
          return out;
        };
      } else if (kind.value === 'covering') {
        const layer = select(
          [['', 'All coverings'], ...state.layers.map(l => [l.value, l.label])]);
        const room = select(
          [['', 'Everywhere'], ...state.rooms.map(r => [String(r.id), r.name])]);
        const position = select(
          [['100', 'Close'], ['0', 'Open'], ['50', 'Half way']]);
        const grid = el('div', 'modal-grid');
        grid.appendChild(field('Which', layer));
        grid.appendChild(field('Where', room));
        bits.appendChild(grid);
        bits.appendChild(field('Do', position));
        read = () => ({
          kind: 'covering',
          layer: layer.value || null,
          room_id: room.value ? Number(room.value) : null,
          position: Number(position.value),
        });
      } else {
        const url = document.createElement('input');
        url.type = 'text';
        url.placeholder = 'https://example.local/hook';
        const method = select([['POST', 'POST'], ['GET', 'GET']]);
        bits.appendChild(field('URL', url));
        bits.appendChild(field('Method', method));
        read = () => (url.value.trim()
          ? { kind: 'webhook', url: url.value.trim(), method: method.value }
          : null);
      }
    }

    kind.addEventListener('change', rebuild);
    rebuild();
    row._read = () => read();
    return row;
  }

  document.getElementById('auto-add').addEventListener('click', showEditor);
  load();
}
