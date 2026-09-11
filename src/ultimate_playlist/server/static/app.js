/* Ultimate Playlist — web UI.
 *
 * Plain JavaScript, no build step, no external resources.
 *
 * SECURITY: every string that comes back from the API (titles, artists, URLs, messages, paths)
 * is untrusted — a YouTube title can contain HTML. The DOM is built exclusively with
 * document.createElement / textContent through el(); innerHTML is never used anywhere.
 * URLs embedded in src/href are only ever built from ids via encodeURIComponent.
 */
'use strict';

(() => {
  const TERMINAL = new Set(['done', 'skipped', 'error', 'cancelled']);
  const STATUS_LABEL = {
    queued: 'Queued',
    resolving: 'Resolving',
    downloading: 'Downloading',
    converting: 'Converting',
    done: 'Done',
    skipped: 'Skipped',
    error: 'Error',
    cancelled: 'Cancelled',
  };
  const ACTIVE_POLL_MS = 1000;
  const IDLE_POLL_MS = 5000;
  const STATUS_POLL_MS = 30000;
  const SEARCH_DEBOUNCE_MS = 200;
  const PREFS_KEY = 'ultimate-playlist.prefs';
  const SVG_NS = 'http://www.w3.org/2000/svg';

  // ================================================================ DOM helpers

  const $ = (selector, root = document) => root.querySelector(selector);

  /** Build an element. Text always goes through textContent; there is no HTML path on purpose. */
  function el(tag, props = null, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = String(value);
        else if (key === 'style') node.style.cssText = value;
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
        else if (key.startsWith('aria-') || key.startsWith('data-')) node.setAttribute(key, value);
        else if (key === 'for') node.htmlFor = value;
        else if (key in node) node[key] = value;
        else node.setAttribute(key, value);
      }
    }
    appendChildren(node, children);
    return node;
  }

  function appendChildren(node, children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      if (Array.isArray(child)) appendChildren(node, child);
      else node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  /** An <svg><use> reference into the inline sprite in index.html. */
  function icon(name, extraClass) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', extraClass ? `icon ${extraClass}` : 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function setIcon(button, name) {
    const use = button.querySelector('use');
    if (use) use.setAttribute('href', `#i-${name}`);
  }

  function iconButton(name, title, onClick, { disabled = false, danger = false } = {}) {
    return el(
      'button',
      {
        type: 'button',
        class: danger ? 'icon-btn danger' : 'icon-btn',
        title,
        'aria-label': title,
        disabled,
        onclick: (event) => onClick(event),
      },
      icon(name),
    );
  }

  function emptyState(text, iconName) {
    return el('div', { class: 'empty' }, icon(iconName || 'music'), el('div', { class: 'empty-text', text }));
  }

  // ================================================================ Formatting

  const pad2 = (n) => String(n).padStart(2, '0');

  function fmtDuration(seconds) {
    const value = Number(seconds);
    if (seconds === null || seconds === undefined || !Number.isFinite(value) || value < 0) return '–:––';
    const total = Math.round(value);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
  }

  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  }

  function fmtSpeed(bytesPerSecond) {
    const v = Number(bytesPerSecond);
    if (!Number.isFinite(v) || v <= 0) return '';
    const mb = v / 1048576;
    if (mb >= 1) return `${mb.toFixed(1)} MB/s`;
    return `${Math.max(1, Math.round(v / 1024))} KB/s`;
  }

  function fmtEta(seconds) {
    const v = Number(seconds);
    if (seconds === null || seconds === undefined || !Number.isFinite(v) || v < 0) return '';
    return fmtDuration(v);
  }

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const coverUrl = (id) => `/api/library/${encodeURIComponent(id)}/cover`;
  const mediaUrl = (id) => `/media/${encodeURIComponent(id)}`;
  const trackLabel = (t) => `${(t && t.artist) || 'Unknown artist'} - ${(t && t.title) || 'Untitled'}`;

  // ================================================================ API

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
    }
  }

  /** The one fetch wrapper. Resolves with parsed JSON; throws ApiError(detail) on non-2xx. */
  async function api(path, { method = 'GET', body } = {}) {
    const init = { method, headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (_err) {
      throw new ApiError('Cannot reach the app. Is the server still running?', 0);
    }
    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_err) {
        data = text;
      }
    }
    if (!response.ok) throw new ApiError(errorMessage(data, response), response.status);
    return data;
  }

  function errorMessage(data, response) {
    let detail = data && typeof data === 'object' ? data.detail : data;
    if (Array.isArray(detail)) {
      // FastAPI validation errors: [{loc, msg, type}, ...]
      detail = detail.map((d) => (d && typeof d.msg === 'string' ? d.msg : JSON.stringify(d))).join('; ');
    }
    if (typeof detail === 'string' && detail.trim()) return detail;
    const status = response.statusText ? `${response.status} ${response.statusText}` : String(response.status);
    return `Request failed (${status})`;
  }

  // ================================================================ Toasts

  const toastsEl = $('#toasts');

  function toast(message, kind = 'error', ms) {
    const iconName = kind === 'error' ? 'alert' : kind === 'success' ? 'check' : 'info';
    let timer = null;
    const remove = () => {
      clearTimeout(timer);
      node.classList.add('leaving');
      setTimeout(() => node.remove(), 200);
    };
    const node = el(
      'div',
      { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
      icon(iconName),
      el('span', { class: 'toast-text', text: message }),
      el('button', { type: 'button', class: 'toast-close', title: 'Dismiss', onclick: remove }, icon('x')),
    );
    toastsEl.append(node);
    while (toastsEl.childElementCount > 5) toastsEl.firstElementChild.remove();
    timer = setTimeout(remove, ms || (kind === 'error' ? 7000 : 3500));
  }

  function showError(err) {
    toast(err && err.message ? err.message : String(err), 'error');
  }

  // ================================================================ Dialogs

  const dialogEl = $('#dialog');

  function openDialog({ title, body, confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false, focus = null }) {
    return new Promise((resolve) => {
      clear(dialogEl);
      let settled = false;
      const done = (value) => {
        if (settled) return;
        settled = true;
        if (dialogEl.open) dialogEl.close();
        resolve(value);
      };
      const submit = el('button', { type: 'submit', class: danger ? 'btn btn-danger' : 'btn btn-primary', text: confirmLabel });
      const form = el(
        'form',
        {
          method: 'dialog',
          class: 'dialog-form',
          onsubmit: (event) => {
            event.preventDefault();
            done(true);
          },
        },
        el('h3', { class: 'dialog-title', text: title }),
        el('div', { class: 'dialog-body' }, body),
        el(
          'div',
          { class: 'dialog-actions' },
          el('button', { type: 'button', class: 'btn btn-ghost', text: cancelLabel, onclick: () => done(false) }),
          submit,
        ),
      );
      dialogEl.append(form);
      dialogEl.onclose = () => done(false);
      dialogEl.onclick = (event) => {
        if (event.target === dialogEl) done(false);
      };
      dialogEl.showModal();
      (focus || submit).focus();
    });
  }

  async function confirmDialog({ title, message, confirmLabel = 'OK', checkbox = null, danger = false }) {
    const check = checkbox ? el('input', { type: 'checkbox' }) : null;
    const body = [el('p', { text: message })];
    if (check) body.push(el('label', { class: 'check' }, check, el('span', { text: checkbox })));
    const ok = await openDialog({ title, body, confirmLabel, danger });
    return { ok, checked: Boolean(check && check.checked) };
  }

  async function promptDialog({ title, label, value = '', placeholder = '', confirmLabel = 'OK' }) {
    const input = el('input', {
      type: 'text',
      class: 'input',
      value,
      placeholder,
      required: true,
      maxLength: 200,
      // Explicit Enter -> submit so the dialog never depends on the browser's implicit submission.
      onkeydown: (event) => {
        if (event.key !== 'Enter' || event.isComposing) return;
        event.preventDefault();
        const form = input.closest('form');
        if (form) form.requestSubmit();
      },
    });
    const body = [label ? el('label', { class: 'field-label', text: label }) : null, input];
    const ok = await openDialog({ title, body, confirmLabel, focus: input });
    return ok ? input.value.trim() : null;
  }

  // ================================================================ Popover menu

  const menuEl = $('#menu');

  function openMenu(anchor, items) {
    closeMenu();
    for (const item of items) {
      if (item === 'sep') {
        menuEl.append(el('div', { class: 'menu-sep' }));
        continue;
      }
      menuEl.append(
        el(
          'button',
          {
            type: 'button',
            class: 'menu-item',
            role: 'menuitem',
            disabled: Boolean(item.disabled),
            onclick: () => {
              closeMenu();
              item.onClick();
            },
          },
          item.icon ? icon(item.icon) : null,
          el('span', { text: item.label }),
        ),
      );
    }
    menuEl.hidden = false;
    const rect = anchor.getBoundingClientRect();
    const width = menuEl.offsetWidth;
    const height = menuEl.offsetHeight;
    let x = rect.left;
    let y = rect.bottom + 4;
    if (x + width > window.innerWidth - 8) x = Math.max(8, window.innerWidth - width - 8);
    if (y + height > window.innerHeight - 8) y = Math.max(8, rect.top - height - 4);
    menuEl.style.left = `${Math.round(x)}px`;
    menuEl.style.top = `${Math.round(y)}px`;
    // Register the outside-click handler after the click that opened the menu has finished bubbling.
    setTimeout(() => {
      if (menuEl.hidden) return;
      document.addEventListener('click', onDocumentClick);
      document.addEventListener('keydown', onMenuKey);
      window.addEventListener('resize', closeMenu);
    }, 0);
    const first = menuEl.querySelector('button:not([disabled])');
    if (first) first.focus();
  }

  function onDocumentClick(event) {
    if (!menuEl.contains(event.target)) closeMenu();
  }

  function onMenuKey(event) {
    if (event.key === 'Escape') closeMenu();
  }

  function closeMenu() {
    menuEl.hidden = true;
    clear(menuEl);
    document.removeEventListener('click', onDocumentClick);
    document.removeEventListener('keydown', onMenuKey);
    window.removeEventListener('resize', closeMenu);
  }

  // ================================================================ State

  const state = {
    status: null,
    offline: false,
    jobs: [],
    jobStatus: new Map(), // job id -> last seen status (to detect transitions)
    firstJobsPoll: true,
    tracks: [], // library rows currently shown (may be filtered)
    trackIndex: new Map(), // every track by id (unfiltered), used by playlists
    query: '',
    playlists: [],
    selectedPlaylistId: null,
    exportPaths: new Map(), // playlist id -> last exported path
    selection: new Set(),
    tab: 'library',
  };

  // ================================================================ Status chips

  async function loadStatus() {
    try {
      const status = await api('/api/status');
      state.status = status;
      renderStatus(status);
      setOffline(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 0) setOffline(true);
      else showError(err);
    }
  }

  function setOffline(offline) {
    if (state.offline === offline) return;
    state.offline = offline;
    const chips = $('#status-chips');
    const existing = $('#offline-chip', chips);
    if (offline && !existing) {
      chips.prepend(
        chip({
          id: 'offline-chip',
          kind: 'warn',
          mark: '…',
          label: 'Reconnecting',
          tip: 'The app server is not answering. Retrying automatically.',
        }),
      );
    } else if (!offline && existing) {
      existing.remove();
    }
  }

  function renderStatus(status) {
    const chips = $('#status-chips');
    const offline = $('#offline-chip', chips);
    clear(chips);
    if (offline) chips.append(offline);

    const checks = [];
    for (const provider of status.providers || []) {
      for (const check of provider.checks || []) checks.push({ ...check, provider: provider.display_name || provider.name });
    }
    const ffmpeg = status.ffmpeg || {};
    const ffmpegOk = Boolean(ffmpeg.path);
    const ffmpegCheck = checks.find((c) => /ffmpeg/i.test(c.label || ''));
    chips.append(
      chip({
        kind: ffmpegOk ? 'ok' : 'bad',
        mark: ffmpegOk ? '✓' : '✗',
        label: 'ffmpeg',
        short: 'ff',
        tip: ffmpegOk
          ? `ffmpeg ${ffmpeg.version || ''}${ffmpeg.bundled ? ' (bundled)' : ''}\n${ffmpeg.path}`
          : (ffmpegCheck && ffmpegCheck.detail) || 'ffmpeg was not found. Run "doctor" to see what to do, then restart the app.',
      }),
    );

    const js = checks.find((c) => /javascript|js runtime/i.test(c.label || ''));
    chips.append(
      chip({
        kind: js && js.ok ? 'ok' : 'bad',
        mark: js && js.ok ? '✓' : '✗',
        label: 'JS runtime',
        short: 'JS',
        tip: js ? js.detail || js.label : 'No JavaScript runtime check reported. YouTube downloads need Node.js or Deno.',
      }),
    );

    chips.append(
      chip({
        id: 'library-chip',
        kind: 'neutral',
        iconName: 'folder',
        label: 'Library',
        short: 'Lib',
        tip: libraryChipTip(status),
        onclick: openLibraryFolder,
      }),
    );

    const brand = $('#brand');
    if (status.version) brand.title = `Ultimate Playlist v${status.version}`;
  }

  function libraryChipTip(status) {
    return `${status.library_dir || ''}\n${plural(status.tracks || 0, 'track')} · click to open the folder`;
  }

  /** The library grew or shrank locally: refresh the chip's count without re-running the doctor checks. */
  function refreshTrackCount() {
    if (!state.status) return;
    const count = state.trackIndex.size;
    if (state.status.tracks === count) return;
    state.status.tracks = count;
    const node = $('#library-chip');
    if (!node) return;
    const tip = libraryChipTip(state.status);
    node.setAttribute('data-tip', tip);
    node.setAttribute('aria-label', `Library: ${tip}`);
  }

  /** Status chip. `short` is the label shown in narrow windows so two ✓ chips stay distinguishable.
   *  The tooltip is the CSS `data-tip` bubble; `title` is only set when there is no tip, so the
   *  browser's native tooltip never stacks a second copy on top of it. */
  function chip({ id, kind, mark, iconName, label, short, tip, onclick }) {
    const node = el(onclick ? 'button' : 'span', {
      id,
      type: onclick ? 'button' : null,
      class: `chip chip-${kind}`,
      'data-tip': tip || null,
      title: tip ? null : label,
      'aria-label': tip ? `${label}: ${tip}` : label,
      onclick,
    });
    node.append(
      iconName ? icon(iconName) : el('span', { class: 'chip-mark', text: mark }),
      el('span', { class: 'chip-label', text: label }),
      el('span', { class: 'chip-short', text: short || label }),
    );
    return node;
  }

  async function openLibraryFolder() {
    try {
      await api('/api/library/open', { method: 'POST' });
    } catch (err) {
      showError(err);
    }
  }

  // ================================================================ Queue

  let jobsTimer = null;
  let jobsInFlight = false;
  let pollAgain = false; // a refresh was requested while a poll was already in flight

  async function pollJobs() {
    clearTimeout(jobsTimer);
    if (jobsInFlight) {
      pollAgain = true; // do not drop an explicit refresh (Download / Cancel / Retry clicked)
      return;
    }
    jobsInFlight = true;
    let active = false;
    try {
      const data = await api('/api/jobs');
      const jobs = Array.isArray(data.jobs) ? data.jobs : [];
      noteJobTransitions(jobs);
      state.jobs = jobs;
      renderQueue();
      active = jobs.some((j) => !TERMINAL.has(j.status));
      setOffline(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 0) setOffline(true);
      else showError(err);
    } finally {
      jobsInFlight = false;
      const delay = pollAgain ? 0 : active ? ACTIVE_POLL_MS : IDLE_POLL_MS;
      pollAgain = false;
      jobsTimer = setTimeout(pollJobs, delay);
    }
  }

  /** Detect status changes between polls: refresh the library when a download lands, toast failures. */
  function noteJobTransitions(jobs) {
    let libraryChanged = false;
    const failures = [];
    for (const job of jobs) {
      const previous = state.jobStatus.get(job.id);
      if (previous === job.status) continue;
      state.jobStatus.set(job.id, job.status);
      if (state.firstJobsPoll) continue;
      if (job.status === 'done' && job.track) libraryChanged = true;
      if (job.status === 'error' && previous !== undefined) failures.push(job);
    }
    const ids = new Set(jobs.map((j) => j.id));
    for (const id of [...state.jobStatus.keys()]) if (!ids.has(id)) state.jobStatus.delete(id);
    state.firstJobsPoll = false;
    // reloadTracks() refreshes the library chip's count itself; the full status poll (doctor
    // checks, ffmpeg probe) stays on its 30 s interval instead of running per finished track.
    if (libraryChanged) reloadTracks();
    if (failures.length > 3) toast(`${failures.length} downloads failed — see the queue for details`);
    else for (const job of failures) toast(`${job.title || job.url}: ${job.error || 'download failed'}`);
  }

  const jobNodes = new Map(); // job id -> { sig, node } so unchanged rows keep their DOM identity
  const byCreated = (a, b) => String(a.created_at || '').localeCompare(String(b.created_at || ''));
  const PROGRESS_STATUSES = new Set(['downloading', 'converting', 'resolving']);

  /** What a row's structure depends on. Progress / speed / ETA / the live message are patched in
   *  place (see updateJobRow) so the row and its Cancel button keep their DOM identity across polls. */
  function jobSignature(job, kids, isChild) {
    const status = String(job.status || 'queued');
    const trackId = job.track && job.track.id ? job.track.id : null;
    return JSON.stringify([
      job.id,
      status,
      job.title,
      job.url,
      job.error,
      job.parent_id,
      job.child_count,
      PROGRESS_STATUSES.has(status) ? null : job.message,
      trackId,
      trackId ? state.trackIndex.has(trackId) : null,
      isChild,
      kids ? kids.map((k) => k.status) : null,
      playingId(),
      audio.paused,
    ]);
  }

  /** The progress line of a live row: percentage plus speed / ETA / the provider's message. */
  function jobStats(job, status) {
    const pct = Math.max(0, Math.min(100, Math.round(Number(job.progress || 0) * 100)));
    const stats = [];
    if (status === 'downloading') {
      stats.push(`${pct}%`);
      const speed = fmtSpeed(job.speed);
      if (speed) stats.push(speed);
      const eta = fmtEta(job.eta);
      if (eta) stats.push(`ETA ${eta}`);
      if (job.message) stats.push(job.message);
    } else if (status === 'converting') {
      stats.push(job.message || 'Converting…');
    } else {
      stats.push(job.message || 'Looking up the link…');
    }
    return { pct, text: stats.join(' · ') };
  }

  function updateJobRow(row, job) {
    const status = String(job.status || 'queued');
    if (!PROGRESS_STATUSES.has(status)) return;
    const { pct, text } = jobStats(job, status);
    const fill = row.querySelector('.bar-fill');
    if (fill && status === 'downloading') fill.style.width = `${pct}%`;
    const stats = row.querySelector('.job-stats');
    if (stats && stats.textContent !== text) stats.textContent = text;
  }

  function renderQueue() {
    const list = $('#queue-list');
    const jobs = state.jobs;
    $('#clear-finished').disabled = !jobs.some((j) => TERMINAL.has(j.status));
    if (!jobs.length) {
      jobNodes.clear();
      clear(list);
      list.append(emptyState('Paste a link above to get started', 'download'));
      return;
    }
    const empty = list.querySelector('.empty');
    if (empty) empty.remove();

    const ids = new Set(jobs.map((j) => j.id));
    const children = new Map();
    for (const job of jobs) {
      if (!job.parent_id || !ids.has(job.parent_id)) continue;
      if (!children.has(job.parent_id)) children.set(job.parent_id, []);
      children.get(job.parent_id).push(job);
    }
    const ordered = [];
    for (const job of jobs) {
      if (job.parent_id && ids.has(job.parent_id)) continue; // rendered under its parent
      const kids = children.get(job.id) || null;
      ordered.push([job, kids, false]);
      if (kids) for (const kid of kids.slice().sort(byCreated)) ordered.push([kid, null, true]);
    }
    for (const [job, kids, isChild] of ordered) {
      const sig = jobSignature(job, kids, isChild);
      let entry = jobNodes.get(job.id);
      if (!entry || entry.sig !== sig) {
        if (entry) entry.node.remove();
        entry = { sig, node: jobRow(job, kids, isChild) };
        jobNodes.set(job.id, entry);
      } else {
        updateJobRow(entry.node, job); // only the numbers moved: patch, do not rebuild
      }
      list.append(entry.node); // append() moves an existing node, which keeps the order in sync
    }
    for (const [id, entry] of jobNodes) {
      if (!ids.has(id)) {
        entry.node.remove();
        jobNodes.delete(id);
      }
    }
  }

  function jobRow(job, kids, isChild) {
    const status = String(job.status || 'queued');
    const live = !TERMINAL.has(status);
    const row = el('div', {
      class: `job job-${status}${isChild ? ' job-child' : ''}${status === 'error' ? ' job-error-state' : ''}`,
    });
    row.append(
      el(
        'div',
        { class: 'job-head' },
        el('div', { class: 'job-title', title: job.url || '', text: job.title || job.url || '(untitled)' }),
        el('span', { class: `pill pill-${status}${live ? ' pill-live' : ''}`, text: STATUS_LABEL[status] || status }),
      ),
    );

    const isParent = Number(job.child_count) > 0;
    if (isParent) {
      let summary = job.message || `Playlist: ${plural(job.child_count, 'track')}`;
      if (kids && kids.length) {
        const finished = kids.filter((k) => k.status === 'done' || k.status === 'skipped').length;
        const failed = kids.filter((k) => k.status === 'error' || k.status === 'cancelled').length;
        summary += ` · ${finished}/${kids.length} finished`;
        if (failed) summary += ` · ${failed} failed`;
      }
      row.append(el('div', { class: 'job-meta', text: summary }));
    } else if (PROGRESS_STATUSES.has(status)) {
      const { pct, text } = jobStats(job, status);
      const barClass = status === 'resolving' ? 'bar bar-indeterminate' : status === 'converting' ? 'bar bar-busy' : 'bar';
      row.append(el('div', { class: barClass }, el('div', { class: 'bar-fill', style: status === 'downloading' ? `width:${pct}%` : null })));
      row.append(el('div', { class: 'job-stats', text }));
    } else if (job.message) {
      row.append(el('div', { class: 'job-meta', text: job.message }));
    }
    if (job.error) row.append(el('div', { class: 'job-error', text: job.error }));

    const actions = el('div', { class: 'job-actions' });
    if (live) {
      actions.append(el('button', { type: 'button', class: 'btn btn-xs btn-ghost', onclick: () => cancelJob(job) }, icon('x'), 'Cancel'));
    } else if (status === 'error' || status === 'cancelled') {
      actions.append(el('button', { type: 'button', class: 'btn btn-xs btn-ghost', onclick: () => retryJob(job) }, icon('retry'), 'Retry'));
    }
    // Play only while the track is still in the library: a Done row whose track was deleted
    // (or vanished on rescan) would otherwise offer a button that ends in a 404 toast.
    if (job.track && job.track.id && state.trackIndex.has(job.track.id)) {
      const isPlaying = playingId() === job.track.id && !audio.paused;
      actions.append(
        el(
          'button',
          { type: 'button', class: 'btn btn-xs btn-ghost', onclick: () => playTracks([job.track], 0, 'Queue') },
          icon(isPlaying ? 'pause' : 'play'),
          isPlaying ? 'Playing' : 'Play',
        ),
      );
    }
    if (actions.childElementCount) row.append(actions);
    return row;
  }

  async function submitUrls() {
    const input = $('#url-input');
    const button = $('#download-btn');
    const text = input.value.trim();
    if (!text) {
      input.focus();
      return;
    }
    button.disabled = true;
    try {
      const data = await api('/api/jobs', { method: 'POST', body: { url: text } });
      const count = Array.isArray(data && data.jobs) ? data.jobs.length : 1;
      input.value = '';
      autosize(input);
      toast(count === 1 ? 'Added to the queue' : `Added ${count} links to the queue`, 'success');
      pollJobs();
    } catch (err) {
      showError(err);
    } finally {
      button.disabled = false;
      input.focus();
    }
  }

  async function cancelJob(job) {
    try {
      await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' });
      pollJobs();
    } catch (err) {
      showError(err);
    }
  }

  async function retryJob(job) {
    try {
      await api(`/api/jobs/${encodeURIComponent(job.id)}/retry`, { method: 'POST' });
      pollJobs();
    } catch (err) {
      showError(err);
    }
  }

  async function clearFinished() {
    try {
      const data = await api('/api/jobs/finished', { method: 'DELETE' });
      const removed = Number(data && data.removed) || 0;
      toast(removed ? `Cleared ${plural(removed, 'finished job')}` : 'Nothing to clear', 'success');
      pollJobs();
    } catch (err) {
      showError(err);
    }
  }

  function autosize(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 120)}px`;
  }

  // ================================================================ Library

  let searchTimer = null;

  function setIndex(tracks) {
    state.trackIndex = new Map(tracks.map((t) => [t.id, t]));
    for (const id of [...state.selection]) if (!state.trackIndex.has(id)) state.selection.delete(id);
    refreshTrackCount();
    renderQueue(); // Play buttons in the queue follow what is actually in the library
  }

  async function loadLibrary() {
    const query = state.query;
    try {
      const data = await api(`/api/library${query ? `?q=${encodeURIComponent(query)}` : ''}`);
      if (query !== state.query) return; // a newer search superseded this one
      const tracks = Array.isArray(data.tracks) ? data.tracks : [];
      state.tracks = tracks;
      if (!query) setIndex(tracks);
      else for (const t of tracks) state.trackIndex.set(t.id, t);
      renderLibrary();
      renderPlaylistDetail();
    } catch (err) {
      showError(err);
    }
  }

  /** Refresh the visible rows and, when a search filter is active, the unfiltered index too. */
  async function reloadTracks() {
    await loadLibrary();
    if (!state.query) return;
    try {
      const data = await api('/api/library');
      setIndex(Array.isArray(data.tracks) ? data.tracks : []);
      renderPlaylistDetail();
      updateSelectionBar();
    } catch (err) {
      showError(err);
    }
  }

  const coverObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        if (img.dataset.src) {
          img.src = img.dataset.src;
          delete img.dataset.src;
        }
        coverObserver.unobserve(img);
      }
    },
    { rootMargin: '200px' },
  );

  /** Cover thumbnail with a neutral fallback; the image is only fetched once it scrolls into view. */
  function coverThumb(track, { size = null, eager = false } = {}) {
    const wrap = el('div', { class: size ? `cover cover-${size}` : 'cover' }, el('span', { class: 'cover-fallback' }, icon('music')));
    if (!track || track.has_cover === false) return wrap;
    const img = el('img', {
      alt: '',
      decoding: 'async',
      onload: () => wrap.classList.add('has-img'),
      onerror: () => img.remove(),
    });
    wrap.append(img);
    if (eager) img.src = coverUrl(track.id);
    else {
      img.dataset.src = coverUrl(track.id);
      coverObserver.observe(img);
    }
    return wrap;
  }

  /** Empty a container, forgetting any lazy cover images inside it. */
  function clearCovers(host) {
    for (const img of host.querySelectorAll('img[data-src]')) coverObserver.unobserve(img);
    clear(host);
  }

  function renderLibrary() {
    const host = $('#library-list');
    const top = host.scrollTop; // a refresh (e.g. a download landing) must not jump to the top
    clearCovers(host);
    const tracks = state.tracks;
    $('#library-count').textContent = tracks.length ? plural(tracks.length, 'track') : '';
    updateSelectionBar();
    updatePlayButton(); // the big Play button starts the library once there is something to play
    if (!tracks.length) {
      host.append(emptyState(state.query ? 'No tracks match your search' : 'Your library is empty. Downloaded tracks will show up here.', 'music'));
      return;
    }
    const selectAll = el('input', {
      type: 'checkbox',
      title: 'Select all',
      'aria-label': 'Select all',
      onchange: () => {
        if (selectAll.checked) for (const t of tracks) state.selection.add(t.id);
        else for (const t of tracks) state.selection.delete(t.id);
        renderLibrary();
      },
    });
    syncSelectAll(selectAll);
    const thead = el(
      'thead',
      null,
      el(
        'tr',
        null,
        el('th', { class: 'col-check' }, selectAll),
        el('th', { class: 'col-cover' }),
        el('th', { text: 'Title' }),
        el('th', { class: 'col-artist', text: 'Artist' }),
        el('th', { class: 'col-num', text: 'Length' }),
        el('th', { class: 'col-date', text: 'Added' }),
        el('th', { class: 'col-actions' }),
      ),
    );
    const tbody = el('tbody');
    tracks.forEach((track, index) => tbody.append(trackRow(track, index, selectAll)));
    host.append(el('table', { class: 'tracks' }, thead, tbody));
    if (top) host.scrollTop = top;
  }

  function syncSelectAll(selectAll) {
    const tracks = state.tracks;
    const selected = tracks.filter((t) => state.selection.has(t.id)).length;
    selectAll.checked = tracks.length > 0 && selected === tracks.length;
    selectAll.indeterminate = selected > 0 && selected < tracks.length;
  }

  function trackRow(track, index, selectAll) {
    const selected = state.selection.has(track.id);
    const check = el('input', {
      type: 'checkbox',
      checked: selected,
      'aria-label': 'Select track',
      onchange: () => {
        if (check.checked) state.selection.add(track.id);
        else state.selection.delete(track.id);
        row.classList.toggle('selected', check.checked);
        updateSelectionBar();
        syncSelectAll(selectAll);
      },
    });
    const row = el(
      'tr',
      {
        class: `track-row${selected ? ' selected' : ''}${playingId() === track.id ? ' playing' : ''}`,
        dataset: { id: track.id },
        onclick: (event) => {
          if (event.target.closest('button, input, a, label')) return;
          playTracks(state.tracks, index, state.query ? 'Search results' : 'Library');
        },
      },
      el('td', { class: 'col-check' }, check),
      el('td', { class: 'col-cover' }, coverThumb(track)),
      el(
        'td',
        { class: 'col-title' },
        el('div', { class: 'cell-title', text: track.title || 'Untitled' }),
        track.album ? el('div', { class: 'cell-sub muted', text: track.album }) : null,
      ),
      el('td', { class: 'col-artist', text: track.artist || 'Unknown artist' }),
      el('td', { class: 'col-num', text: fmtDuration(track.duration) }),
      el('td', { class: 'col-date', text: fmtDate(track.added_at), title: track.added_at || '' }),
      el(
        'td',
        { class: 'col-actions' },
        el(
          'div',
          { class: 'row-actions' },
          iconButton('play', 'Play', () => playTracks(state.tracks, index, state.query ? 'Search results' : 'Library')),
          iconButton('plus', 'Add to playlist', (event) => openAddToPlaylistMenu(event.currentTarget, [track.id])),
          iconButton('trash', 'Delete', () => deleteTrack(track), { danger: true }),
        ),
      ),
    );
    return row;
  }

  function updateSelectionBar() {
    const bar = $('#selection-bar');
    const count = state.selection.size;
    bar.hidden = count === 0;
    $('#selection-count').textContent = count ? `${count} selected` : '';
  }

  function clearSelection() {
    if (!state.selection.size) return;
    state.selection.clear();
    renderLibrary();
  }

  async function deleteTrack(track) {
    const { ok, checked } = await confirmDialog({
      title: 'Remove from library',
      message: `Remove “${trackLabel(track)}” from the library?`,
      confirmLabel: 'Remove',
      checkbox: 'Also delete the audio file from disk',
      danger: true,
    });
    if (!ok) return;
    // Release the file before asking the server to delete it: on Windows a file the player is
    // still streaming cannot be removed (the server would answer 409 "still in use").
    if (checked && playingId() === track.id) forgetInPlayer(track.id);
    try {
      await api(`/api/library/${encodeURIComponent(track.id)}?delete_file=${checked ? 'true' : 'false'}`, { method: 'DELETE' });
      state.selection.delete(track.id);
      forgetInPlayer(track.id);
      toast(checked ? 'Track removed and file deleted' : 'Track removed from the library', 'success');
      await reloadTracks();
      await loadPlaylists();
      loadStatus();
    } catch (err) {
      showError(err);
    }
  }

  async function rescanLibrary() {
    const button = $('#rescan');
    button.disabled = true;
    try {
      const data = await api('/api/library/rescan', { method: 'POST' });
      const changed = Number(data && data.changed) || 0;
      const total = Number(data && data.tracks) || 0;
      toast(changed ? `Rescan done: ${plural(changed, 'change')}, ${plural(total, 'track')} in the library` : 'Rescan done: nothing new', 'success');
      await reloadTracks();
      await loadPlaylists();
      loadStatus();
    } catch (err) {
      showError(err);
    } finally {
      button.disabled = false;
    }
  }

  // ================================================================ Playlists

  /** Accept either a bare playlist dict or {playlist: {...}} from mutation endpoints. */
  function asPlaylist(data) {
    if (data && typeof data === 'object') {
      if (data.playlist && typeof data.playlist === 'object') return data.playlist;
      if (typeof data.id === 'string') return data;
    }
    return null;
  }

  async function loadPlaylists() {
    try {
      const data = await api('/api/playlists');
      state.playlists = Array.isArray(data.playlists) ? data.playlists : [];
      if (state.selectedPlaylistId && !state.playlists.some((p) => p.id === state.selectedPlaylistId)) state.selectedPlaylistId = null;
      if (!state.selectedPlaylistId && state.playlists.length) state.selectedPlaylistId = state.playlists[0].id;
      renderPlaylists();
    } catch (err) {
      showError(err);
    }
  }

  function currentPlaylist() {
    return state.playlists.find((p) => p.id === state.selectedPlaylistId) || null;
  }

  function renderPlaylists() {
    renderPlaylistList();
    renderPlaylistDetail();
  }

  function renderPlaylistList() {
    const list = $('#playlist-list');
    clear(list);
    if (!state.playlists.length) {
      list.append(emptyState('No playlists yet', 'list'));
      return;
    }
    for (const playlist of state.playlists) {
      const name = el('span', { class: 'pl-name', text: playlist.name, title: 'Double-click to rename' });
      const item = el(
        'li',
        {
          class: `pl-item${playlist.id === state.selectedPlaylistId ? ' active' : ''}`,
          tabIndex: 0,
          onclick: () => selectPlaylist(playlist.id),
          ondblclick: (event) => {
            if (event.target.closest('button, input')) return;
            startRename(playlist, name);
          },
          onkeydown: (event) => {
            if (event.target !== item) return;
            if (event.key === 'Enter') selectPlaylist(playlist.id);
            if (event.key === 'F2') startRename(playlist, name);
          },
        },
        icon('list'),
        name,
        el('span', { class: 'pl-count', text: String((playlist.track_ids || []).length) }),
        iconButton('trash', 'Delete playlist', () => deletePlaylist(playlist), { danger: true }),
      );
      list.append(item);
    }
  }

  function selectPlaylist(id) {
    if (state.selectedPlaylistId === id) return;
    state.selectedPlaylistId = id;
    renderPlaylists();
  }

  function startRename(playlist, nameEl) {
    if (!nameEl.isConnected) return;
    const input = el('input', { type: 'text', class: 'input input-sm', value: playlist.name, maxLength: 200, 'aria-label': 'Playlist name' });
    let finished = false;
    const finish = async (commit) => {
      if (finished) return;
      finished = true;
      const name = input.value.trim();
      input.replaceWith(nameEl);
      if (!commit || !name || name === playlist.name) return;
      await renamePlaylist(playlist, name);
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.code === 'Enter' || event.code === 'NumpadEnter') {
        event.preventDefault();
        finish(true);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        finish(false);
      }
    });
    input.addEventListener('blur', () => finish(true));
    input.addEventListener('click', (event) => event.stopPropagation());
    nameEl.replaceWith(input);
    input.focus();
    input.select();
  }

  async function renamePlaylist(playlist, name) {
    try {
      await api(`/api/playlists/${encodeURIComponent(playlist.id)}`, { method: 'PATCH', body: { name } });
      await loadPlaylists();
    } catch (err) {
      showError(err);
    }
  }

  async function renamePlaylistPrompt(playlist) {
    const name = await promptDialog({ title: 'Rename playlist', label: 'Name', value: playlist.name, confirmLabel: 'Rename' });
    if (name === null || !name || name === playlist.name) return;
    await renamePlaylist(playlist, name);
  }

  async function createPlaylist(trackIds = []) {
    const name = await promptDialog({ title: 'New playlist', label: 'Name', placeholder: 'My playlist', confirmLabel: 'Create' });
    if (!name) return;
    try {
      const created = asPlaylist(await api('/api/playlists', { method: 'POST', body: { name } }));
      if (created && trackIds.length) {
        await api(`/api/playlists/${encodeURIComponent(created.id)}/tracks`, { method: 'POST', body: { track_ids: trackIds } });
      }
      if (created) state.selectedPlaylistId = created.id;
      await loadPlaylists();
      toast(trackIds.length ? `Created “${name}” with ${plural(trackIds.length, 'track')}` : `Created “${name}”`, 'success');
      if (trackIds.length) clearSelection();
      else switchTab('playlists');
    } catch (err) {
      showError(err);
    }
  }

  async function deletePlaylist(playlist) {
    const { ok } = await confirmDialog({
      title: 'Delete playlist',
      message: `Delete the playlist “${playlist.name}”? The tracks stay in your library.`,
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await api(`/api/playlists/${encodeURIComponent(playlist.id)}`, { method: 'DELETE' });
      state.exportPaths.delete(playlist.id);
      if (state.selectedPlaylistId === playlist.id) state.selectedPlaylistId = null;
      await loadPlaylists();
      toast(`Deleted “${playlist.name}”`, 'success');
    } catch (err) {
      showError(err);
    }
  }

  function openAddToPlaylistMenu(anchor, trackIds) {
    if (!trackIds.length) return;
    const items = state.playlists.map((p) => ({ label: p.name, icon: 'list', onClick: () => addToPlaylist(p, trackIds) }));
    if (!items.length) items.push({ label: 'No playlists yet', disabled: true });
    items.push('sep', { label: 'New playlist…', icon: 'plus', onClick: () => createPlaylist(trackIds) });
    openMenu(anchor, items);
  }

  async function addToPlaylist(playlist, trackIds) {
    try {
      await api(`/api/playlists/${encodeURIComponent(playlist.id)}/tracks`, { method: 'POST', body: { track_ids: trackIds } });
      await loadPlaylists();
      toast(`Added ${plural(trackIds.length, 'track')} to “${playlist.name}”`, 'success');
      clearSelection();
    } catch (err) {
      showError(err);
    }
  }

  async function removeFromPlaylist(playlist, trackId) {
    try {
      await api(`/api/playlists/${encodeURIComponent(playlist.id)}/tracks/${encodeURIComponent(trackId)}`, { method: 'DELETE' });
      await loadPlaylists();
    } catch (err) {
      showError(err);
    }
  }

  async function setOrder(playlist, trackIds) {
    const previous = playlist.track_ids;
    playlist.track_ids = trackIds; // optimistic
    renderPlaylistDetail();
    try {
      await api(`/api/playlists/${encodeURIComponent(playlist.id)}`, { method: 'PATCH', body: { track_ids: trackIds } });
      await loadPlaylists();
    } catch (err) {
      playlist.track_ids = previous;
      renderPlaylistDetail();
      showError(err);
    }
  }

  function moveTrack(playlist, from, delta) {
    const ids = (playlist.track_ids || []).slice();
    const to = from + delta;
    if (to < 0 || to >= ids.length) return;
    [ids[from], ids[to]] = [ids[to], ids[from]];
    setOrder(playlist, ids);
  }

  async function exportPlaylist(playlist) {
    try {
      const data = await api(`/api/playlists/${encodeURIComponent(playlist.id)}/export`, { method: 'POST' });
      const path = data && typeof data.path === 'string' ? data.path : '';
      state.exportPaths.set(playlist.id, path);
      renderPlaylistDetail();
      toast(path ? `Exported “${playlist.name}” to ${path}` : `Exported “${playlist.name}”`, 'success', 6000);
    } catch (err) {
      showError(err);
    }
  }

  let detailDirty = false; // a re-render was requested while a row was being dragged

  function renderPlaylistDetail() {
    if (dragIndex !== null) {
      detailDirty = true; // rebuilding the rows mid-drag would detach the one being dragged
      return;
    }
    detailDirty = false;
    const host = $('#playlist-detail');
    const previousScroller = host.querySelector('.scroll');
    const top = previousScroller ? previousScroller.scrollTop : 0;
    clearCovers(host);
    const playlist = currentPlaylist();
    if (!playlist) {
      host.append(
        emptyState(
          state.playlists.length ? 'Select a playlist on the left' : 'No playlists yet. Create one, then add tracks from the Library tab.',
          'list',
        ),
      );
      return;
    }
    const ids = playlist.track_ids || [];
    const present = ids.map((id) => state.trackIndex.get(id)).filter(Boolean);
    const totalSeconds = present.reduce((sum, t) => sum + (Number(t.duration) || 0), 0);
    const missing = ids.length - present.length;
    const subtitle = [plural(ids.length, 'track'), fmtDuration(totalSeconds)];
    if (missing) subtitle.push(`${missing} missing`);

    // The head and export banner stay put; only the table scrolls (in its own .scroll box) so
    // the sticky column header cannot slide under the head.
    const scroller = el('div', { class: 'scroll' });
    host.append(
      el(
        'div',
        { class: 'pl-head' },
        el('div', { class: 'pl-head-text' }, el('h3', { text: playlist.name }), el('div', { class: 'muted', text: subtitle.join(' · ') })),
        el(
          'div',
          { class: 'pl-actions' },
          el(
            'button',
            { type: 'button', class: 'btn btn-sm btn-primary', disabled: !present.length, onclick: () => playTracks(present, 0, playlist.name) },
            icon('play'),
            'Play all',
          ),
          el(
            'button',
            { type: 'button', class: 'btn btn-sm', disabled: !present.length, onclick: () => exportPlaylist(playlist), title: 'Write an .m3u8 file next to your music' },
            icon('download'),
            'Export M3U8',
          ),
          el('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => renamePlaylistPrompt(playlist) }, icon('edit'), 'Rename'),
          el('button', { type: 'button', class: 'btn btn-sm btn-ghost btn-danger-ghost', onclick: () => deletePlaylist(playlist) }, icon('trash'), 'Delete'),
        ),
      ),
    );

    if (state.exportPaths.has(playlist.id)) {
      const path = state.exportPaths.get(playlist.id);
      host.append(
        el(
          'div',
          { class: 'export-info' },
          icon('check'),
          el('span', { text: path ? 'Exported to' : 'Exported.' }),
          path ? el('code', { text: path }) : null,
          el(
            'a',
            { href: `/api/playlists/${encodeURIComponent(playlist.id)}/export.m3u8`, title: 'Download a copy with absolute paths (for VLC on this machine)' },
            icon('link'),
            ' Download .m3u8',
          ),
        ),
      );
    }

    host.append(scroller);
    if (!ids.length) {
      scroller.append(emptyState('This playlist is empty. Add tracks from the Library tab with the + button.', 'music'));
      return;
    }

    const tbody = el('tbody');
    ids.forEach((id, index) => {
      const track = state.trackIndex.get(id) || null;
      const row = el(
        'tr',
        {
          class: `track-row${track ? '' : ' missing'}${track && playingId() === id ? ' playing' : ''}`,
          dataset: { id },
          draggable: true,
          onclick: (event) => {
            if (!track || event.target.closest('button, input, a')) return;
            playTracks(present, present.indexOf(track), playlist.name);
          },
        },
        el('td', { class: 'col-idx' }, el('span', { class: 'grip', title: 'Drag to reorder' }, icon('grip')), String(index + 1)),
        el('td', { class: 'col-cover' }, coverThumb(track)),
        el(
          'td',
          null,
          el('div', { class: 'cell-title', text: track ? track.title || 'Untitled' : 'Missing track' }),
          el('div', { class: 'cell-sub muted', text: track ? track.artist || 'Unknown artist' : `Not in the library any more (${id})` }),
        ),
        el('td', { class: 'col-num', text: track ? fmtDuration(track.duration) : '' }),
        el(
          'td',
          { class: 'col-actions' },
          el(
            'div',
            { class: 'row-actions' },
            iconButton('up', 'Move up', () => moveTrack(playlist, index, -1), { disabled: index === 0 }),
            iconButton('down', 'Move down', () => moveTrack(playlist, index, 1), { disabled: index === ids.length - 1 }),
            iconButton('x', 'Remove from playlist', () => removeFromPlaylist(playlist, id), { danger: true }),
          ),
        ),
      );
      attachDragAndDrop(row, playlist, index);
      tbody.append(row);
    });
    scroller.append(
      el(
        'table',
        { class: 'tracks' },
        el(
          'thead',
          null,
          el(
            'tr',
            null,
            el('th', { class: 'col-idx', text: '#' }),
            el('th', { class: 'col-cover' }),
            el('th', { text: 'Track' }),
            el('th', { class: 'col-num', text: 'Length' }),
            el('th', { class: 'col-actions' }),
          ),
        ),
        tbody,
      ),
    );
    if (top) scroller.scrollTop = top;
  }

  let dragIndex = null;

  function clearDropMarkers() {
    for (const node of document.querySelectorAll('.drop-before, .drop-after')) node.classList.remove('drop-before', 'drop-after');
  }

  function attachDragAndDrop(row, playlist, index) {
    row.addEventListener('dragstart', (event) => {
      dragIndex = index;
      row.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      try {
        event.dataTransfer.setData('text/plain', String(index));
      } catch (_err) {
        /* some browsers throw for unsupported types; the index is kept in dragIndex anyway */
      }
    });
    row.addEventListener('dragend', () => {
      dragIndex = null;
      row.classList.remove('dragging');
      clearDropMarkers();
      if (detailDirty) renderPlaylistDetail(); // the refresh that was held back during the drag
    });
    row.addEventListener('dragover', (event) => {
      if (dragIndex === null) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      const rect = row.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      row.classList.toggle('drop-after', after);
      row.classList.toggle('drop-before', !after);
    });
    row.addEventListener('dragleave', () => row.classList.remove('drop-before', 'drop-after'));
    row.addEventListener('drop', (event) => {
      if (dragIndex === null) return;
      event.preventDefault();
      const rect = row.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      const from = dragIndex;
      let to = index + (after ? 1 : 0);
      dragIndex = null;
      clearDropMarkers();
      if (to > from) to -= 1;
      if (to === from) return;
      const ids = (playlist.track_ids || []).slice();
      const [moved] = ids.splice(from, 1);
      ids.splice(to, 0, moved);
      setOrder(playlist, ids);
    });
  }

  // ================================================================ Player

  const audio = $('#audio');
  const player = {
    queue: [], // tracks of the list that started playback
    order: [], // indices into queue, shuffled or not
    pos: -1, // position inside order
    current: null,
    shuffle: false,
    repeat: 'off', // off | all | one
    label: '',
    seeking: false,
    lastVolume: 1,
    errorStreak: 0, // consecutive tracks that failed to play; stops auto-skip from looping forever
  };

  const playingId = () => (player.current ? player.current.id : null);

  function shuffleArray(items) {
    for (let i = items.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [items[i], items[j]] = [items[j], items[i]];
    }
    return items;
  }

  function buildOrder(startIndex) {
    const indices = [...Array(player.queue.length).keys()];
    if (player.shuffle) {
      const rest = shuffleArray(indices.filter((i) => i !== startIndex));
      player.order = [startIndex, ...rest];
    } else {
      player.order = indices;
    }
    player.pos = player.order.indexOf(startIndex);
  }

  /** Start playing `tracks[index]`; the list becomes the current queue (prev/next/shuffle apply to it). */
  function playTracks(tracks, index, label) {
    const list = (tracks || []).filter((t) => t && t.id);
    if (!list.length) return;
    const start = Math.max(0, Math.min(Number(index) || 0, list.length - 1));
    const sameTrack = player.current && player.current.id === list[start].id && player.queue.length && !audio.error;
    if (sameTrack && player.label === (label || '')) {
      // The same row of the same list was clicked again: toggle instead of restarting.
      player.queue = list.slice();
      buildOrder(start);
      togglePlay();
      return;
    }
    player.queue = list.slice();
    player.label = label || '';
    buildOrder(start);
    if (sameTrack) {
      // "Play all" on a playlist whose first entry is already playing: adopt the new queue and
      // keep going (or resume) instead of pausing.
      player.pos = player.order.indexOf(start);
      renderNowPlaying();
      highlightPlaying();
      if (audio.paused) togglePlay();
      return;
    }
    playAtPos(player.pos);
  }

  /** A track left the library: drop it from the queue and stop it if it was the one playing. */
  function forgetInPlayer(trackId) {
    if (!player.queue.some((t) => t.id === trackId)) return;
    const wasCurrent = player.current && player.current.id === trackId;
    const currentId = player.current ? player.current.id : null;
    player.queue = player.queue.filter((t) => t.id !== trackId);
    if (wasCurrent) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
      player.current = null;
      player.pos = -1;
      player.order = [...Array(player.queue.length).keys()];
      if (player.shuffle) shuffleArray(player.order);
      renderNowPlaying();
      highlightPlaying();
      updateSeek();
      return;
    }
    const keep = player.queue.findIndex((t) => t.id === currentId);
    buildOrder(Math.max(0, keep));
  }

  function playAtPos(pos) {
    const index = player.order[pos];
    const track = player.queue[index];
    if (!track) return;
    player.pos = pos;
    player.current = track;
    audio.src = mediaUrl(track.id);
    audio.load();
    const attempt = audio.play();
    if (attempt && typeof attempt.catch === 'function') {
      attempt.catch((err) => {
        // AbortError: superseded by another play(). NotSupportedError: the <audio> 'error' event reports it.
        if (err && (err.name === 'AbortError' || err.name === 'NotSupportedError')) return;
        toast(`Cannot play “${trackLabel(track)}”: ${err && err.message ? err.message : 'playback failed'}`);
      });
    }
    renderNowPlaying();
    highlightPlaying();
  }

  /** A track that cannot be played: report it once, then move on (but never loop over a broken queue). */
  function onPlaybackError() {
    const track = player.current;
    if (!track) return;
    const code = audio.error && audio.error.code;
    const reason =
      code === 4 ? 'the file is missing or not playable' : code === 2 ? 'network error' : code === 3 ? 'the file could not be decoded' : 'playback failed';
    toast(`Cannot play “${trackLabel(track)}”: ${reason}`);
    updatePlayButton();
    player.errorStreak += 1;
    if (player.queue.length > 1 && player.errorStreak < player.queue.length) setTimeout(() => playNext(true), 400);
  }

  function togglePlay() {
    if (!player.current) {
      if (state.tracks.length) playTracks(state.tracks, 0, state.query ? 'Search results' : 'Library');
      return;
    }
    if (audio.paused) {
      const attempt = audio.play();
      if (attempt && typeof attempt.catch === 'function') attempt.catch(() => {});
    } else {
      audio.pause();
    }
  }

  function playNext(auto = false) {
    if (!player.queue.length) return;
    if (auto && player.repeat === 'one') {
      audio.currentTime = 0;
      const attempt = audio.play();
      if (attempt && typeof attempt.catch === 'function') attempt.catch(() => {});
      return;
    }
    let pos = player.pos + 1;
    if (pos >= player.order.length) {
      if (auto && player.repeat !== 'all') {
        audio.pause();
        audio.currentTime = 0;
        updatePlayButton();
        return;
      }
      if (player.shuffle) buildOrder(player.order[Math.floor(Math.random() * player.order.length)]);
      pos = 0;
    }
    playAtPos(pos);
  }

  function playPrev() {
    if (!player.queue.length) return;
    if (audio.currentTime > 3) {
      audio.currentTime = 0;
      return;
    }
    const pos = player.pos - 1 < 0 ? player.order.length - 1 : player.pos - 1;
    playAtPos(pos);
  }

  function toggleShuffle() {
    player.shuffle = !player.shuffle;
    if (player.queue.length) buildOrder(player.order[player.pos] ?? 0);
    updateToggles();
    savePrefs();
  }

  function cycleRepeat() {
    player.repeat = player.repeat === 'off' ? 'all' : player.repeat === 'all' ? 'one' : 'off';
    updateToggles();
    savePrefs();
  }

  function updateToggles() {
    const shuffle = $('#btn-shuffle');
    shuffle.classList.toggle('on', player.shuffle);
    shuffle.setAttribute('aria-pressed', String(player.shuffle));
    shuffle.title = player.shuffle ? 'Shuffle: on' : 'Shuffle: off';
    const repeat = $('#btn-repeat');
    repeat.classList.toggle('on', player.repeat !== 'off');
    repeat.setAttribute('aria-pressed', String(player.repeat !== 'off'));
    repeat.title = `Repeat: ${player.repeat === 'one' ? 'this track' : player.repeat}`;
    $('.badge', repeat).hidden = player.repeat !== 'one';
  }

  function updatePlayButton() {
    const button = $('#btn-play');
    const playing = player.current && !audio.paused && !audio.ended;
    setIcon(button, playing ? 'pause' : 'play');
    button.title = playing ? 'Pause (Space)' : 'Play (Space)';
    button.disabled = !player.current && !state.tracks.length;
  }

  function renderNowPlaying() {
    const track = player.current;
    $('#np-title').textContent = track ? track.title || 'Untitled' : 'Nothing playing';
    $('#np-artist').textContent = track
      ? `${track.artist || 'Unknown artist'}${player.label ? ` · ${player.label}` : ''}`
      : 'Pick a track from the library';
    const coverHost = $('#np-cover');
    clear(coverHost);
    coverHost.append(coverThumb(track, { size: 'np', eager: true }));
    document.title = track ? `${track.title || 'Untitled'} · ${track.artist || 'Unknown artist'} — Ultimate Playlist` : 'Ultimate Playlist';
    $('#btn-prev').disabled = !track;
    $('#btn-next').disabled = !track;
    $('#seek').disabled = !track;
    updatePlayButton();
    if ('mediaSession' in navigator && typeof window.MediaMetadata === 'function') {
      try {
        navigator.mediaSession.metadata = track
          ? new window.MediaMetadata({
              title: track.title || 'Untitled',
              artist: track.artist || '',
              album: track.album || '',
              artwork: track.has_cover === false ? [] : [{ src: coverUrl(track.id) }],
            })
          : null;
      } catch (_err) {
        /* optional nicety */
      }
    }
  }

  function highlightPlaying() {
    const id = playingId();
    for (const row of document.querySelectorAll('tr.track-row')) row.classList.toggle('playing', Boolean(id) && row.dataset.id === id);
    renderQueue();
  }

  function updateSeek() {
    if (player.seeking) return;
    const duration = audio.duration;
    const seek = $('#seek');
    if (Number.isFinite(duration) && duration > 0) {
      seek.value = String(Math.round((audio.currentTime / duration) * 1000));
      $('#time-dur').textContent = fmtDuration(duration);
    } else {
      seek.value = '0';
      $('#time-dur').textContent = player.current && player.current.duration ? fmtDuration(player.current.duration) : '0:00';
    }
    $('#time-cur').textContent = fmtDuration(audio.currentTime || 0);
  }

  function updateVolumeIcon() {
    const muted = audio.muted || audio.volume === 0;
    setIcon($('#btn-mute'), muted ? 'mute' : 'volume');
    $('#btn-mute').title = muted ? 'Unmute' : 'Mute';
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({ volume: audio.volume, shuffle: player.shuffle, repeat: player.repeat }));
    } catch (_err) {
      /* storage may be unavailable; preferences are a convenience only */
    }
  }

  function restorePrefs() {
    let prefs = null;
    try {
      prefs = JSON.parse(localStorage.getItem(PREFS_KEY) || 'null');
    } catch (_err) {
      prefs = null;
    }
    if (prefs && typeof prefs === 'object') {
      if (typeof prefs.volume === 'number' && prefs.volume >= 0 && prefs.volume <= 1) audio.volume = prefs.volume;
      if (typeof prefs.shuffle === 'boolean') player.shuffle = prefs.shuffle;
      if (['off', 'all', 'one'].includes(prefs.repeat)) player.repeat = prefs.repeat;
    }
    $('#volume').value = String(audio.volume);
    player.lastVolume = audio.volume || 1;
    updateToggles();
    updateVolumeIcon();
  }

  function initPlayer() {
    $('#btn-play').addEventListener('click', togglePlay);
    $('#btn-prev').addEventListener('click', playPrev);
    $('#btn-next').addEventListener('click', () => playNext(false));
    $('#btn-shuffle').addEventListener('click', toggleShuffle);
    $('#btn-repeat').addEventListener('click', cycleRepeat);
    $('#btn-mute').addEventListener('click', () => {
      if (audio.muted || audio.volume === 0) {
        audio.muted = false;
        if (audio.volume === 0) audio.volume = player.lastVolume || 1;
      } else {
        player.lastVolume = audio.volume;
        audio.muted = true;
      }
      $('#volume').value = String(audio.muted ? 0 : audio.volume);
      updateVolumeIcon();
    });

    const seek = $('#seek');
    seek.addEventListener('pointerdown', () => {
      player.seeking = true;
    });
    seek.addEventListener('input', () => {
      player.seeking = true;
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        $('#time-cur').textContent = fmtDuration((Number(seek.value) / 1000) * audio.duration);
      }
    });
    seek.addEventListener('change', () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) audio.currentTime = (Number(seek.value) / 1000) * audio.duration;
      player.seeking = false;
      updateSeek();
    });
    seek.addEventListener('pointerup', () => {
      player.seeking = false;
    });

    const volume = $('#volume');
    volume.addEventListener('input', () => {
      audio.volume = Number(volume.value);
      audio.muted = false;
      if (audio.volume > 0) player.lastVolume = audio.volume;
      updateVolumeIcon();
      savePrefs();
    });

    audio.addEventListener('timeupdate', updateSeek);
    audio.addEventListener('durationchange', updateSeek);
    audio.addEventListener('loadedmetadata', updateSeek);
    audio.addEventListener('play', () => {
      updatePlayButton();
      renderQueue(); // the queue's Play/Playing button follows the real playback state
    });
    audio.addEventListener('pause', () => {
      updatePlayButton();
      renderQueue();
    });
    audio.addEventListener('ended', () => playNext(true));
    audio.addEventListener('volumechange', updateVolumeIcon);
    audio.addEventListener('error', onPlaybackError);
    audio.addEventListener('playing', () => {
      player.errorStreak = 0;
    });

    if ('mediaSession' in navigator) {
      const handlers = [
        ['play', togglePlay],
        ['pause', togglePlay],
        ['previoustrack', playPrev],
        ['nexttrack', () => playNext(false)],
      ];
      for (const [name, handler] of handlers) {
        try {
          navigator.mediaSession.setActionHandler(name, handler);
        } catch (_err) {
          /* unsupported action */
        }
      }
    }

    restorePrefs();
    renderNowPlaying();
  }

  // ================================================================ Tabs, keyboard, init

  function switchTab(name) {
    if (name !== 'library' && name !== 'playlists') return;
    state.tab = name;
    for (const tab of document.querySelectorAll('.tab')) {
      const active = tab.dataset.tab === name;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
    }
    $('#tab-library').classList.toggle('active', name === 'library');
    $('#tab-playlists').classList.toggle('active', name === 'playlists');
    if (window.location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
    if (name === 'library') renderLibrary(); // re-observe lazy covers that were hidden
    else renderPlaylistDetail();
  }

  function isTypingTarget(target) {
    if (!target || !(target instanceof Element)) return false;
    if (target.isContentEditable) return true;
    return target.matches('input, textarea, select');
  }

  function initKeyboard() {
    document.addEventListener('keydown', (event) => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
      if (isTypingTarget(event.target) || dialogEl.open || !menuEl.hidden) return;
      // A button reached with Tab keeps its native Space activation.
      const onControl = event.target instanceof Element && event.target.matches('button, a, [role="menuitem"]');
      if (event.key === ' ' || event.code === 'Space') {
        if (onControl) return;
        event.preventDefault();
        togglePlay();
      } else if (event.key === 'ArrowRight' && player.current && !onControl) {
        event.preventDefault();
        audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 5);
      } else if (event.key === 'ArrowLeft' && player.current && !onControl) {
        event.preventDefault();
        audio.currentTime = Math.max(0, audio.currentTime - 5);
      }
    });
    // After a mouse click the button would keep focus, so Space would re-fire it instead of
    // toggling playback. Drop focus for mouse clicks only (keyboard-triggered clicks have detail 0).
    document.addEventListener('click', (event) => {
      if (event.detail === 0 || !(event.target instanceof Element)) return;
      const button = event.target.closest('button');
      if (button && button !== document.activeElement) return;
      if (button && !dialogEl.contains(button) && !menuEl.contains(button)) button.blur();
    });
  }

  function init() {
    const input = $('#url-input');
    $('#download-form').addEventListener('submit', (event) => {
      event.preventDefault();
      submitUrls();
    });
    input.addEventListener('keydown', (event) => {
      const isEnter = event.key === 'Enter' || event.code === 'Enter' || event.code === 'NumpadEnter';
      if (isEnter && !event.shiftKey) {
        event.preventDefault();
        submitUrls();
      }
    });
    input.addEventListener('input', () => autosize(input));
    input.addEventListener('paste', () => setTimeout(() => autosize(input), 0));
    $('#clear-finished').addEventListener('click', clearFinished);

    for (const tab of document.querySelectorAll('.tab')) tab.addEventListener('click', () => switchTab(tab.dataset.tab));

    $('#search').addEventListener('input', (event) => {
      state.query = event.target.value.trim();
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadLibrary, SEARCH_DEBOUNCE_MS);
    });
    $('#add-selected').addEventListener('click', (event) => openAddToPlaylistMenu(event.currentTarget, [...state.selection]));
    $('#clear-selection').addEventListener('click', clearSelection);
    $('#rescan').addEventListener('click', rescanLibrary);
    $('#new-playlist').addEventListener('click', () => createPlaylist([]));

    initPlayer();
    initKeyboard();

    if (window.location.hash === '#playlists') switchTab('playlists');

    loadStatus();
    pollJobs();
    loadLibrary();
    loadPlaylists();
    setInterval(loadStatus, STATUS_POLL_MS);
    window.addEventListener('hashchange', () => switchTab(window.location.hash.slice(1) || 'library'));
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
