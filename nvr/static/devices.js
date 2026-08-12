/*
 * Settings > Devices: relays and smart switches (Shelly and friends).
 *
 * Deliberately plain — a list, an add form, and per-device On/Off/Test. Names
 * and errors are set with textContent, never innerHTML, because a device name
 * is user-typed and an error string comes back from the device itself.
 */
function initDevices() {
  const list = document.getElementById('device-list');
  if (!list) return;
  const addBtn = document.getElementById('device-add');
  let drivers = [];

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  async function load() {
    let data;
    try {
      data = await (await fetch('/api/devices')).json();
    } catch {
      list.replaceChildren(el('div', 'muted small', 'Could not load devices.'));
      return;
    }
    drivers = data.drivers || [];
    render(data.devices || []);
  }

  function render(items) {
    list.replaceChildren();
    if (!items.length) {
      list.appendChild(el('div', 'muted small',
        'No devices yet. Add a Shelly relay to control a floodlight from Sentry.'));
      return;
    }
    for (const device of items) list.appendChild(row(device));
  }

  function row(device) {
    const item = el('div', 'list-item');
    item.dataset.device = device.id;

    const dot = el('span', 'dot ' + (device.last_error ? 'dot-bad' : 'dot-ok'));
    item.appendChild(dot);

    const grow = el('div', 'grow');
    const title = el('div', 'title', device.name);
    if (!device.enabled) title.appendChild(el('span', 'pill pill-off', 'Disabled'));
    grow.appendChild(title);

    const bits = [device.host, driverLabel(device.driver)];
    if (device.channel) bits.push('channel ' + device.channel);
    const sub = el('div', 'sub muted small', bits.join(' · '));
    grow.appendChild(sub);

    const status = el('div', 'sub small');
    if (device.last_error) {
      status.classList.add('error-text');
      status.textContent = device.last_error;
    } else if (device.last_state != null) {
      status.textContent = device.last_state ? 'Currently on' : 'Currently off';
    }
    grow.appendChild(status);
    item.appendChild(grow);

    item.appendChild(button('On', () => setState(device.id, 'on', status)));
    item.appendChild(button('Off', () => setState(device.id, 'off', status)));
    item.appendChild(button('Test', () => test(device.id, status)));

    const remove = el('button', 'remove-x', '×');
    remove.title = 'Remove device';
    remove.addEventListener('click', async () => {
      if (!confirm(`Remove ${device.name}? Sentry stops controlling it; the device itself is untouched.`)) return;
      await fetch(`/api/devices/${encodeURIComponent(device.id)}`, { method: 'DELETE' });
      load();
    });
    item.appendChild(remove);
    return item;
  }

  function button(label, onClick) {
    const b = el('button', 'btn btn-sm', label);
    b.type = 'button';
    b.addEventListener('click', async () => {
      b.disabled = true;
      try { await onClick(); } finally { b.disabled = false; }
    });
    return b;
  }

  function driverLabel(value) {
    const found = drivers.find(d => d.value === value);
    return found ? found.label : value;
  }

  async function setState(id, state, status) {
    status.classList.remove('error-text');
    status.textContent = 'Working…';
    try {
      const r = await fetch(`/api/devices/${encodeURIComponent(id)}/state`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'failed');
      status.textContent = data.state ? 'Currently on' : 'Currently off';
    } catch (err) {
      status.classList.add('error-text');
      status.textContent = String(err.message || err);
    }
  }

  async function test(id, status) {
    status.classList.remove('error-text');
    status.textContent = 'Testing…';
    try {
      const r = await fetch(`/api/devices/${encodeURIComponent(id)}/test`, { method: 'POST' });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'failed');
      const info = data.info || {};
      const bits = [info.model, info.firmware && ('fw ' + info.firmware)].filter(Boolean);
      status.textContent = 'Reached it' + (bits.length ? ' — ' + bits.join(', ') : '')
        + (data.state == null ? '' : (data.state ? ' · on' : ' · off'));
    } catch (err) {
      status.classList.add('error-text');
      status.textContent = String(err.message || err);
    }
  }

  // --- add form ------------------------------------------------------------
  if (addBtn) addBtn.addEventListener('click', showAddDialog);

  function showAddDialog() {
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h2', null, 'Add device'));
    const close = el('button', 'modal-close', '×');
    close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);

    const body = el('div', 'modal-body');
    const form = el('form', 'settings-grid');

    const name = field(form, 'Name', 'text', 'Porch floodlight');
    const host = field(form, 'Address', 'text', '192.168.1.50');
    host.nextHint = 'The device’s LAN IP. Reserve it on your router so it cannot move.';

    const driverWrap = el('label', 'set-field');
    driverWrap.appendChild(el('span', 'set-label', 'Type'));
    const driverSel = el('select', 'rec-select');
    for (const d of drivers) {
      const opt = el('option', null, d.label);
      opt.value = d.value;
      driverSel.appendChild(opt);
    }
    driverWrap.appendChild(driverSel);
    form.appendChild(driverWrap);

    const channel = field(form, 'Channel', 'number', '0');
    channel.value = '0';

    const user = field(form, 'Username (optional)', 'text', 'admin');
    const pass = field(form, 'Password (optional)', 'password', '');

    body.appendChild(form);
    const hint = el('p', 'muted small', 'Only set a username and password if you enabled authentication on the device.');
    hint.style.margin = '4px 0 0';
    body.appendChild(hint);

    const row = el('div', 'row');
    row.style.cssText = 'gap:10px;margin-top:14px;justify-content:flex-end;align-items:center';
    const err = el('span', 'small error-text');
    const save = el('button', 'btn btn-primary', 'Add device');
    save.type = 'button';
    row.appendChild(err);
    row.appendChild(save);
    body.appendChild(row);
    modal.appendChild(body);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);
    name.focus();

    const dismiss = () => backdrop.remove();
    close.addEventListener('click', dismiss);
    backdrop.addEventListener('pointerdown', e => { if (e.target === backdrop) dismiss(); });

    save.addEventListener('click', async () => {
      err.textContent = '';
      save.disabled = true;
      try {
        const r = await fetch('/api/devices', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: name.value.trim(), host: host.value.trim(),
            driver: driverSel.value, channel: Number(channel.value) || 0,
            username: user.value.trim(), password: pass.value,
          }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || 'could not add device');
        dismiss();
        load();
      } catch (e) {
        err.textContent = String(e.message || e);
      } finally {
        save.disabled = false;
      }
    });
  }

  function field(form, label, type, placeholder) {
    const wrap = el('label', 'set-field');
    wrap.appendChild(el('span', 'set-label', label));
    const input = el('input', 'rec-select');
    input.type = type;
    if (placeholder) input.placeholder = placeholder;
    wrap.appendChild(input);
    form.appendChild(wrap);
    return input;
  }

  load();
}
window.initDevices = initDevices;
