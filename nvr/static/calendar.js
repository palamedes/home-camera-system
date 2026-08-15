/*
 * Household calendar: a month grid, and a side panel showing one day at a time.
 *
 * Hand-rolled rather than pulling in a calendar library — the whole app has no
 * JS dependencies and no build step, and a month grid is far simpler than the
 * canvas timeline or the WebGL dewarp already in here. The API it talks to is
 * the ordinary one, so swapping in a library later would not change the server.
 *
 * Times cross the wire as epoch seconds and are rendered in the browser's local
 * zone. Everyone in a household shares a timezone, so that avoids dragging in a
 * timezone library for no benefit.
 */
function initCalendar() {
  const grid = document.getElementById('cal-grid');
  if (!grid) return;

  const monthLabel = document.getElementById('cal-month');
  const legend = document.getElementById('cal-legend');
  const dayTitle = document.getElementById('cal-day-title');
  const dayEvents = document.getElementById('cal-day-events');

  const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  let calendars = [];
  let events = [];
  let cursor = startOfMonth(new Date());
  let selected = null;          // Date of the day shown in the side panel

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;   // never innerHTML: user text
    return node;
  };

  function startOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
  function addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, 1); }
  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
        && a.getDate() === b.getDate();
  }
  const dayKey = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

  // The grid always starts on a Monday and runs whole weeks, so the month sits
  // in a stable 6-row shape instead of jumping height as you page through.
  function gridStart(month) {
    const first = startOfMonth(month);
    const offset = (first.getDay() + 6) % 7;     // JS weeks start Sunday
    return new Date(first.getFullYear(), first.getMonth(), 1 - offset);
  }

  function fmtTime(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: 'numeric', minute: '2-digit',
    });
  }

  function colorOf(calendarId) {
    const cal = calendars.find(c => c.id === calendarId);
    return cal ? cal.color : '#64748b';
  }
  function nameOf(calendarId) {
    const cal = calendars.find(c => c.id === calendarId);
    return cal ? cal.name : 'Calendar';
  }

  // --- loading -------------------------------------------------------------

  async function load() {
    const from = gridStart(cursor);
    const to = new Date(from.getFullYear(), from.getMonth(), from.getDate() + 42);
    try {
      const [cals, evs] = await Promise.all([
        fetch('/api/calendar/calendars').then(r => r.json()),
        fetch(`/api/calendar/events?start=${from.getTime() / 1000}&end=${to.getTime() / 1000}`)
          .then(r => r.json()),
      ]);
      calendars = Array.isArray(cals) ? cals : [];
      events = Array.isArray(evs) ? evs : [];
    } catch {
      events = [];
    }
    renderLegend();
    renderGrid();
    if (selected) showDay(selected);
  }

  function renderLegend() {
    legend.replaceChildren();
    for (const cal of calendars) {
      const chip = el('span', 'cal-chip');
      const dot = el('span', 'cal-dot');
      dot.style.background = cal.color;
      chip.appendChild(dot);
      chip.appendChild(el('span', null, cal.name + (cal.shared ? '' : ' (private)')));
      legend.appendChild(chip);
    }
  }

  // An event belongs to a day if it overlaps that day at all, so a multi-day
  // trip appears on each of its days rather than only the first.
  function eventsOn(day) {
    const from = new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime() / 1000;
    const to = from + 86400;
    return events
      .filter(e => e.start < to && e.end > from)
      .sort((a, b) => (a.all_day === b.all_day) ? a.start - b.start : (a.all_day ? -1 : 1));
  }

  function renderGrid() {
    monthLabel.textContent = cursor.toLocaleDateString([], {
      month: 'long', year: 'numeric',
    });
    grid.replaceChildren();

    for (const name of DAY_NAMES) grid.appendChild(el('div', 'cal-dow', name));

    const today = new Date();
    const start = gridStart(cursor);
    for (let i = 0; i < 42; i++) {
      const day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      const cell = el('button', 'cal-day');
      cell.type = 'button';
      if (day.getMonth() !== cursor.getMonth()) cell.classList.add('other-month');
      if (sameDay(day, today)) cell.classList.add('today');
      if (selected && sameDay(day, selected)) cell.classList.add('selected');
      cell.dataset.day = dayKey(day);

      cell.appendChild(el('span', 'cal-daynum', String(day.getDate())));

      const list = el('span', 'cal-day-list');
      const todays = eventsOn(day);
      for (const event of todays.slice(0, 3)) {
        const pill = el('span', 'cal-event');
        const dot = el('span', 'cal-dot');
        if (event.kind === 'task') {
          // Hollow dot: a due date is a deadline, not an appointment, and at a
          // glance the two should not look like the same kind of commitment.
          dot.classList.add('cal-dot-task');
        } else {
          dot.style.background = colorOf(event.calendar_id);
        }
        pill.appendChild(dot);
        pill.appendChild(el('span', 'cal-event-title',
          (event.all_day ? '' : fmtTime(event.start) + ' ') + event.title));
        list.appendChild(pill);
      }
      if (todays.length > 3) {
        list.appendChild(el('span', 'cal-more', `+${todays.length - 3} more`));
      }
      cell.appendChild(list);

      cell.addEventListener('click', () => {
        selected = day;
        renderGrid();
        showDay(day);
      });
      grid.appendChild(cell);
    }
  }

  // --- day panel -----------------------------------------------------------

  function showDay(day) {
    selected = day;
    dayTitle.textContent = day.toLocaleDateString([], {
      weekday: 'long', month: 'long', day: 'numeric',
    });
    dayEvents.replaceChildren();

    const todays = eventsOn(day);
    if (!todays.length) {
      dayEvents.appendChild(el('p', 'muted small', 'Nothing on this day.'));
    }
    for (const event of todays) dayEvents.appendChild(eventCard(event));

    const add = el('button', 'btn btn-sm btn-primary', 'Add on this day');
    add.type = 'button';
    add.style.marginTop = '12px';
    add.addEventListener('click', () => showEventDialog(null, day));
    dayEvents.appendChild(add);
  }

  function eventCard(event) {
    const card = el('div', 'cal-card');

    // A task's due date rides along in the events feed but is not an event:
    // it lives in the task list, so it is shown read-only here with a link
    // back rather than Edit/Delete buttons that would edit the wrong thing.
    if (event.kind === 'task') return taskCard(event, card);

    card.style.borderLeftColor = colorOf(event.calendar_id);

    const head = el('div', 'cal-card-head');
    head.appendChild(el('strong', null, event.title));
    card.appendChild(head);

    const when = event.all_day
      ? 'All day'
      : `${fmtTime(event.start)} – ${fmtTime(event.end)}`;
    card.appendChild(el('div', 'muted small', when + ' · ' + nameOf(event.calendar_id)));

    if (event.location) card.appendChild(el('div', 'small', '📍 ' + event.location));
    if (event.description) card.appendChild(el('div', 'small cal-desc', event.description));

    const actions = el('div', 'row');
    actions.style.cssText = 'gap:8px;margin-top:8px';
    const edit = el('button', 'btn btn-sm', 'Edit');
    edit.type = 'button';
    edit.addEventListener('click', () => showEventDialog(event, null));
    actions.appendChild(edit);

    const del = el('button', 'btn btn-sm btn-danger', 'Delete');
    del.type = 'button';
    del.addEventListener('click', async () => {
      if (!confirm(`Delete "${event.title}"?`)) return;
      await fetch(`/api/calendar/events/${event.id}`, { method: 'DELETE' });
      load();
    });
    actions.appendChild(del);
    card.appendChild(actions);
    return card;
  }

  function taskCard(task, card) {
    card.classList.add('cal-card-task');
    const head = el('div', 'cal-card-head');
    head.appendChild(el('strong', null, task.title));
    head.appendChild(el('span', 'pill', 'Task'));
    card.appendChild(head);
    card.appendChild(el('div', 'muted small', 'Due today'));
    if (task.description) {
      card.appendChild(el('div', 'small cal-desc', task.description));
    }

    const actions = el('div', 'row');
    actions.style.cssText = 'gap:8px;margin-top:8px';
    const open = document.createElement('a');
    open.className = 'btn btn-sm';
    open.href = '/tasks';
    open.textContent = 'Open in Tasks';
    actions.appendChild(open);

    const done = el('button', 'btn btn-sm', 'Mark done');
    done.type = 'button';
    done.addEventListener('click', async () => {
      done.disabled = true;
      await fetch(`/api/tasks/${task.task_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ done: true }),
      });
      load();
    });
    actions.appendChild(done);
    card.appendChild(actions);
    return card;
  }

  // --- add / edit ----------------------------------------------------------

  function pad(n) { return String(n).padStart(2, '0'); }
  const dateValue = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const timeValue = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

  function epochFrom(dateStr, timeStr) {
    const [y, m, d] = dateStr.split('-').map(Number);
    const [hh, mm] = (timeStr || '00:00').split(':').map(Number);
    return new Date(y, m - 1, d, hh || 0, mm || 0).getTime() / 1000;
  }

  function showEventDialog(existing, day) {
    const writable = calendars.filter(c => c.shared || c.mine);
    if (!writable.length) {
      alert('No calendar to write to yet.');
      return;
    }
    const base = existing
      ? new Date(existing.start * 1000)
      : (day ? new Date(day.getFullYear(), day.getMonth(), day.getDate(), 18, 0)
             : new Date());
    const endBase = existing
      ? new Date(existing.end * 1000)
      : new Date(base.getTime() + 3600 * 1000);

    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h2', null, existing ? 'Edit event' : 'New event'));
    const close = el('button', 'modal-close', '×');
    close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);

    const body = el('div', 'modal-body');
    const form = el('form', 'settings-grid');

    const title = textField(form, 'Title', existing ? existing.title : '');
    const calSel = el('select', 'rec-select');
    for (const cal of writable) {
      const opt = el('option', null, cal.name + (cal.shared ? ' (shared)' : ''));
      opt.value = cal.id;
      if (existing && existing.calendar_id === cal.id) opt.selected = true;
      calSel.appendChild(opt);
    }
    wrapField(form, 'Calendar', calSel);

    const allDay = el('input');
    allDay.type = 'checkbox';
    allDay.checked = existing ? existing.all_day : false;
    wrapField(form, 'All day', allDay);

    const startDate = inputField(form, 'Starts', 'date', dateValue(base));
    const startTime = inputField(form, 'at', 'time', timeValue(base));
    const endDate = inputField(form, 'Ends', 'date', dateValue(endBase));
    const endTime = inputField(form, 'at', 'time', timeValue(endBase));

    const syncTimeFields = () => {
      const off = allDay.checked;
      startTime.disabled = off;
      endTime.disabled = off;
    };
    allDay.addEventListener('change', syncTimeFields);
    syncTimeFields();

    const location = textField(form, 'Location', existing ? (existing.location || '') : '');
    const notes = textField(form, 'Notes', existing ? (existing.description || '') : '');

    body.appendChild(form);
    const row = el('div', 'row');
    row.style.cssText = 'gap:10px;margin-top:14px;justify-content:flex-end;align-items:center';
    const err = el('span', 'small error-text');
    const save = el('button', 'btn btn-primary', existing ? 'Save' : 'Add event');
    save.type = 'button';
    row.appendChild(err);
    row.appendChild(save);
    body.appendChild(row);
    modal.appendChild(body);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);
    title.focus();

    const dismiss = () => backdrop.remove();
    close.addEventListener('click', dismiss);
    backdrop.addEventListener('pointerdown', e => { if (e.target === backdrop) dismiss(); });

    save.addEventListener('click', async () => {
      err.textContent = '';
      const isAllDay = allDay.checked;
      const startEpoch = epochFrom(startDate.value, isAllDay ? '00:00' : startTime.value);
      // An all-day event runs to the *end* of its last day, so a single-day one
      // still covers a real span rather than being zero-length.
      const endEpoch = isAllDay
        ? epochFrom(endDate.value, '00:00') + 86400
        : epochFrom(endDate.value, endTime.value);

      const payload = {
        calendar_id: calSel.value, title: title.value.trim(),
        description: notes.value.trim(), location: location.value.trim(),
        start: startEpoch, end: endEpoch, all_day: isAllDay,
      };
      save.disabled = true;
      try {
        const url = existing
          ? `/api/calendar/events/${existing.id}` : '/api/calendar/events';
        const r = await fetch(url, {
          method: existing ? 'PATCH' : 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || 'could not save');
        dismiss();
        load();
      } catch (e) {
        err.textContent = String(e.message || e);
      } finally {
        save.disabled = false;
      }
    });
  }

  function wrapField(form, label, control) {
    const wrap = el('label', 'set-field');
    wrap.appendChild(el('span', 'set-label', label));
    wrap.appendChild(control);
    form.appendChild(wrap);
    return control;
  }
  function inputField(form, label, type, value) {
    const input = el('input', 'rec-select');
    input.type = type;
    input.value = value;
    return wrapField(form, label, input);
  }
  function textField(form, label, value) {
    const input = el('input', 'rec-select');
    input.type = 'text';
    input.value = value || '';
    return wrapField(form, label, input);
  }

  // --- chrome --------------------------------------------------------------

  document.getElementById('cal-prev').addEventListener('click', () => {
    cursor = addMonths(cursor, -1); load();
  });
  document.getElementById('cal-next').addEventListener('click', () => {
    cursor = addMonths(cursor, 1); load();
  });
  document.getElementById('cal-today').addEventListener('click', () => {
    cursor = startOfMonth(new Date());
    selected = new Date();
    load();
  });
  document.getElementById('cal-add').addEventListener('click', () => {
    showEventDialog(null, selected || new Date());
  });

  selected = new Date();
  load();
}
window.initCalendar = initCalendar;
