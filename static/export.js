/* Library page: bulk-export everything in Phoenix Records as CBZ or folders. */
(function () {
  const box = document.getElementById('exportbox');
  if (!box) return;

  const bar = box.querySelector('.exp-bar > div');
  const count = box.querySelector('.exp-count');
  const status = box.querySelector('.exp-status');
  const msg = box.querySelector('.exp-msg');
  const link = box.querySelector('.exp-link');
  const fmtButtons = box.querySelectorAll('button[data-format]');

  let timer = null;
  let job = null;
  let starting = false;

  function setBusy(busy) {
    fmtButtons.forEach(b => { b.disabled = busy; });
  }

  function post(url, body) {
    const form = new FormData();
    for (const [k, v] of Object.entries(body)) form.append(k, v);
    return fetch(url, { method: 'POST', body: form }).then(r => r.json());
  }

  function start(fmt) {
    if (timer || starting) return;
    starting = true;
    setBusy(true);
    link.hidden = true;
    msg.textContent = '';
    status.hidden = false;
    status.textContent = 'Preparing…';
    bar.style.width = '0%';
    count.textContent = '';

    post('/api/export', { format: fmt }).then(res => {
      starting = false;
      if (!res || res.error) {
        setBusy(false);
        status.textContent = res && res.error ? res.error : 'Could not start export.';
        return;
      }
      job = res.job;
      timer = setInterval(poll, 700);
      poll();
    }).catch(() => {
      starting = false;
      setBusy(false);
      status.textContent = 'Could not start export.';
    });
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    job = null;
    setBusy(false);
  }

  function poll() {
    if (!job) { stop(); return; }
    fetch(`/api/job/${job}`).then(r => r.json()).then(j => {
      if (!j || !j.id) { stop(); return; }
      const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
      bar.style.width = pct + '%';
      if (j.total) count.textContent = `${j.done} / ${j.total} episodes (${pct}%)`;
      if (j.message) msg.textContent = j.message;

      if (j.status === 'done') {
        stop();
        status.textContent = j.size_mb ? `Ready — ${j.size_mb} MB` : 'Ready';
        if (j.url) {
          link.href = j.url;
          link.setAttribute('download', j.filename || '');
          link.textContent = `⬇ Save ${j.filename || 'archive'}`;
          link.hidden = false;
          // Kick off the download immediately; the link stays for a retry.
          link.click();
        }
      } else if (j.status === 'error') {
        stop();
        status.textContent = j.message || 'Export failed.';
      }
    }).catch(() => {
      stop();
      status.textContent = 'Lost contact with the export job.';
    });
  }

  fmtButtons.forEach(b => {
    b.addEventListener('click', () => start(b.dataset.format));
  });
})();
