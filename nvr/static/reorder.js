/*
 * Drag-to-reorder for the camera grid. Tiles are <a> links (a plain click
 * still opens the camera); pressing and dragging one rearranges the grid and
 * POSTs the new order. The server stores it as each camera's sort_order, so
 * the dashboard and wall — which read cameras in that order — follow along.
 *
 * Uses native HTML5 drag-and-drop (desktop). Only the real-camera tiles carry
 * data-reorder, so virtual-camera tiles stay put at the end and are never
 * included in the saved order.
 */
function initReorder(opts) {
  const grid = document.querySelector(opts.grid);
  if (!grid) return;
  const sel = opts.item || '[data-reorder]';
  const url = opts.url || '/api/cameras/order';

  let dragEl = null;
  let dragged = false;   // did the pointer actually move a tile this drag?

  grid.querySelectorAll(sel).forEach((el) => { el.draggable = true; });

  grid.addEventListener('dragstart', (e) => {
    const el = e.target.closest(sel);
    if (!el || !grid.contains(el)) return;
    dragEl = el;
    dragged = false;
    el.classList.add('dragging');
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      // Some browsers need data set for a drag to start at all.
      try { e.dataTransfer.setData('text/plain', el.dataset.reorder || ''); } catch (_) {}
    }
  });

  grid.addEventListener('dragover', (e) => {
    if (!dragEl) return;
    e.preventDefault();                       // allow the drop
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
    // browsers fire after a drag, then clear it so the next real click on a
    // tile still opens the camera. The click (if any) is dispatched before this
    // macrotask runs.
    setTimeout(() => { dragged = false; }, 0);
  });

  // Don't navigate the anchor when the user was rearranging. Capture phase so
  // it beats the link.
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
