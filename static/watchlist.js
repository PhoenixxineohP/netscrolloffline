/* watchlist.js — init, scan, flag, import, rescan, remove, add-to-library. */
(function () {
  const rowsBox = document.getElementById('wl-rows');
  const jobbox = document.getElementById('wl-jobbox');
  const jobCount = document.getElementById('wl-job-count');
  const jobBar = document.getElementById('wl-job-bar');
  const jobMsg = document.getElementById('wl-job-msg');
  const summary = document.getElementById('wl-summary');
  const importBtn = document.getElementById('wl-import');
  const scanBtn = document.getElementById('wl-scan');

  // ---------- scan entire list ----------
  async function launchScan() {
    summary.textContent = 'Scanning…';
    jobbox.hidden = false;
    jobMsg.textContent = '';
    try {
      const res = await fetch('/api/watchlist/scan', { method: 'POST' });
      const j = await res.json();
      if (j.error) { alert(j.error); return; }
      startScanPoll(j.job);
    } catch (e) { alert('Scan failed: ' + e.message); }
  }

  function startScanPoll(jobId) {
    pollTimer = setInterval(pollJob, 700);
    pollJob();
  }
  function pollJob() {
    fetch(`/api/job/${jobId}`).then(r => r.json()).then(j => {
      if (!j) { clearInterval(pollTimer); return; }
      const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
      jobCount.textContent = `${j.done}/${j.total}`;
      jobBar.style.width = pct + '%';
      jobMsg.textContent = j.message || '';
      if (j.status === 'done' || j.status === 'idle') {
        clearInterval(pollTimer); pollTimer = null;
        location.reload();
      }
    }).catch(() => { clearInterval(pollTimer); pollTimer = null; });
  }

  // ---------- flag unlock type ----------
  window.flag = function (sel) {
    const row = sel.closest('.wrow');
    const wid = row.dataset.id;
    const unlock = sel.value;
    if (!wid) return;
    fetch('/api/watchlist/flag', { method: 'POST',
      body: new FormData().append('id', wid).append('unlock', unlock) }).then(() => {});
  };

  // ---------- remove row ----------
  document.addEventListener('click', e => {
    const btn = e.target.closest('.del');
    if (!btn) return;
    if (!confirm('Remove this from the watchlist?')) return;
    const row = btn.closest('.wrow');
    const wid = row.dataset.id;
    fetch('/api/watchlist/remove', { method: 'POST', body: new FormData().append('id', wid) })
      .then(() => {
        row.remove();
        if (!rowsBox.children.length) summary.textContent = 'Watchlist is empty';
      });
  });

  // ---------- re‑scan a single row ----------
  document.addEventListener('click', e => {
    const btn = e.target.closest('.act-rescan');
    if (!btn) return;
    const row = btn.closest('.wrow');
    const wid = row.dataset.id;
    if (!wid) return;
    fetch('/api/job/wl-rescan', { method: 'POST',
      body: new FormData().append('id', wid) }).then(r => r.json())
      .then(j => { if (j.message) alert(j.message); });
  });

  // ---------- import preset ----------
  importBtn.addEventListener('click', () => {
    fetch('/api/watchlist/import').then(r => r.json()).then(j => {
      if (j.error) { alert(j.error); return; }
      summary.textContent = `Imported/updated ${j.added} entries`;
      // fire off a quick limited-scan so anything newly import‑matched appears
      fetch('/api/watchlist/scan', { method: 'POST', body: new FormData().append('limit', 30) })
        .then(r => r.json()).then(jj => { if (jj.status === 'done') summary.textContent = 'Limited scan complete, full scan available below'; });
    });
  });

  // ---------- scan-run UI ----------
  launchScan(); // initial auto-scan on page load
})();
</script>