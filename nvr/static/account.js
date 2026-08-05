/*
 * Account menu and change-password modal.
 *
 * Global chrome, loaded on every authenticated page. Kept dependency-free and
 * defensive: if the markup is not present (an unauthenticated page slipping
 * through), every lookup is guarded so nothing throws.
 */

(function () {
  const account = document.getElementById('account');
  const button = document.getElementById('account-btn');
  const menu = document.getElementById('account-menu');
  if (!account || !button || !menu) return;

  function openMenu() {
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
  }
  function closeMenu() {
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
  }
  const isOpen = () => !menu.hidden;

  button.addEventListener('click', event => {
    event.stopPropagation();
    isOpen() ? closeMenu() : openMenu();
  });

  // Click anywhere else, or Escape, dismisses the menu.
  document.addEventListener('click', event => {
    if (isOpen() && !account.contains(event.target)) closeMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && isOpen()) closeMenu();
  });

  // ---- theme toggle (light / dark) -------------------------------------
  // The saved theme is already applied pre-paint by the inline script in
  // base.html; here we just keep the label in sync and let the user flip it.

  const themeItem = document.getElementById('theme-item');
  if (themeItem) {
    const label = themeItem.querySelector('[data-theme-label]');
    const root = document.documentElement;

    function syncTheme() {
      const dark = (root.dataset.theme || 'dark') !== 'light';
      // The label names the action, not the current state.
      if (label) label.textContent = dark ? 'Light mode' : 'Dark mode';
      themeItem.setAttribute('aria-pressed', String(!dark));
    }

    themeItem.addEventListener('click', () => {
      const next = (root.dataset.theme || 'dark') === 'light' ? 'dark' : 'light';
      root.dataset.theme = next;
      try { localStorage.setItem('sentry-theme', next); } catch (e) {}
      syncTheme();
      closeMenu();
    });

    syncTheme();
  }

  // ---- change-password modal -------------------------------------------

  const modal = document.getElementById('password-modal');
  const openItem = document.getElementById('change-password-item');
  const closeBtn = document.getElementById('pw-close');
  const form = document.getElementById('password-form');
  const alertBox = document.getElementById('pw-alert');
  const submit = document.getElementById('pw-submit');
  if (!modal || !openItem || !form) return;

  const fields = {
    current: document.getElementById('pw-current'),
    next: document.getElementById('pw-new'),
    confirm: document.getElementById('pw-confirm'),
  };

  function showModal() {
    closeMenu();
    alertBox.innerHTML = '';
    form.reset();
    modal.hidden = false;
    fields.current.focus();
  }
  function hideModal() {
    modal.hidden = true;
  }

  openItem.addEventListener('click', showModal);
  closeBtn.addEventListener('click', hideModal);
  // Click on the dim backdrop (but not the dialog itself) closes.
  modal.addEventListener('click', event => {
    if (event.target === modal) hideModal();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !modal.hidden) hideModal();
  });

  function fail(message) {
    alertBox.innerHTML = `<div class="alert alert-error">${message}</div>`;
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    alertBox.innerHTML = '';

    if (fields.next.value !== fields.confirm.value) {
      fail('New passwords do not match.');
      return;
    }
    if (fields.next.value.length < 8) {
      fail('New password must be at least 8 characters.');
      return;
    }

    submit.disabled = true;
    const label = submit.textContent;
    submit.innerHTML = '<span class="spinner"></span> Updating';
    try {
      const response = await fetch('/account/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          current_password: fields.current.value,
          new_password: fields.next.value,
          confirm: fields.confirm.value,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        fail(data.error || 'Could not update the password.');
        return;
      }
      alertBox.innerHTML =
        '<div class="alert alert-info">Password updated. Other sessions were signed out.</div>';
      form.reset();
      setTimeout(hideModal, 1400);
    } catch (error) {
      fail('Network error — please try again.');
    } finally {
      submit.disabled = false;
      submit.textContent = label;
    }
  });
})();
