/*
 * Network inventory: what is on the LAN, and which of it Sentry manages.
 *
 * The scan is read-only — one datagram per address to make the kernel resolve
 * it, then the ARP cache is read. Manufacturer names come from the IEEE
 * registry on disk, so no part of this tells anyone else what is in the house.
 */
function initNetwork() {
  const table = document.getElementById('net-table');
  if (!table) return;
  const alertEl = document.getElementById('net-alert');
  const button = document.getElementById('net-scan');

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

  async function scan() {
    button.disabled = true;
    // A sweep touches every address on the subnet and waits for ARP to settle,
    // so it is slow enough that saying nothing reads as broken.
    say('Sweeping the network — this takes about half a minute.', false, true);
    let data;
    try {
      const r = await fetch('/api/network/scan', { method: 'POST' });
      data = await r.json();
      if (!r.ok) throw new Error(data.error || 'scan failed');
    } catch (err) {
      say(err.message, true);
      return;
    } finally {
      button.disabled = false;
    }
    render(data);
    say(`${data.devices.length} answering · ${data.unknown} not yet identified.`);
  }

  function render(data) {
    table.replaceChildren();
    if (!data.devices.length) {
      table.appendChild(el('div', 'list-item muted small', 'Nothing answered.'));
      return;
    }
    for (const d of data.devices) {
      const row = el('div', 'list-item');
      row.appendChild(el('span', 'dot ' + (d.known_kind ? 'dot-ok' : 'dot-off')));

      const grow = el('div', 'grow');
      const title = el('div', 'title', d.known_name || d.vendor || 'Unknown device');
      if (d.known_kind) title.appendChild(el('span', 'pill', d.known_kind));
      else if (d.randomised) title.appendChild(el('span', 'pill pill-off', 'randomised'));
      grow.appendChild(title);

      const bits = [d.address, d.mac];
      if (d.known_name && d.vendor) bits.push(d.vendor);
      grow.appendChild(el('div', 'sub muted small', bits.join(' · ')));
      row.appendChild(grow);
      table.appendChild(row);
    }
  }

  button.addEventListener('click', scan);
}
