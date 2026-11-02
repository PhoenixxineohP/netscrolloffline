/* queue.js — the download queue: slots, pause, retry, cancel, auto-retry. */
(function () {
  const list = document.getElementById('q-list');
  const empty = document.getElementById('q-empty');
  const statusEl = document.getElementById('q-status');
  const slotsInput = document.getElementById('q-slots');
  const slotsOut = document.getElementById('q-slots-out');
  const pauseBtn = document.getElementById('q-pause');
  const autoRetryBox = document.getElementById('q-auto-retry');
  const maxAttemptsInput = document.getElementById('q-max-attempts');

  const STATUS_LABEL = {
    queued: 'Queued', running: 'Running', done: 'Done',
    error: 'Failed', cancelled: 'Cancelled',
  };

  let rows = new Map(); // job id -> element
  let lastSnapshot = null;

  function form(obj) {
    const f = new FormData();
    for (const [k, v] of Object.entries(obj || {})) f.append(k, v);
    return f;
  }

  function action(url, body) {
    return fetch(url, { method: 'POST', body: form(body) })
      .then(r => r.json())
      .then(j => { if (j.error) alert(j.error); return j; })
      .catch(() => null);
  }

  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function jobRow(job) {
    const el = document.createElement('div');
    el.className = 'eprow qrow-item';
    el.dataset.id = job.id;
    el.innerHTML = `
      <span class="no qstate"></span>
      <span class="ti">
        <b class="qdesc"></b>
        <span class="muted qmeta"></span>
        <div class="bar"><div class="qbar"></div></div>
        <span class="jobmsg qmsg"></span>
      </span>
      <span class="btns"></span>`;
    return el;
  }

  function renderJob(job) {
    let el = rows.get(job.id);
    if (!el) {
      el = jobRow(job);
      rows.set(job.id, el);
      list.appendChild(el);
    }
    el.className = 'eprow qrow-item q-' + job.status;
    el.querySelector('.qstate').textContent = job.status === 'running' ? '▶'
      : job.status === 'done' ? '✓' : job.status === 'error' ? '!' : '•';
    el.querySelector('.qdesc').textContent = job.desc || job.id;
    const pct = job.total ? Math.round(100 * job.done / job.total) : 0;
    el.querySelector('.qbar').style.width = pct + '%';
    const bits = [];
    const label = STATUS_LABEL[job.status] || job.status;
    bits.push(label);
    if (job.kind) bits.push(job.kind);
    if (job.total) bits.push(`${job.done}/${job.total} (${pct}%)`);
    if (job.attempts) bits.push(`attempt ${job.attempts}/${job.max_attempts}`);
    if (job.failed) bits.push(`${job.failed} failed`);
    if (job.status === 'done' && job.finished_at) bits.push(`at ${fmtTime(job.finished_at)}`);
    if (job.status === 'queued' && job.retry_at) bits.push(`retry at ${fmtTime(job.retry_at)}`);
    el.querySelector('.qmeta').textContent = bits.join(' · ');
    el.querySelector('.qmsg').textContent = job.error || job.message || '';

    const btns = el.querySelector('.btns');
    btns.innerHTML = '';
    const add = (text, cls, url, extra) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'mini ' + (cls || '');
      b.textContent = text;
      b.addEventListener('click', () => {
        if (extra && !confirm(extra)) return;
        action(url).then(() => refresh());
      });
      btns.appendChild(b);
    };
    if (job.status === 'running' || job.status === 'queued') {
      add('Cancel', 'danger', `/api/queue/cancel/${job.id}`);
    }
    if (job.status === 'error' || job.status === 'cancelled' || job.status === 'done') {
      add('Retry', '', `/api/queue/retry/${job.id}`);
    }
    if (job.status === 'queued' && job.retry_at) {
      add('Retry now', '', `/api/queue/retry/${job.id}`);
    }
    if (job.status !== 'running') {
      add('Remove', '', `/api/queue/remove/${job.id}`);
    }
    if (job.status === 'done' && job.url) {
      const a = document.createElement('a');
      a.className = 'exp-link';
      a.href = job.url;
      a.textContent = '⤓ Download';
      btns.appendChild(a);
    }
  }

  function applySnapshot(snap) {
    lastSnapshot = snap;
    slotsInput.value = snap.slots;
    slotsOut.textContent = snap.slots;
    pauseBtn.textContent = snap.paused ? '▶ Resume queue' : '⏸ Pause queue';
    pauseBtn.classList.toggle('primary', snap.paused);
    autoRetryBox.checked = !!snap.auto_retry;
    if (document.activeElement !== maxAttemptsInput) {
      maxAttemptsInput.value = snap.max_attempts;
    }
    const running = snap.jobs.filter(j => j.status === 'running').length;
    statusEl.textContent = [
      snap.paused ? 'paused' : 'running',
      `${running}/${snap.slots} slots busy`,
      `${snap.queued} waiting`,
      `${snap.jobs.length} tracked`,
    ].join(' · ');

    const seen = new Set();
    for (const job of snap.jobs) {
      seen.add(job.id);
      renderJob(job);
    }
    for (const [id, el] of rows) {
      if (!seen.has(id)) { el.remove(); rows.delete(id); }
    }
    // keep newest jobs on top (list is in creation order, so prepend oldest first)
    snap.jobs.forEach(job => {
      const el = rows.get(job.id);
      if (el) list.prepend(el);
    });
    empty.hidden = snap.jobs.length > 0;
  }

  function refresh() {
    return fetch('/api/queue').then(r => r.json()).then(applySnapshot).catch(() => null);
  }

  let polling = false;
  function slowPoll() {
    if (polling) return;
    polling = true;
    fetch('/api/queue').then(r => r.json()).then(applySnapshot)
      .catch(() => {})
      .finally(() => { polling = false; });
  }

  slotsInput.addEventListener('change', () => {
    slotsOut.textContent = slotsInput.value;
    action('/api/queue/slots', { slots: slotsInput.value }).then(refresh);
  });
  slotsInput.addEventListener('input', () => { slotsOut.textContent = slotsInput.value; });

  pauseBtn.addEventListener('click', () => {
    const paused = lastSnapshot && lastSnapshot.paused;
    action(paused ? '/api/queue/resume' : '/api/queue/pause').then(refresh);
  });

  document.getElementById('q-retry-failed').addEventListener('click', () =>
    action('/api/queue/retry-failed').then(refresh));
  document.getElementById('q-cancel-queued').addEventListener('click', () => {
    if (!confirm('Cancel every job that has not started yet?')) return;
    action('/api/queue/cancel-queued').then(refresh);
  });
  document.getElementById('q-clear').addEventListener('click', () => {
    if (!confirm('Remove all finished, failed and cancelled jobs from the queue?')) return;
    action('/api/queue/clear').then(() => {
      rows.forEach(el => el.remove());
      rows.clear();
      refresh();
    });
  });

  function pushAutoRetry() {
    action('/api/queue/auto-retry', {
      enabled: autoRetryBox.checked ? '1' : '0',
      max_attempts: maxAttemptsInput.value || '3',
    }).then(refresh);
  }
  autoRetryBox.addEventListener('change', pushAutoRetry);
  maxAttemptsInput.addEventListener('change', pushAutoRetry);

  refresh();
  setInterval(slowPoll, 1500);
})();
