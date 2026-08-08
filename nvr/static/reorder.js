/*
 * Drag-to-reorder for the camera grid. Each tile carries a small grip handle
 * (the ⠿ at its top); dragging that handle rearranges the grid and POSTs the
 * new order. A plain click anywhere on the tile still opens it — only the grip
 * starts a drag, so the two never fight.
 *
 * Cameras and virtual cameras share one order space: every tile carries
 * data-reorder="cam:<id>" or "vcam:<id>", and the saved order is the sequence
 * of those tokens. The server stores it as sort_order on each row, so the
 * dashboard and wall show the same interleaved arrangement.
 *
 * Native HTML5 drag-and-drop (desktop).
 */
function initReorder(opts) {
  const grid = document.querySelector(opts.grid);
  if (!grid) return;
  const sel = opts.item || '[data-reorder]';
  const url = opts.url || '/api/cameras/order';

  let dragEl = null;
  let dragged = false;   // did the pointer actually move a tile this drag?

  // Only the grip is draggable, and a click on it must not open the camera.
  grid.querySelectorAll(sel).forEach((el) => {
    const handle = el.querySelector('[data-drag-handle]');
    if (!handle) return;
    handle.draggable = true;
    handle.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); });
  });

  grid.addEventListener('dragstart', (e) => {
    const handle = e.target.closest('[data-drag-handle]');
    if (!handle) return;                       // drags only start from the grip
    const el = handle.closest(sel);
    if (!el || !grid.contains(el)) return;
    dragEl = el;
    dragged = false;
    el.classList.add('dragging');
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', el.dataset.reorder || ''); } catch (_) {}
      // Drag the whole tile as the ghost, grabbed under the cursor.
      try {
        const b = el.getBoundingClientRect();
        e.dataTransfer.setDragImage(el, e.clientX - b.left, e.clientY - b.top);
      } catch (_) {}
    }
  });

  grid.addEventListener('dragover', (e) => {
    if (!dragEl) return;
    e.preventDefault();                        // allow the drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    const target = closestTile(e.clientX, e.clientY);
    if (!target || target.el === dragEl) return;
    dragged = true;
    grid.insertBefore(dragEl, target.before ? target.el : target.el.nextSibling);
  });

  grid.addEventListener('drop', (e) => { if (dragEl) e.preventDefault(); });

  grid.addEventListener('dragend', () => {
    if (!dragEl) return;
    dragEl.classList.remove('dragging');
    dragEl = null;
    if (dragged) save();
    // Keep `dragged` true just long enough to swallow the synthetic click some
    // browsers fire after a drag, then clear it. The click (if any) is
    // dispatched before this macrotask runs.
    setTimeout(() => { dragged = false; }, 0);
  });

  // Belt-and-suspenders: don't navigate a tile if a drag just happened.
  grid.addEventListener('click', (e) => {
    if (dragged) { e.preventDefault(); e.stopPropagation(); }
  }, true);

  // The tile nearest the cursor, and whether the drop goes before or after it.
  function closestTile(x, y) {
    let best = null;
    grid.querySelectorAll(sel + ':not(.dragging)').forEach((el) => {
      const b = el.getBoundingClientRect();
      const cx = b.left + b.width / 2, cy = b.top + b.height / 2;
      const d = Math.hypot(x - cx, y - cy);
      if (best && d >= best.d) return;
      const before = y < b.top ? true : y > b.bottom ? false : x < cx;
      best = { d, el, before };
    });
    return best;
  }

  let pending = null;
  function save() {
    const order = [...grid.querySelectorAll(sel)].map((el) => el.dataset.reorder);
    clearTimeout(pending);
    pending = setTimeout(() => {
      fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order }),
      }).catch(() => {});
    }, 150);
  }
}
window.initReorder = initReorder;
