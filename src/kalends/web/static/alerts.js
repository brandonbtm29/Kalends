/* Kalends: turn on browser notifications (settings panel and shared pages). */
(function () {
  for (const el of document.querySelectorAll('[data-tz]')) {
    try { if (!el.value) el.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { /* ignore */ }
  }
  const box = document.querySelector('[data-push]');
  if (!box) return;
  const button = box.querySelector('[data-push-on]');
  const status = box.querySelector('[data-push-status]');
  const say = (text) => { if (status) status.textContent = text; };
  const ios = /iP(hone|ad|od)/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = window.navigator.standalone === true ||
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
  const store = (key, value) => { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; } return null; };

  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    if (button) button.disabled = true;
    say(ios && !standalone
      ? 'On iPhone or iPad: tap Share, then Add to Home Screen. Open it from your Home Screen and turn notifications on there.'
      : "This browser can't show notifications from websites. Try another way below.");
    return;
  }
  const manageKey = 'kalends-alerts:' + location.pathname.split('/').slice(0, 3).join('/');
  const known = store(manageKey);
  const link = box.querySelector('[data-push-manage]');
  if (known && link) { link.href = known; link.hidden = false; }

  const keyBytes = (b64) => {
    const pad = '='.repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  };

  if (button) button.addEventListener('click', async (e) => {
    e.preventDefault();
    button.disabled = true;
    try {
      say('Asking your browser…');
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        say('Notifications are blocked for this site. Allow them in your browser settings, then try again.');
        button.disabled = false;
        return;
      }
      const reg = await navigator.serviceWorker.register('/sw.js');
      await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (sub && box.dataset.key && sub.options && sub.options.applicationServerKey) {
        const have = new Uint8Array(sub.options.applicationServerKey);
        const want = keyBytes(box.dataset.key);
        if (have.length !== want.length || have.some((b, i) => b !== want[i])) { await sub.unsubscribe(); sub = null; }
      }
      if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(box.dataset.key) });
      const form = button.closest('form');
      const prefs = {};
      if (form) {
        const data = new FormData(form);
        prefs.changes = data.get('changes') === 'on';
        prefs.reminders = data.get('reminders') === 'on';
        prefs.lead = Number(data.get('lead') || 30);
      }
      const headers = { 'Content-Type': 'application/json' };
      if (box.dataset.csrf) headers['X-CSRF'] = box.dataset.csrf;
      const resp = await fetch(box.dataset.post, {
        method: 'POST',
        headers,
        credentials: 'same-origin',
        body: JSON.stringify(Object.assign({
          token: box.dataset.token || '',
          subscription: sub.toJSON(),
          tz: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
        }, prefs)),
      });
      const out = await resp.json().catch(() => ({}));
      if (!resp.ok || !out.ok) throw new Error(out.error || 'Kalends refused it');
      if (out.manage) { store(manageKey, out.manage); location.href = out.manage; } else { location.reload(); }
    } catch (err) {
      say('That didn’t work: ' + (err && err.message ? err.message : err) + '.');
      button.disabled = false;
    }
  });
})();
