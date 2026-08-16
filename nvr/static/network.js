/*
 * Network inventory: what is on the LAN, and what each thing actually is.
 *
 * The scan is read-only — one datagram per address to make the kernel resolve
 * it, then the ARP cache is read. Manufacturer names come from the IEEE
 * registry on disk, so nothing here tells anyone else what is in the house.
 *
 * Labels are stored against the MAC, never the address, so "Kitchen
 * dishwasher" survives a DHCP lease change. The vendor is only ever a fallback:
 * "GE Appliances" is a guess, and only a person knows which appliance it is.
 */
function initNetwork() {
  const table = document.getElementById('net-table');
  if (!table) return;

  const alertEl = document.getElementById('net-alert');
  const scanBtn = document.getElementById('net-scan');
  const filter = document.getElementById('net-filter');
  const summary = document.getElementById('net-summary');
  const modal = document.getElementById('net-modal');
  const modalBox = modal.querySelector('.modal');
  const modalTitle = document.getElementById('net-modal-title');
  const modalBody = document.getElementById('net-modal-body');

  let state = { devices: [], kinds: [], unknown: 0, stale: false };

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  function say(message, bad, sticky) {
    alertEl.replaceChildren();
    if (!message) return;
    alertEl.appendChild(el('div', 'alert ' + (bad ? 'alert-error' : 'alert-info'), message));
    if (!bad && !sticky) setTimeout(() => alertEl.replaceChildren(), 5000);
  }

  async function api(url, options) {
    const r = await fetch(url, options);
    let data = {};
    try { data = await r.json(); } catch { /* empty body is fine */ }
    if (!r.ok) throw new Error(data.error || `request failed (${r.status})`);
    return data;
  }

  // --- loading -------------------------------------------------------------

  async function loadKnown() {
    try {
      state = await api('/api/network/devices');
    } catch (err) {
      table.replaceChildren(el('div', 'list-item muted small',
        'Could not load the device list.'));
      return;
    }
    render();
  }

  async function scan() {
    scanBtn.disabled = true;
    // The sweep touches every address and waits for ARP to settle, so it is
    // slow enough that saying nothing reads as broken.
    say('Sweeping the network — this takes about half a minute.', false, true);
    try {
      state = await api('/api/network/scan', { method: 'POST' });
      say(`${state.devices.length} answering · ${state.unknown} still unidentified.`);
    } catch (err) {
      say(err.message, true);
    } finally {
      scanBtn.disabled = false;
    }
    render();
  }

  // --- rendering -----------------------------------------------------------

  function iconFor(device) {
    if (device.icon && device.kind !== 'unknown') return device.icon;
    if (device.randomised) return '📱';
    return '❓';
  }

  function isNew(device) {
    // Something that first appeared in the last day is worth a second look in
    // a house that runs a security system.
    if (!device.first_seen) return false;
    return (Date.now() / 1000 - device.first_seen) < 86400;
  }

  function visible() {
    const mode = filter.value;
    return state.devices.filter(d => {
      if (mode === 'unidentified') {
        return !(d.label || d.known_kind || d.kind !== 'unknown'
                 || d.dismissed || d.randomised);
      }
      if (mode === 'iot') return !d.randomised;
      return true;
    });
  }

  function render() {
    table.replaceChildren();
    const rows = visible();
    summary.textContent = state.devices.length
      ? `${state.devices.length} device(s) · ${state.unknown} unidentified`
        + (state.stale ? ' · last known, not just scanned' : '')
      : '';
    if (!rows.length) {
      table.appendChild(el('div', 'list-item muted small',
        state.devices.length ? 'Nothing matches that filter.'
          : 'Nothing recorded yet — run a scan.'));
      return;
    }
    for (const device of rows) table.appendChild(row(device));
  }

  function row(device) {
    const item = el('div', 'list-item net-row');
    item.appendChild(el('span', 'net-icon', iconFor(device)));

    const grow = el('div', 'grow');
    const title = el('div', 'title', device.display);
    if (device.known_kind) {
      title.appendChild(el('span', 'pill', 'in Sentry'));
    }
    if (isNew(device)) title.appendChild(el('span', 'pill', 'new'));
    if (device.randomised) {
      title.appendChild(el('span', 'pill pill-off', 'randomised'));
    }
    if (device.dismissed) title.appendChild(el('span', 'pill pill-off', 'ignored'));
    grow.appendChild(title);

    const bits = [device.address || 'not seen', device.mac];
    if (device.vendor && device.vendor !== device.display) bits.push(device.vendor);
    grow.appendChild(el('div', 'sub muted small', bits.join(' · ')));
    if (device.notes) {
      grow.appendChild(el('div', 'sub muted small net-notes', device.notes));
    }
    item.appendChild(grow);

    item.classList.add('net-row-open');
    item.tabIndex = 0;
    item.setAttribute('role', 'button');
    item.setAttribute('aria-label', `Edit ${device.display}`);
    const open = () => showEditor(device);
    item.addEventListener('click', open);
    item.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
    return item;
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
  document.getElementById('net-modal-close').addEventListener('click', closeModal);
  modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });

  function field(labelText, input) {
    const wrap = el('div', 'field');
    wrap.appendChild(el('label', null, labelText));
    wrap.appendChild(input);
    return wrap;
  }

  function detailRow(key, value) {
    const r = el('div', 'task-detail-row');
    r.appendChild(el('span', 'k', key));
    r.appendChild(el('span', 'v', value));
    return r;
  }

  function fmtWhen(seconds) {
    if (!seconds) return 'never';
    return new Date(seconds * 1000).toLocaleString();
  }

  function showEditor(device) {
    const body = openModal(device.display);

    body.appendChild(detailRow('Address', device.address || 'not seen'));
    body.appendChild(detailRow('MAC', device.mac));
    body.appendChild(detailRow('Manufacturer', device.vendor || 'unknown'));
    if (device.known_name) {
      body.appendChild(detailRow('Managed by Sentry',
        `${device.known_name} (${device.known_kind})`));
    }
    body.appendChild(detailRow('First seen', fmtWhen(device.first_seen)));
    body.appendChild(detailRow('Last seen', fmtWhen(device.last_seen)));
    if (device.randomised) {
      body.appendChild(el('p', 'muted small',
        'This is a privacy-randomised address. Phones and laptops rotate these '
        + 'per network, so a label here will stop matching when it changes.'));
    }

    const name = document.createElement('input');
    name.type = 'text';
    name.value = device.label || '';
    name.placeholder = device.vendor ? `e.g. Kitchen ${device.vendor}` : 'e.g. Garage freezer';
    body.appendChild(field('What is it called?', name));

    const kind = document.createElement('select');
    for (const k of state.kinds) {
      const option = document.createElement('option');
      option.value = k.value;
      option.textContent = `${k.icon}  ${k.label}`;
      if (k.value === device.kind) option.selected = true;
      kind.appendChild(option);
    }
    body.appendChild(field('What kind of thing?', kind));

    const notes = document.createElement('textarea');
    notes.rows = 3;
    notes.value = device.notes || '';
    notes.placeholder = 'Where it lives, what it is for, anything worth remembering.';
    body.appendChild(field('Notes', notes));

    const ignore = el('label', 'checkbox');
    const ignoreBox = document.createElement('input');
    ignoreBox.type = 'checkbox';
    ignoreBox.checked = device.dismissed;
    ignore.appendChild(ignoreBox);
    ignore.appendChild(el('span', null, 'Stop counting this as unidentified'));
    body.appendChild(ignore);

    const save = el('button', 'btn btn-primary', 'Save');
    save.type = 'button';
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        await api(`/api/network/devices/${encodeURIComponent(device.mac)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            label: name.value.trim(),
            kind: kind.value,
            notes: notes.value.trim(),
            dismissed: ignoreBox.checked,
            address: device.address,
          }),
        });
        closeModal();
        await loadKnown();
      } catch (err) {
        say(err.message, true);
      } finally {
        save.disabled = false;
      }
    });
    body.appendChild(save);

    const forget = el('button', 'btn btn-danger', 'Forget this device');
    forget.type = 'button';
    forget.addEventListener('click', async () => {
      if (!confirm(`Forget ${device.display}? Its label and notes are lost; if `
        + 'it is still on the network the next scan will find it again, '
        + 'unidentified.')) return;
      try {
        await api(`/api/network/devices/${encodeURIComponent(device.mac)}`,
                  { method: 'DELETE' });
        closeModal();
        await loadKnown();
      } catch (err) { say(err.message, true); }
    });
    body.appendChild(forget);
    name.focus();
  }

  scanBtn.addEventListener('click', scan);
  filter.addEventListener('change', render);
  loadKnown();
}
