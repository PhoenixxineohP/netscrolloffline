/* Library page: adopt downloads that already exist on disk into the library. */
(function () {
  const box = document.getElementById('importbox');
  if (!box) return;

  const pathInput = document.getElementById('imp-path');
  const lookupInput = document.getElementById('imp-lookup');
  const goBtn = document.getElementById('imp-go');
  const bar = box.querySelector('#imp-bar > div');
  const count = document.getElementById('imp-count');
  const status = document.getElementById('imp-status');
  const msg = document.getElementById('imp-msg');

  let timer = null;
  let job = null;
  let starting = false;

  function post(url, body) {
    const form = new FormData();
    for (const [k, v] of Object.entries(body || {})) form.append(k, v);
    return fetch(url, { method: 'POST', body: form }).then(r => r.json());
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    job = null;
    goBtn.disabled = false;
  }

  function poll() {
    if (!job) { stop(); return; }
    fetch(`/api/job/${job}`).then(r => r.json()).then(j => {
      if (!j || !j.id) { stop(); return; }
      const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
      bar.style.width = pct + '%';
      if (j.total) count.textContent = `${j.done} / ${j.total} series (${pct}%)`;
      if (j.message) msg.textContent = j.message;

      if (j.status === 'done') {
        stop();
        status.hidden = false;
        status.textContent = 'Import finished';
        msg.textContent = j.message || 'Import finished';
        // Reload so the library grid reflects the newly adopted series.
        setTimeout(() => window.location.reload(), 900);
      } else if (j.status === 'error' || j.status === 'cancelled') {
        stop();
        status.hidden = false;
        status.textContent = j.status === 'cancelled' ? 'Cancelled' : 'Import failed';
        msg.textContent = j.message || j.error || 'Import failed';
      }
    }).catch(() => {
      stop();
      msg.textContent = 'Lost contact with the import job.';
    });
  }

  goBtn.addEventListener('click', () => {
    if (timer || starting) return;
    starting = true;
    goBtn.disabled = true;
    bar.style.width = '0%';
    count.textContent = '';
    status.hidden = true;
    msg.textContent = 'Scanning folders…';

    post('/api/library/import', {
      path: pathInput.value.trim(),
      lookup: lookupInput.checked ? '1' : '0',
    }).then(res => {
      starting = false;
      if (!res || res.error) {
        goBtn.disabled = false;
        msg.textContent = (res && res.error) || 'Could not start the import.';
        return;
      }
      job = res.job;
      if (res.series) {
        msg.textContent = `Found ${res.series} series, ${res.episodes} episode(s).`;
      }
      timer = setInterval(poll, 600);
      poll();
    }).catch(() => {
      starting = false;
      goBtn.disabled = false;
      msg.textContent = 'Could not start the import.';
    });
  });
})();
