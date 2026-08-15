/*
 * Tasks: a shared household to-do board.
 *
 * The columns are the *thing* the work belongs to — the house, the boat, the
 * car — not a workflow stage. For a household that sorts the work usefully,
 * whereas To-do/Doing/Done mostly creates a column nobody moves cards out of.
 * "Done" is therefore a checkbox on the card, not a place cards go.
 *
 * Everyone signed in sees and edits everything, like the shared calendar. A
 * family list where you cannot tick off a job somebody else wrote down is not
 * a household feature.
 *
 * All text is set with textContent — titles, notes and list names are typed by
 * people.
 */
function initTasks() {
  const board = document.getElementById('task-board');
  if (!board) return;

  const alertEl = document.getElementById('task-alert');
  const modal = document.getElementById('task-modal');
  const modalTitle = document.getElementById('task-modal-title');
  const modalBody = document.getElementById('task-modal-body');
  const showDone = document.getElementById('task-show-done');
  const onlyMine = document.getElementById('task-mine');

  let state = { lists: [], tasks: [], users: [], me: null, can_edit_lists: false };

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
    if (!bad) setTimeout(() => alertEl.replaceChildren(), 4000);
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
      state = await api('/api/tasks');
    } catch {
      board.replaceChildren(el('div', 'muted small', 'Could not load tasks.'));
      return;
    }
    render();
  }

  // --- due dates -----------------------------------------------------------

  const DAY = 86400;

  function startOfToday() {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d.getTime() / 1000;
  }

  function describeDue(due) {
    if (due == null) return null;
    const today = startOfToday();
    const days = Math.floor((due - today) / DAY);
    if (days < 0) return { text: days === -1 ? 'Yesterday' : `${-days} days overdue`, tone: 'overdue' };
    if (days === 0) return { text: 'Today', tone: 'today' };
    if (days === 1) return { text: 'Tomorrow', tone: 'soon' };
    if (days <= 7) return { text: `In ${days} days`, tone: 'soon' };
    return {
      text: new Date(due * 1000).toLocaleDateString(undefined,
        { month: 'short', day: 'numeric' }),
      tone: '',
    };
  }

  // --- rendering -----------------------------------------------------------

  function visibleTasks() {
    return state.tasks.filter(t => {
      if (!showDone.checked && t.done) return false;
      if (onlyMine.checked && t.assignee_id !== state.me) return false;
      return true;
    });
  }

  function render() {
    board.replaceChildren();
    const tasks = visibleTasks();
    const columns = [...state.lists.map(l => ({ list: l, id: l.id }))];
    const loose = tasks.filter(t => t.list_id == null);
    if (loose.length) columns.push({ list: { id: null, name: 'Uncategorised' }, id: null });

    for (const column of columns) {
      board.appendChild(columnEl(column.list,
        tasks.filter(t => t.list_id === column.id)));
    }
    if (!columns.length) {
      board.appendChild(el('div', 'muted small', 'No lists yet.'));
    }
  }

  function columnEl(list, tasks) {
    const col = el('div', 'task-col panel');

    const head = el('div', 'list-item task-col-head');
    if (list.color) head.style.borderTopColor = list.color;
    const grow = el('div', 'grow');
    grow.appendChild(el('h2', null, list.name));
    const open = tasks.filter(t => !t.done).length;
    grow.appendChild(el('div', 'muted small',
      open ? `${open} open` : 'nothing outstanding'));
    head.appendChild(grow);

    const add = el('button', 'btn btn-sm', '+');
    add.type = 'button';
    add.title = `Add a task to ${list.name}`;
    add.setAttribute('aria-label', `Add a task to ${list.name}`);
    add.addEventListener('click', () => showDialog(null, list.id));
    head.appendChild(add);

    if (state.can_edit_lists && list.id !== null) {
      const remove = el('button', 'remove-x', '×');
      remove.title = 'Delete list';
      remove.setAttribute('aria-label', 'Delete list');
      remove.addEventListener('click', async () => {
        if (!confirm(`Delete the ${list.name} list? Its tasks move to `
          + 'Uncategorised — nothing is lost.')) return;
        try {
          await api(`/api/tasks/lists/${list.id}`, { method: 'DELETE' });
          await load();
        } catch (err) { say(err.message, true); }
      });
      head.appendChild(remove);
    }
    col.appendChild(head);

    const body = el('div', 'task-col-body');
    if (!tasks.length) {
      body.appendChild(el('div', 'muted small task-empty', 'Nothing here.'));
    } else {
      for (const task of tasks) body.appendChild(taskCard(task));
    }
    col.appendChild(body);
    return col;
  }

  function taskCard(task) {
    const card = el('div', 'task-card');
    if (task.done) card.classList.add('is-done');

    const top = el('div', 'task-card-top');
    const tick = document.createElement('input');
    tick.type = 'checkbox';
    tick.checked = task.done;
    tick.title = task.done ? 'Mark as not done' : 'Mark done';
    tick.setAttribute('aria-label', `Mark "${task.title}" done`);
    tick.addEventListener('change', async () => {
      tick.disabled = true;
      try {
        await send(`/api/tasks/${task.id}`, 'PATCH', { done: tick.checked });
        await load();
      } catch (err) {
        say(err.message, true);
        tick.checked = !tick.checked;
      } finally {
        tick.disabled = false;
      }
    });
    top.appendChild(tick);

    const text = el('div', 'grow');
    text.appendChild(el('div', 'task-title', task.title));
    const meta = el('div', 'task-meta');
    if (task.assignee) meta.appendChild(el('span', 'pill', task.assignee));
    const due = describeDue(task.due);
    if (due && !task.done) {
      meta.appendChild(el('span', 'task-due ' + (due.tone ? 'task-due-' + due.tone : ''),
        due.text));
    }
    if (meta.childElementCount) text.appendChild(meta);
    if (task.notes) text.appendChild(el('div', 'muted small task-notes', task.notes));
    top.appendChild(text);

    card.appendChild(top);

    // The whole card opens the task. The checkbox is the one thing inside it
    // that means something else, so it stops the click from bubbling — ticking
    // a task off should not also open it.
    tick.addEventListener('click', e => e.stopPropagation());
    card.classList.add('task-card-open');
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', `Open "${task.title}"`);
    const open = () => showDetail(task);
    card.addEventListener('click', open);
    card.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
    return card;
  }

  // --- detail --------------------------------------------------------------

  function fullDate(seconds) {
    return new Date(seconds * 1000).toLocaleDateString(undefined, {
      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
    });
  }

  function listName(listId) {
    const found = state.lists.find(l => l.id === listId);
    return found ? found.name : 'Uncategorised';
  }

  function detailRow(key, value) {
    const row = el('div', 'task-detail-row');
    row.appendChild(el('span', 'k', key));
    row.appendChild(el('span', 'v', value));
    return row;
  }

  function showDetail(task) {
    const body = openModal(task.title);
    body.appendChild(detailRow('List', listName(task.list_id)));
    body.appendChild(detailRow('Assigned to', task.assignee || 'Anyone'));
    const due = describeDue(task.due);
    body.appendChild(detailRow('Due',
      task.due == null ? 'No due date' : `${fullDate(task.due)} (${due.text})`));
    body.appendChild(detailRow('Status', task.done ? 'Done' : 'Outstanding'));
    if (task.done && task.done_utc) {
      body.appendChild(detailRow('Completed', fullDate(task.done_utc)));
    }

    if (task.notes) {
      body.appendChild(el('div', 'muted small', 'Notes'));
      body.appendChild(el('div', 'task-detail-notes', task.notes));
    }

    const actions = el('div', 'row');
    actions.style.cssText = 'gap:8px;margin-top:16px';

    const toggle = el('button', 'btn', task.done ? 'Reopen' : 'Mark done');
    toggle.type = 'button';
    toggle.addEventListener('click', async () => {
      toggle.disabled = true;
      try {
        await send(`/api/tasks/${task.id}`, 'PATCH', { done: !task.done });
        closeModal();
        await load();
      } catch (err) { say(err.message, true); }
      finally { toggle.disabled = false; }
    });
    actions.appendChild(toggle);

    const edit = el('button', 'btn btn-primary', 'Edit');
    edit.type = 'button';
    edit.addEventListener('click', () => showDialog(task, task.list_id));
    actions.appendChild(edit);
    body.appendChild(actions);
  }

  // --- dialog --------------------------------------------------------------

  const modalBox = modal.querySelector('.modal');

  function openModal(title, wide) {
    modalTitle.textContent = title;
    modalBody.replaceChildren();
    modalBox.classList.toggle('modal-wide', Boolean(wide));
    modal.hidden = false;
    return modalBody;
  }

  const closeModal = () => { modal.hidden = true; };
  document.getElementById('task-modal-close').addEventListener('click', closeModal);
  modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });

  function field(labelText, input) {
    const wrap = el('div', 'field');
    wrap.appendChild(el('label', null, labelText));
    wrap.appendChild(input);
    return wrap;
  }

  function isoDate(seconds) {
    const d = new Date(seconds * 1000);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function showDialog(task, listId) {
    const body = openModal(task ? 'Edit task' : 'Add task', true);

    const title = document.createElement('input');
    title.type = 'text';
    title.value = task ? task.title : '';
    title.placeholder = 'Change the oil';
    body.appendChild(field('Task', title));

    // The three short fields pair up across the wide modal; notes gets the
    // full width underneath, which is what it actually needs.
    const grid = el('div', 'modal-grid');

    const list = document.createElement('select');
    const none = document.createElement('option');
    none.value = '';
    none.textContent = 'Uncategorised';
    list.appendChild(none);
    for (const l of state.lists) {
      const o = document.createElement('option');
      o.value = String(l.id);
      o.textContent = l.name;
      if ((task ? task.list_id : listId) === l.id) o.selected = true;
      list.appendChild(o);
    }
    grid.appendChild(field('List', list));

    const who = document.createElement('select');
    const anyone = document.createElement('option');
    anyone.value = '';
    anyone.textContent = 'Anyone';
    who.appendChild(anyone);
    for (const u of state.users) {
      const o = document.createElement('option');
      o.value = String(u.id);
      o.textContent = u.username;
      if (task && task.assignee_id === u.id) o.selected = true;
      who.appendChild(o);
    }
    grid.appendChild(field('Who', who));

    const due = document.createElement('input');
    due.type = 'date';
    if (task && task.due != null) due.value = isoDate(task.due);
    grid.appendChild(field('Due', due));
    body.appendChild(grid);
    body.appendChild(el('p', 'muted small',
      'A due date puts this on the calendar. Leave it blank if it just needs '
      + 'doing sometime.'));

    const notes = document.createElement('textarea');
    notes.rows = 6;
    notes.value = task && task.notes ? task.notes : '';
    body.appendChild(field('Notes', notes));

    const save = el('button', 'btn btn-primary', task ? 'Save' : 'Add task');
    save.type = 'button';
    save.addEventListener('click', async () => {
      if (!title.value.trim()) { say('Give the task a name.', true); return; }
      // A date input yields YYYY-MM-DD; parse as LOCAL midnight, since
      // new Date('2026-08-15') would be UTC and can land on the day before.
      let dueSeconds = null;
      if (due.value) {
        const [y, m, d] = due.value.split('-').map(Number);
        dueSeconds = new Date(y, m - 1, d).getTime() / 1000;
      }
      const payload = {
        title: title.value.trim(),
        list_id: list.value === '' ? null : Number(list.value),
        assignee_id: who.value === '' ? null : Number(who.value),
        due: dueSeconds,
        notes: notes.value.trim(),
      };
      save.disabled = true;
      try {
        if (task) await send(`/api/tasks/${task.id}`, 'PATCH', payload);
        else await send('/api/tasks', 'POST', payload);
        closeModal();
        await load();
      } catch (err) {
        say(err.message, true);
      } finally {
        save.disabled = false;
      }
    });
    body.appendChild(save);

    if (task) {
      const del = el('button', 'btn btn-danger', 'Delete task');
      del.type = 'button';
      del.addEventListener('click', async () => {
        if (!confirm(`Delete "${task.title}"?`)) return;
        try {
          await api(`/api/tasks/${task.id}`, { method: 'DELETE' });
          closeModal();
          await load();
        } catch (err) { say(err.message, true); }
      });
      body.appendChild(del);
    }
    title.focus();
  }

  // --- wiring --------------------------------------------------------------

  document.getElementById('task-add').addEventListener('click', () =>
    showDialog(null, state.lists.length ? state.lists[0].id : null));

  showDone.addEventListener('change', render);
  onlyMine.addEventListener('change', render);

  const addList = document.getElementById('task-add-list');
  if (addList) {
    addList.addEventListener('click', async () => {
      const name = prompt('List name (e.g. House, Boat, Car)');
      if (!name || !name.trim()) return;
      try {
        await send('/api/tasks/lists', 'POST', { name: name.trim() });
        await load();
      } catch (err) { say(err.message, true); }
    });
  }

  load();
}
