/*
 * Live viewer counts. Subscribes to the presence SSE stream and updates every
 * [data-presence] badge the instant the server pushes a change — no polling.
 * A badge's data-presence is a camera id, or "vcam:<id>" for a virtual camera.
 */
function initPresence() {
  function apply(counts) {
    document.querySelectorAll('[data-presence]').forEach(el => {
      const c = counts[el.dataset.presence];
      if (c && c.watching > 0) {
        el.textContent = `👁 ${c.watching}` + (c.listening ? ` 🔊 ${c.listening}` : '');
        el.title = `${c.watching} watching`
          + (c.listening ? `, ${c.listening} with audio` : '');
        el.hidden = false;
      } else {
        el.hidden = true;
      }
    });
  }

  let es = null;
  function connect() {
    try { es = new EventSource('/api/presence/stream'); } catch (_) { return; }
    es.onmessage = e => { try { apply(JSON.parse(e.data)); } catch (_) {} };
    es.onerror = () => {
      // EventSource auto-reconnects, but if the connection is closed (e.g. a
      // proxy dropped it) re-open after a short delay.
      if (es && es.readyState === EventSource.CLOSED) {
        setTimeout(connect, 3000);
      }
    };
  }
  connect();
}

window.initPresence = initPresence;
