// hug-events.js — capture user interactions for the dataset.
//
// Two write paths, always both:
//   1) POST /event to the local backend (the durable dataset on disk)
//   2) localStorage buffer (works on GitHub Pages / static hosts; ~500 records cap)
//
// Console helpers:
//   hugSession()  → returns the persistent anonymous session id
//   hugExport()   → downloads the local buffer as a JSONL file
//   hugClear()    → wipes the local buffer (does not touch the server log)
(() => {
  const SESSION_KEY  = 'hug:session_id';
  const BUFFER_KEY   = 'hug:events';
  const BUFFER_LIMIT = 500;
  const API_BASE = (window.HUG_API_BASE || '').replace(/\/+$/, '');

  function apiUrl(path) {
    return `${API_BASE}${path}`;
  }

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      const v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function getSessionId() {
    let id = null;
    try { id = localStorage.getItem(SESSION_KEY); } catch (e) {}
    if (!id) {
      id = uuid();
      try { localStorage.setItem(SESSION_KEY, id); } catch (e) {}
    }
    return id;
  }

  function pushBuffer(record) {
    try {
      const buf = JSON.parse(localStorage.getItem(BUFFER_KEY) || '[]');
      buf.push(record);
      while (buf.length > BUFFER_LIMIT) buf.shift();
      localStorage.setItem(BUFFER_KEY, JSON.stringify(buf));
    } catch (e) {
      // localStorage quota / parse failure — silently drop; server log still has it
    }
  }

  function pageName() {
    return (location.pathname.split('/').pop() || 'index.html').toLowerCase();
  }

  async function hugEvent(event_type, payload) {
    payload = payload || {};
    const record = {
      session_id: getSessionId(),
      page:       pageName(),
      event_type: event_type,
      payload:    payload,
      timestamp:  new Date().toISOString(),
    };
    pushBuffer(record);
    refreshCounts();
    try {
      // best-effort POST — silently fails on static hosts (GitHub Pages, file://)
      await fetch(apiUrl('/event'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(record),
        keepalive: true,  // survive page unload (claim_submitted before navigation)
      });
    } catch (e) {
      // server unreachable — the buffer holds the record for export
    }
  }

  function bufferLength() {
    try { return JSON.parse(localStorage.getItem(BUFFER_KEY) || '[]').length; }
    catch (e) { return 0; }
  }

  // Where tester exports get shipped. Pre-fills the user's email composer.
  const EXPORT_EMAIL = 'yuexing@mit.edu';

  function hugExport() {
    const buf = JSON.parse(localStorage.getItem(BUFFER_KEY) || '[]');
    if (buf.length === 0) {
      console.log('[hug] no events in local buffer');
      // visual nudge for the footer link
      document.querySelectorAll('[data-hug-export]').forEach((el) => {
        const orig = el.dataset.origText || el.textContent;
        el.dataset.origText = orig;
        el.textContent = 'no events yet';
        setTimeout(() => { wireExportLinks(true); }, 1400);
      });
      return;
    }

    // 1. Download the JSONL file.
    const sid      = getSessionId();
    const filename = `hug-events-${sid.slice(0, 8)}-${Date.now()}.jsonl`;
    const text     = buf.map((r) => JSON.stringify(r)).join('\n') + '\n';
    const blob     = new Blob([text], { type: 'application/x-ndjson' });
    const url      = URL.createObjectURL(blob);
    const a        = document.createElement('a');
    a.href     = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

    // 2. Build a summary (event counts only — no raw user text in the body,
    //    since mailto bodies leak through email logs and browser history).
    const types = {};
    buf.forEach(r => { types[r.event_type] = (types[r.event_type] || 0) + 1; });
    const typeLines = Object.entries(types)
      .sort((a, b) => b[1] - a[1])
      .map(([t, n]) => `  ${t}: ${n}`)
      .join('\n');
    const firstTs = buf[0] && buf[0].timestamp ? buf[0].timestamp : '';
    const lastTs  = buf[buf.length - 1] && buf[buf.length - 1].timestamp
      ? buf[buf.length - 1].timestamp : '';

    const subject = `Hug.Claims session export — ${sid.slice(0, 8)}`;
    const body = [
      'Hi Yuexing,',
      '',
      `I just exported my Hug.Claims session.`,
      '',
      `Session ID: ${sid}`,
      `Events captured: ${buf.length}`,
      `Time range: ${firstTs}  →  ${lastTs}`,
      `Filename: ${filename}`,
      '',
      'Event breakdown:',
      typeLines,
      '',
      `Please attach ${filename} (it just downloaded) before sending.`,
      '',
      '— sent from hug.claims tester export',
    ].join('\n');

    // 3. Open the email composer (mailto: opens externally, no page navigation).
    const mailto = `mailto:${EXPORT_EMAIL}`
      + `?subject=${encodeURIComponent(subject)}`
      + `&body=${encodeURIComponent(body)}`;
    // Slight delay so the file download starts before the composer steals focus.
    setTimeout(() => {
      const m = document.createElement('a');
      m.href = mailto;
      m.rel  = 'noopener';
      document.body.appendChild(m);
      m.click();
      m.remove();
    }, 250);

    console.log(`[hug] exported ${buf.length} events → ${filename}; opening mail composer to ${EXPORT_EMAIL}`);
  }

  function hugClear() {
    try { localStorage.removeItem(BUFFER_KEY); } catch (e) {}
    refreshCounts();
    console.log('[hug] local buffer cleared (server log untouched)');
  }

  // --- Auto-wired footer link: any element with [data-hug-export] becomes
  // a clickable "Export my session" link with a live event count. ---
  function refreshCounts() {
    const n = bufferLength();
    document.querySelectorAll('[data-hug-export]').forEach((el) => {
      const span = el.querySelector('[data-hug-count]');
      if (!span) return;
      span.textContent = n === 0 ? '(empty)' : `(${n} event${n === 1 ? '' : 's'})`;
    });
  }
  function wireExportLinks(forceReset) {
    document.querySelectorAll('[data-hug-export]').forEach((el) => {
      if (forceReset && el.dataset.origText) {
        el.textContent = el.dataset.origText;
      }
      if (!el.querySelector('[data-hug-count]')) {
        // Restore label + append a count span. Idempotent: skip if already wired.
        if (!el.dataset.hugWired) {
          // remember the user-authored label so we can rebuild after the "no events yet" flash
          el.dataset.origLabel = el.textContent.trim() || 'Export my session';
          el.textContent = el.dataset.origLabel + ' ';
          const span = document.createElement('span');
          span.dataset.hugCount = 'true';
          span.style.opacity = '0.7';
          span.style.marginLeft = '2px';
          el.appendChild(span);
        }
      }
      if (!el.dataset.hugWired) {
        el.dataset.hugWired = 'true';
        el.style.cursor = 'pointer';
        el.addEventListener('click', (e) => { e.preventDefault(); hugExport(); });
      }
    });
    refreshCounts();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => wireExportLinks(false));
  } else {
    wireExportLinks(false);
  }

  // Expose globals so other inline scripts and the console can call them
  window.hugEvent   = hugEvent;
  window.hugExport  = hugExport;
  window.hugClear   = hugClear;
  window.hugSession = getSessionId;

  // Auto page_view on load
  function firePageView() {
    hugEvent('page_view', {
      referrer: document.referrer || null,
      title:    document.title,
      url:      location.pathname + location.search,
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', firePageView);
  } else {
    firePageView();
  }

  // Convenience: include the session_id alongside any /chat or /verify_claim request
  // by grabbing window.HUG_SESSION_ID from inline scripts.
  window.HUG_SESSION_ID = getSessionId();
})();
