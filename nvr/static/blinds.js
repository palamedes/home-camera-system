/*
 * Blinds: motorised window coverings, grouped by room.
 *
 * Two things are worth knowing before reading this.
 *
 * 1. The protocol's position scale is 0 = fully open, 100 = fully closed. That
 *    is backwards from how a slider reads, so the UI shows "% closed" and the
 *    wire value is used as-is. The flip lives here and nowhere else.
 *
 * 2. Every covering belongs to a layer: 'sheer' (light filtering, you can see
 *    through it) or 'blackout' (room darkening, you cannot). A dual-roller
 *    window has one of each, which is why every group control comes in three
 *    flavours: sheers, blackouts, both.
 *
 * All text is set with textContent — room and covering names are user-typed and
 * error strings come back from the hub itself.
 */
function initBlinds() {
  const roomsEl = document.getElementById('blinds-rooms');
  if (!roomsEl) return;

  const alertEl = document.getElementById('blinds-alert');
  const allBar = document.getElementById('blinds-allbar');
  const modal = document.getElementById('blinds-modal');
  const modalTitle = document.getElementById('blinds-modal-title');
  const modalBody = document.getElementById('blinds-modal-body');

  let state = { rooms: [], coverings: [], hubs: [], layers: [], can_edit: false };

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  // `sticky` keeps a message up until something replaces it. Needed for the
  // LAN sweep, which outlasts the 4s auto-clear and would otherwise look like
  // a scan that silently gave up.
  function say(message, bad, sticky) {
    alertEl.replaceChildren();
    if (!message) return;
    alertEl.appendChild(el('div', 'alert ' + (bad ? 'alert-error' : 'alert-info'), message));
    if (!bad && !sticky) setTimeout(() => alertEl.replaceChildren(), 4000);
  }

  async function api(url, options) {
    const r = await fetch(url, options);
    let data = {};
    try { data = await r.json(); } catch { /* empty body is fine */ }
    if (!r.ok) throw new Error(data.error || `request failed (${r.status})`);
    return data;
  }

  function post(url, body) {
    return api(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  // --- loading -------------------------------------------------------------

  async function load() {
    try {
      state = await api('/api/blinds');
    } catch (err) {
      roomsEl.replaceChildren(el('div', 'muted small', 'Could not load blinds.'));
      return;
    }
    render();
  }

  function layerLabel(value) {
    const found = (state.layers || []).find(l => l.value === value);
    return found ? found.label : value;
  }

  function coveringsIn(roomId) {
    return state.coverings.filter(c =>
      roomId === null ? c.room_id == null : c.room_id === roomId);
  }

  // --- rendering -----------------------------------------------------------

  function render() {
    roomsEl.replaceChildren();
    // The whole-house bar is the same component as a room header, just scoped
    // to every covering — so the two can never drift apart visually.
    allBar.hidden = state.coverings.length === 0;
    const allGroups = document.getElementById('blinds-allbar-groups');
    if (allGroups) {
      allGroups.replaceChildren(groupControls(null, state.coverings));
    }

    if (!state.hubs.length) {
      roomsEl.appendChild(emptyState());
      return;
    }

    for (const room of state.rooms) {
      roomsEl.appendChild(roomPanel(room, coveringsIn(room.id)));
    }
    const loose = coveringsIn(null);
    if (loose.length) {
      roomsEl.appendChild(roomPanel(
        { id: null, name: 'Unassigned' }, loose,
        'These are paired to a hub but not yet in a room.'));
    }
    if (!state.rooms.length && !loose.length) {
      roomsEl.appendChild(el('div', 'muted small',
        'No coverings yet. Use Refresh on a hub to pull in whatever is paired to it.'));
    }
    roomsEl.appendChild(schedulePanel());
    roomsEl.appendChild(hubPanel());
  }

  // --- schedules -----------------------------------------------------------
  //
  // A rule is a moment, not a span: "at 5pm on Sundays, close the blackouts".
  // It fires once when its time comes round, so raising a shade by hand
  // afterwards sticks. "Open in the morning, close in the evening" is two
  // rules, which is also how people say it — and it is what makes different
  // times on different days expressible at all.

  const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const DAY_PRESETS = [
    { label: 'Every day', mask: 0b1111111 },
    { label: 'Weekdays', mask: 0b0011111 },
    { label: 'Weekend', mask: 0b1100000 },
  ];

  function describeDays(mask) {
    if (mask === 0b1111111) return 'every day';
    if (mask === 0b0011111) return 'weekdays';
    if (mask === 0b1100000) return 'weekends';
    const on = DAY_NAMES.filter((_, i) => mask & (1 << i));
    return on.length ? on.join(', ') : 'never';
  }

  function describeTime(minutes) {
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    const suffix = h < 12 ? 'AM' : 'PM';
    const hour = h % 12 === 0 ? 12 : h % 12;
    return `${hour}:${String(m).padStart(2, '0')} ${suffix}`;
  }

  function describePosition(position) {
    if (position === 0) return 'Open';
    if (position === 100) return 'Close';
    return `Move to ${position}% closed`;
  }

  function describeTarget(rule) {
    const layer = rule.layer ? layerLabel(rule.layer) : 'All coverings';
    if (rule.covering_id) {
      const c = state.coverings.find(x => x.id === rule.covering_id);
      return c ? c.name : 'One covering';
    }
    if (rule.room_id == null) return layer;
    const room = state.rooms.find(r => r.id === rule.room_id);
    return `${layer} · ${room ? room.name : 'unknown room'}`;
  }

  function schedulePanel() {
    const panel = el('div', 'panel blinds-room');

    const head = el('div', 'list-item blinds-room-title');
    const headText = el('div', 'grow');
    headText.appendChild(el('h2', null, 'Schedules'));
    headText.appendChild(el('div', 'muted small',
      'Each rule fires once at its time. Move a shade by hand afterwards and it stays put.'));
    head.appendChild(headText);
    if (state.can_edit) {
      const add = el('button', 'btn btn-sm btn-primary', 'Add schedule');
      add.type = 'button';
      add.addEventListener('click', () => editRule(null));
      head.appendChild(add);
    }
    panel.appendChild(head);

    const list = el('div', 'blinds-list');
    const rules = (state.schedules || [])
      .slice()
      .sort((a, b) => a.at - b.at);
    if (!rules.length) {
      list.appendChild(el('div', 'list-item muted small',
        'No schedules yet. Add one to close the blackouts at sunset, or open the '
        + 'sheers in the morning.'));
    } else {
      for (const rule of rules) list.appendChild(ruleRow(rule));
    }
    panel.appendChild(list);
    return panel;
  }

  function ruleRow(rule) {
    const item = el('div', 'list-item');
    item.appendChild(el('span', 'dot ' + (rule.enabled ? 'dot-ok' : 'dot-off')));

    const grow = el('div', 'grow');
    const title = el('div', 'title',
      `${describePosition(rule.position)} — ${describeTarget(rule)}`);
    if (!rule.enabled) title.appendChild(el('span', 'pill pill-off', 'Paused'));
    grow.appendChild(title);
    grow.appendChild(el('div', 'sub muted small',
      `${describeTime(rule.at)} · ${describeDays(rule.days)}`));
    item.appendChild(grow);

    if (state.can_edit) {
      const pause = el('button', 'btn btn-sm', rule.enabled ? 'Pause' : 'Resume');
      pause.type = 'button';
      pause.addEventListener('click', async () => {
        pause.disabled = true;
        try {
          await api(`/api/blinds/schedules/${rule.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !rule.enabled }),
          });
          await load();
        } catch (err) { say(err.message, true); }
        finally { pause.disabled = false; }
      });
      item.appendChild(pause);

      const remove = el('button', 'remove-x', '×');
      remove.title = 'Delete schedule';
      remove.setAttribute('aria-label', 'Delete schedule');
      remove.addEventListener('click', async () => {
        if (!confirm('Delete this schedule?')) return;
        try {
          await api(`/api/blinds/schedules/${rule.id}`, { method: 'DELETE' });
          await load();
        } catch (err) { say(err.message, true); }
      });
      item.appendChild(remove);
    }
    return item;
  }

  function editRule() {
    const body = openModal('Add schedule');

    // What to move.
    const layer = document.createElement('select');
    for (const opt of [...state.layers, { value: '', label: 'Both layers' }]) {
      const o = document.createElement('option');
      o.value = opt.value;
      o.textContent = opt.label;
      layer.appendChild(o);
    }
    body.appendChild(field('Move', layer));

    const room = document.createElement('select');
    const allRooms = document.createElement('option');
    allRooms.value = '';
    allRooms.textContent = 'Everywhere';
    room.appendChild(allRooms);
    for (const r of state.rooms) {
      const o = document.createElement('option');
      o.value = String(r.id);
      o.textContent = r.name;
      room.appendChild(o);
    }
    body.appendChild(field('Where', room));

    // What to do.
    const action = document.createElement('select');
    for (const [value, label] of [['100', 'Close'], ['0', 'Open'], ['custom', 'Part way…']]) {
      const o = document.createElement('option');
      o.value = value;
      o.textContent = label;
      action.appendChild(o);
    }
    body.appendChild(field('Do', action));

    const custom = document.createElement('input');
    custom.type = 'number';
    custom.min = '0';
    custom.max = '100';
    custom.step = '5';
    custom.value = '50';
    const customField = field('Percent closed', custom);
    customField.hidden = true;
    body.appendChild(customField);
    action.addEventListener('change', () => {
      customField.hidden = action.value !== 'custom';
    });

    // When.
    const at = document.createElement('input');
    at.type = 'time';
    at.value = '17:00';
    body.appendChild(field('At', at));

    const daysWrap = el('div', 'field');
    daysWrap.appendChild(el('label', null, 'On'));
    const presets = el('div', 'blinds-day-presets');
    const boxes = [];
    for (let i = 0; i < 7; i++) {
      const label = el('label', 'blinds-day');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = true;
      boxes.push(box);
      label.appendChild(box);
      label.appendChild(el('span', null, DAY_NAMES[i]));
      presets.appendChild(label);
    }
    daysWrap.appendChild(presets);

    const quick = el('div', 'blinds-day-quick');
    for (const preset of DAY_PRESETS) {
      const b = el('button', 'btn btn-sm', preset.label);
      b.type = 'button';
      b.addEventListener('click', () => {
        boxes.forEach((box, i) => { box.checked = Boolean(preset.mask & (1 << i)); });
      });
      quick.appendChild(b);
    }
    daysWrap.appendChild(quick);
    body.appendChild(daysWrap);

    const save = el('button', 'btn btn-primary', 'Add schedule');
    save.type = 'button';
    save.addEventListener('click', async () => {
      const [hh, mm] = (at.value || '').split(':');
      const minutes = Number(hh) * 60 + Number(mm);
      if (!Number.isFinite(minutes)) { say('Pick a time.', true); return; }
      let mask = 0;
      boxes.forEach((box, i) => { if (box.checked) mask |= (1 << i); });
      if (!mask) { say('Pick at least one day.', true); return; }
      const position = action.value === 'custom'
        ? Number(custom.value) : Number(action.value);
      save.disabled = true;
      try {
        await post('/api/blinds/schedules', {
          layer: layer.value || null,
          room_id: room.value === '' ? null : Number(room.value),
          position,
          at: minutes,
          days: mask,
        });
        closeModal();
        await load();
        say('Schedule added.');
      } catch (err) {
        say(err.message, true);
      } finally {
        save.disabled = false;
      }
    });
    body.appendChild(save);
  }

  function emptyState() {
    const panel = el('div', 'panel');
    const pad = el('div', 'panel-pad');
    pad.appendChild(el('h2', null, 'No shade hub yet'));
    pad.appendChild(el('p', 'muted small',
      'Sentry talks to a Connector / Motionblinds bridge over the LAN — no cloud '
      + 'and no vendor account. Add the hub by address, or let Sentry look for it.'));
    if (state.can_edit) {
      const find = el('button', 'btn btn-sm btn-primary', 'Look for a hub');
      find.type = 'button';
      find.addEventListener('click', discoverHubs);
      pad.appendChild(find);
    }
    panel.appendChild(pad);
    return panel;
  }

  function roomPanel(room, coverings, note) {
    const panel = el('div', 'panel blinds-room');

    // A normal-height .list-item, exactly like a settings row: its 14/16
    // padding is what the .remove-x strip's negative margins are sized to
    // cancel, so the delete button lands flush to the panel edge. Group
    // controls go in their own band below rather than in here — inside this
    // row they would stretch it, and the strip with it.
    const head = el('div', 'list-item blinds-room-title');
    const title = el('div', 'grow');
    title.appendChild(el('h2', null, room.name));
    if (note) title.appendChild(el('div', 'muted small', note));
    head.appendChild(title);

    if (state.can_edit && room.id !== null) {
      const rename = el('button', 'btn btn-sm', 'Rename');
      rename.type = 'button';
      rename.addEventListener('click', () => renameRoom(room));
      head.appendChild(rename);

      const remove = el('button', 'remove-x', '×');
      remove.title = 'Delete room';
      remove.setAttribute('aria-label', 'Delete room');
      remove.addEventListener('click', () => deleteRoom(room));
      head.appendChild(remove);
    }
    panel.appendChild(head);

    if (coverings.length) {
      const actions = el('div', 'blinds-room-actions');
      actions.appendChild(groupControls(room.id, coverings));
      panel.appendChild(actions);
    }

    const list = el('div', 'blinds-list');
    if (!coverings.length) {
      list.appendChild(el('div', 'muted small', 'Nothing in this room yet.'));
    } else {
      for (const covering of coverings) list.appendChild(coveringRow(covering));
    }
    panel.appendChild(list);
    return panel;
  }

  function groupControls(roomId, coverings) {
    const wrap = el('div', 'blinds-group');
    const present = new Set(coverings.map(c => c.layer));
    // Only offer a per-layer control when both layers are actually present. A
    // "Blackout" button on a room holding nothing but sheers is just a way to
    // be wrong, and it is what made the whole-house bar read as noise.
    const groups = present.size > 1
      ? [...state.layers
            .filter(l => present.has(l.value))
            .map(l => ({ layer: l.value, label: l.label })),
         { layer: null, label: 'Both' }]
      : [{ layer: null, label: 'All' }];

    for (const group of groups) {
      const box = el('div', 'blinds-group-box');
      box.appendChild(el('span', 'blinds-group-label', group.label));
      const buttons = el('div', 'blinds-group-btns');
      buttons.appendChild(groupBtn('Open', roomId, group.layer, 'open'));
      buttons.appendChild(groupBtn('Close', roomId, group.layer, 'close'));
      buttons.appendChild(groupBtn('Stop', roomId, group.layer, 'stop'));
      box.appendChild(buttons);
      wrap.appendChild(box);
    }
    return wrap;
  }

  function groupBtn(label, roomId, layer, action) {
    const b = el('button', 'btn btn-sm', label);
    b.type = 'button';
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        const r = await post('/api/blinds/group/command',
          { room_id: roomId, layer, action });
        reportGroup(r);
        await load();
      } catch (err) {
        say(err.message, true);
      } finally {
        b.disabled = false;
      }
    });
    return b;
  }

  function reportGroup(result) {
    const failed = result.failed || [];
    if (!failed.length) {
      say(`Moved ${(result.moved || []).length} covering(s).`);
      return;
    }
    // Partial success is the common failure on a weak 433 MHz link, and saying
    // "done" when half the room did not move would be a lie.
    say(`${(result.moved || []).length} moved, ${failed.length} did not: `
      + failed.map(f => `${f.name} (${f.error})`).join('; '), true);
  }

  function coveringRow(covering) {
    const item = el('div', 'list-item blinds-item');
    item.dataset.covering = covering.id;

    const dot = el('span', 'dot ' + (covering.last_error ? 'dot-bad' : 'dot-ok'));
    item.appendChild(dot);

    const grow = el('div', 'grow');
    const title = el('div', 'title', covering.name);
    title.appendChild(el('span', 'pill', layerLabel(covering.layer)));
    if (!covering.enabled) title.appendChild(el('span', 'pill pill-off', 'Disabled'));
    grow.appendChild(title);
    grow.appendChild(el('div', 'sub muted small', statusLine(covering)));
    if (covering.last_error) {
      grow.appendChild(el('div', 'sub small error-text', covering.last_error));
    }
    item.appendChild(grow);

    item.appendChild(slider(covering));
    item.appendChild(moveBtn('Open', covering, { action: 'open' }));
    item.appendChild(moveBtn('Close', covering, { action: 'close' }));
    item.appendChild(moveBtn('Stop', covering, { action: 'stop' }));

    if (state.can_edit) {
      const edit = el('button', 'btn btn-sm', 'Edit');
      edit.type = 'button';
      edit.addEventListener('click', () => editCovering(covering));
      item.appendChild(edit);
    }
    return item;
  }

  function statusLine(covering) {
    const bits = [];
    if (covering.last_position != null) {
      bits.push(`${covering.last_position}% closed`);
    }
    if (!covering.bidirectional && covering.last_position != null) {
      // A transmit-only motor cannot confirm anything, so the number above is
      // the last thing we asked for, not the truth. Say so.
      bits.push('last commanded');
    }
    if (covering.battery_volts != null) {
      bits.push(`${covering.battery_volts} V`
        + (covering.battery_percent != null ? ` (~${covering.battery_percent}%)` : ''));
    }
    if (covering.rssi != null) {
      bits.push(`signal ${covering.rssi} dBm` + (covering.rssi < -90 ? ' — weak' : ''));
    }
    return bits.join(' · ') || 'No reading yet';
  }

  function slider(covering) {
    const wrap = el('label', 'blinds-slider');
    const input = document.createElement('input');
    input.type = 'range';
    input.min = '0';
    input.max = '100';
    input.step = '5';
    input.value = String(covering.last_position ?? 0);
    input.title = 'Percent closed';
    const readout = el('span', 'muted small', `${input.value}%`);
    input.addEventListener('input', () => { readout.textContent = `${input.value}%`; });
    input.addEventListener('change', async () => {
      input.disabled = true;
      try {
        await post(`/api/blinds/coverings/${encodeURIComponent(covering.id)}/command`,
          { position: Number(input.value) });
        say(`${covering.name} moving to ${input.value}% closed.`);
      } catch (err) {
        say(err.message, true);
      } finally {
        input.disabled = false;
      }
    });
    wrap.appendChild(input);
    wrap.appendChild(readout);
    return wrap;
  }

  function moveBtn(label, covering, command) {
    const b = el('button', 'btn btn-sm', label);
    b.type = 'button';
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        await post(`/api/blinds/coverings/${encodeURIComponent(covering.id)}/command`,
          command);
        say(`${covering.name}: ${command.action}.`);
        await load();
      } catch (err) {
        say(err.message, true);
      } finally {
        b.disabled = false;
      }
    });
    return b;
  }

  // --- hubs ----------------------------------------------------------------

  function hubPanel() {
    const panel = el('div', 'panel blinds-room');

    // Same shape as a room: a .list-item title row, then .list-item rows. The
    // hub rows used to sit inside .panel-pad, whose 18px padding the
    // .remove-x strip's negative margins cannot cancel — the bug the room
    // headers had.
    const head = el('div', 'list-item blinds-room-title');
    const headText = el('div', 'grow');
    headText.appendChild(el('h2', null, 'Hubs'));
    headText.appendChild(el('div', 'muted small',
      'The bridges Sentry talks to over the LAN.'));
    head.appendChild(headText);
    if (state.can_edit) {
      const find = el('button', 'btn btn-sm', 'Look for a hub');
      find.type = 'button';
      find.addEventListener('click', discoverHubs);
      head.appendChild(find);
    }
    panel.appendChild(head);

    if (!state.hubs.length) {
      const empty = el('div', 'blinds-list');
      empty.appendChild(el('div', 'list-item muted small', 'No hub configured.'));
      panel.appendChild(empty);
      return panel;
    }

    const list = el('div', 'blinds-list');
    for (const hub of state.hubs) {
      const row = el('div', 'list-item');
      const grow = el('div', 'grow');
      grow.appendChild(el('div', 'title', hub.name));
      const bits = [hub.host];
      if (hub.protocol) bits.push(`protocol ${hub.protocol}`);
      bits.push(hub.has_key ? 'key set' : 'no key — read-only unless the hub allows writes');
      grow.appendChild(el('div', 'sub muted small', bits.join(' · ')));
      if (hub.last_error) {
        grow.appendChild(el('div', 'sub small error-text', hub.last_error));
      }
      row.appendChild(grow);

      if (state.can_edit) {
        // Named "Re-scan", not "Refresh": two buttons with the same word doing
        // very different things is what sent a newly paired shade missing.
        const rescan = el('button', 'btn btn-sm', 'Re-scan');
        rescan.type = 'button';
        rescan.title = 'Ask this hub which shades are paired to it, and poll each one';
        rescan.addEventListener('click', async () => {
          rescan.disabled = true;
          try {
            const r = await post(`/api/blinds/hubs/${encodeURIComponent(hub.id)}/refresh`);
            say(`Found ${r.added.length} new, polled ${r.polled}.`);
            await load();
          } catch (err) {
            say(err.message, true);
          } finally {
            rescan.disabled = false;
          }
        });
        row.appendChild(rescan);

        const key = el('button', 'btn btn-sm', 'Key');
        key.type = 'button';
        key.addEventListener('click', () => editHubKey(hub));
        row.appendChild(key);

        const remove = el('button', 'remove-x', '×');
        remove.title = 'Remove hub';
        remove.setAttribute('aria-label', 'Remove hub');
        remove.addEventListener('click', async () => {
          if (!confirm(`Remove ${hub.name}? Its coverings disappear from Sentry; `
            + 'the hub and your shades are untouched.')) return;
          try {
            await api(`/api/blinds/hubs/${encodeURIComponent(hub.id)}`, { method: 'DELETE' });
            await load();
          } catch (err) { say(err.message, true); }
        });
        row.appendChild(remove);
      }
      list.appendChild(row);
    }
    panel.appendChild(list);
    return panel;
  }

  // --- modal helpers -------------------------------------------------------

  function openModal(title) {
    modalTitle.textContent = title;
    modalBody.replaceChildren();
    modal.hidden = false;
    return modalBody;
  }

  function closeModal() { modal.hidden = true; }

  document.getElementById('blinds-modal-close').addEventListener('click', closeModal);
  modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });

  function field(labelText, input) {
    const wrap = el('div', 'field');
    const label = el('label', null, labelText);
    wrap.appendChild(label);
    wrap.appendChild(input);
    return wrap;
  }

  function textInput(value, placeholder) {
    const input = document.createElement('input');
    input.type = 'text';
    if (value != null) input.value = value;
    if (placeholder) input.placeholder = placeholder;
    return input;
  }

  // --- admin actions -------------------------------------------------------

  let scanning = false;

  async function discoverHubs() {
    if (scanning) return;
    scanning = true;
    // Most access points drop multicast and broadcast, so discovery walks the
    // whole subnet one address at a time. That takes about ten seconds, which
    // is long enough that a message which quietly expires reads as failure.
    say('Looking for a hub — checking every address on the network. '
      + 'This takes about ten seconds.', false, true);
    setScanning(true);
    let found;
    try {
      found = (await post('/api/blinds/hubs/discover')).hubs || [];
    } catch (err) {
      say(err.message, true);
      return;
    } finally {
      scanning = false;
      setScanning(false);
    }
    if (!found.length) {
      say('No hub answered. Check it is powered and connected — a slow '
        + 'flashing red light on the hub means it is not on the network. '
        + 'You can also add it by address if you know it.', true);
      return;
    }
    say('');
    const body = openModal(found.length === 1 ? 'Found a hub' : 'Hubs found');
    for (const hub of found) body.appendChild(foundRow(hub));
  }

  function setScanning(on) {
    for (const id of ['blinds-add-hub', 'blinds-refresh']) {
      const b = document.getElementById(id);
      if (b) b.disabled = on;
    }
  }

  function foundRow(hub) {
    const row = el('div', 'found-hub');

    const info = el('div', 'found-hub-info');
    info.appendChild(el('div', 'title', hub.host));
    const bits = [`${hub.devices.length} covering${hub.devices.length === 1 ? '' : 's'} paired`];
    if (hub.protocol) bits.push(`protocol ${hub.protocol}`);
    info.appendChild(el('div', 'muted small', bits.join(' · ')));
    if (hub.known) info.appendChild(el('div', 'muted small', 'Already added'));
    row.appendChild(info);

    const add = el('button', 'btn btn-sm btn-primary', hub.known ? 'Re-add' : 'Add');
    add.type = 'button';
    add.addEventListener('click', async () => {
      add.disabled = true;
      try { await addHub(hub.host); } finally { add.disabled = false; }
    });
    row.appendChild(add);
    return row;
  }

  function addHubForm() {
    const body = openModal('Add hub');
    const host = textInput('', '192.168.1.50');
    const name = textInput('Shade hub');
    const key = textInput('', '12ab345c-d67e-8f');
    body.appendChild(field('Hub address', host));
    body.appendChild(field('Name', name));
    body.appendChild(field('App key (optional)', key));
    body.appendChild(el('p', 'muted small',
      'The key is only needed to move things — discovery and status work without '
      + 'it. Find it in the vendor app under Settings → About, tapping that '
      + 'screen five times. Keep the dashes.'));

    const find = el('button', 'btn btn-sm', 'Look for one instead');
    find.type = 'button';
    find.addEventListener('click', discoverHubs);
    body.appendChild(find);

    const save = el('button', 'btn btn-primary', 'Add hub');
    save.type = 'button';
    save.addEventListener('click', () => addHub(host.value.trim(), name.value.trim(), key.value.trim()));
    body.appendChild(save);
  }

  async function addHub(host, name, apiKey) {
    try {
      const r = await post('/api/blinds/hubs', { host, name, api_key: apiKey });
      closeModal();
      say(`Added ${r.hub.name}; ${r.added.length} covering(s) picked up.`);
      await load();
    } catch (err) {
      say(err.message, true);
    }
  }

  function editHubKey(hub) {
    const body = openModal(`${hub.name} — app key`);
    const key = textInput('', hub.has_key ? '(unchanged)' : '12ab345c-d67e-8f');
    body.appendChild(field('App key', key));
    body.appendChild(el('p', 'muted small',
      'Exactly 16 characters including the dashes. Leave blank to clear it.'));
    const save = el('button', 'btn btn-primary', 'Save');
    save.type = 'button';
    save.addEventListener('click', async () => {
      try {
        await api(`/api/blinds/hubs/${encodeURIComponent(hub.id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: key.value.trim() }),
        });
        closeModal();
        await load();
      } catch (err) { say(err.message, true); }
    });
    body.appendChild(save);
  }

  function editCovering(covering) {
    const body = openModal(covering.name);
    const name = textInput(covering.name);
    body.appendChild(field('Name', name));

    const layer = document.createElement('select');
    for (const l of state.layers) {
      const opt = document.createElement('option');
      opt.value = l.value;
      opt.textContent = l.label;
      if (covering.layer === l.value) opt.selected = true;
      layer.appendChild(opt);
    }
    body.appendChild(field('Layer', layer));
    body.appendChild(el('p', 'muted small',
      'Light filtering is the one you can see through; blackout is the one you '
      + 'cannot. Grouping — "close the blackouts" — keys off this.'));

    const room = document.createElement('select');
    const none = document.createElement('option');
    none.value = '';
    none.textContent = 'Unassigned';
    room.appendChild(none);
    for (const r of state.rooms) {
      const opt = document.createElement('option');
      opt.value = String(r.id);
      opt.textContent = r.name;
      if (covering.room_id === r.id) opt.selected = true;
      room.appendChild(opt);
    }
    body.appendChild(field('Room', room));

    const save = el('button', 'btn btn-primary', 'Save');
    save.type = 'button';
    save.addEventListener('click', async () => {
      try {
        await api(`/api/blinds/coverings/${encodeURIComponent(covering.id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: name.value.trim(),
            layer: layer.value,
            room_id: room.value === '' ? null : Number(room.value),
          }),
        });
        closeModal();
        await load();
      } catch (err) { say(err.message, true); }
    });
    body.appendChild(save);
  }

  async function renameRoom(room) {
    const name = prompt('Room name', room.name);
    if (name == null) return;
    try {
      await api(`/api/blinds/rooms/${room.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim() }),
      });
      await load();
    } catch (err) { say(err.message, true); }
  }

  async function deleteRoom(room) {
    if (!confirm(`Delete ${room.name}? Its coverings move to Unassigned — `
      + 'nothing is unpaired.')) return;
    try {
      await api(`/api/blinds/rooms/${room.id}`, { method: 'DELETE' });
      await load();
    } catch (err) { say(err.message, true); }
  }

  // --- wiring --------------------------------------------------------------

  /* The page-head Refresh asks the HUB what it has, not just the database.
   * It used to only re-read our own rows, which meant a shade you had just
   * paired could never show up no matter how often you pressed it — while a
   * second button, also called "Refresh", buried in the Hubs panel, was the
   * one that actually went and looked. */
  async function refreshAll() {
    if (!state.can_edit || !state.hubs.length) {
      await load();                    // viewers cannot re-enumerate a hub
      return;
    }
    setScanning(true);
    say('Asking the hub what it has — this polls every shade over radio and '
      + 'takes a few seconds.', false, true);
    let added = 0;
    try {
      for (const hub of state.hubs) {
        const r = await post(`/api/blinds/hubs/${encodeURIComponent(hub.id)}/refresh`);
        added += (r.added || []).length;
      }
    } catch (err) {
      say(err.message, true);
      await load();
      return;
    } finally {
      setScanning(false);
    }
    await load();
    say(added
      ? `Found ${added} new covering${added === 1 ? '' : 's'}.`
      : 'Up to date — nothing new paired to the hub.');
  }

  document.getElementById('blinds-refresh').addEventListener('click', refreshAll);

  const addRoomBtn = document.getElementById('blinds-add-room');
  if (addRoomBtn) {
    addRoomBtn.addEventListener('click', async () => {
      const name = prompt('Room name');
      if (!name || !name.trim()) return;
      try {
        await post('/api/blinds/rooms', { name: name.trim() });
        await load();
      } catch (err) { say(err.message, true); }
    });
  }

  const addHubBtn = document.getElementById('blinds-add-hub');
  if (addHubBtn) addHubBtn.addEventListener('click', addHubForm);

  load();
}
