/* Series page: PC archive jobs + "save to this device" offline copies. */
const jobbox = document.getElementById('jobbox');
const jobDesc = document.getElementById('job-desc');
const jobCount = document.getElementById('job-count');
const jobBar = document.getElementById('job-bar');
const jobMsg = document.getElementById('job-msg');
const deviceNote = document.getElementById('device-note');
let pollTimer = null;
let activeJob = null;

/* ---------- server job polling (Phoenix Records saves / checks) ---------- */
function poll() {
  if (!activeJob) return;
  fetch(`/api/job/${activeJob}`).then(r => r.json()).then(j => {
    if (!j || !j.id) { activeJob = null; return; }
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    jobCount.textContent = `${j.done}/${j.total} (${pct}%)`;
    jobBar.style.width = pct + '%';
    if (j.message) jobMsg.textContent = j.message;
    if (j.status === 'done' || j.status === 'error') {
      clearInterval(pollTimer); pollTimer = null; activeJob = null;
      if (j.status === 'error') {
        alert(j.message || 'The job failed.');
        const btn = document.getElementById('btn-refresh');
        if (btn) { btn.disabled = false; }
        return;
      }
      setTimeout(() => location.reload(), 900);
    }
  }).catch(() => { clearInterval(pollTimer); pollTimer = null; activeJob = null; });
}

function post(url, body) {
  const form = new FormData();
  for (const [k, v] of Object.entries(body)) form.append(k, v);
  return fetch(url, { method: 'POST', body: form }).then(r => r.json());
}

function startJob(url, body) {
  return post(url, body).then(res => {
    if (res.error) { alert(res.error); return; }
    activeJob = res.job;
    jobbox.hidden = false;
    jobMsg.textContent = '';
    pollTimer = setInterval(poll, 700);
    poll();
  });
}

/* ---------- device (offline) helpers ---------- */
function deviceMarkRow(row, pages) {
  const btn = row.querySelector('.act-device');
  if (!btn) return;
  btn.textContent = 'On ✓';
  btn.disabled = true;
  btn.title = `Saved on this device (${pages} pages)`;
}

async function refreshDeviceNote() {
  if (!navigator.onLine) return; // the page itself may be offline-cached
  const list = await NS.deviceList(SID).catch(() => []);
  if (deviceNote) {
    deviceNote.textContent = list.length
      ? `${list.length} episode(s) already on this device for offline reading`
      : 'Nothing saved on this device yet';
  }
}

async function deviceSaveEp(row, epNo) {
  const btn = row.querySelector('.act-device');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  let data;
  try { data = await fetch(`/api/ep/${SID}/${epNo}/images`).then(r => r.json()); }
  catch (e) { data = { error: 'offline' }; }

  if (data.error) {
    // Even if the PC can't serve it, a previously made device copy is fine.
    const rec = await NS.deviceGet(SID, epNo).catch(() => null);
    if (rec) { deviceMarkRow(row, rec.images.length); return true; }
    alert(data.message || 'Could not reach the PC archive and no device copy exists.');
    if (btn) { btn.disabled = false; btn.textContent = '📱 Device'; }
    return false;
  }

  jobbox.hidden = false;
  jobDesc.textContent = `Saving Ep ${epNo} to this device…`;
  jobMsg.textContent = '';
  try {
    const pages = await NS.saveToDevice(SID, epNo, data.images,
      (d, t) => {
        jobCount.textContent = `${d}/${t} pages`;
        jobBar.style.width = Math.round(100 * d / t) + '%';
      });
    jobDesc.textContent = `Ep ${epNo} saved on this device`;
    jobCount.textContent = `${pages} pages`;
    jobBar.style.width = '100%';
    deviceMarkRow(row, pages);
    refreshDeviceNote();
    return true;
  } catch (e) {
    jobMsg.textContent = String(e);
    if (btn) { btn.disabled = false; btn.textContent = '📱 Device'; }
    return false;
  }
}

document.getElementById('btn-device-range').addEventListener('click', async () => {
  const from = prompt('Save to device from which episode?', '1');
  if (!from) return;
  const to = prompt(`Save through which episode? (1–${Math.max(MAXNO, 20)})`, String(Math.min(MAXNO || 20, (+from) + 19)));
  if (!to || (+to) < (+from)) return;
  document.querySelectorAll(`.eprow`).forEach(async (row) => {
    const no = +row.dataset.no;
    if (no < +from || no > +to) return;
    const rec = await NS.deviceHas(SID, no).catch(() => false);
    if (!rec) await deviceSaveEp(row, no);
  });
});

/* ---------- server buttons ---------- */
document.getElementById('btn-refresh').addEventListener('click', (e) => {
  e.target.disabled = true;
  startJob(`/api/series/${SID}/refresh`, {});
  /* startJob alerts and polls; re-enable the button when the page reloads. */
});

document.getElementById('btn-savefree').addEventListener('click', () => {
  /* full=1 makes the server recrawl the whole episode list first, so this
     really is "download the entire series", not just what's already stored. */
  startJob('/api/download', { series_id: SID, start: 1, end: 0, full: 1 });
});

document.getElementById('btn-check').addEventListener('click', () => {
  const end = prompt(`Check episodes up to which number? (known max: ${MAXNO})`, MAXNO || 20);
  if (!end) return;
  startJob(`/api/series/${SID}/check`, { start: 1, end: end });
});

/* ---------- bulk export (CBZ / folders) ---------- */
let exportTimer = null;
let exportJob = null;
let exportStarting = false;
function startExport(fmt) {
  if (exportTimer || exportStarting) return;
  exportStarting = true;
  startJobExport(fmt);
}

function startJobExport(fmt) {
  post('/api/export', { series_id: SID, format: fmt }).then(res => {
    exportStarting = false;
    if (res.error) { alert(res.error); return; }
    exportJob = res.job;
    jobbox.hidden = false;
    jobDesc.textContent = `Exporting series as ${fmt === 'cbz' ? 'CBZ' : 'image folders'}`;
    jobMsg.textContent = 'Building archive…';
    jobBar.style.width = '0%';
    jobCount.textContent = '';
    exportTimer = setInterval(pollExport, 600);
    pollExport();
  }).catch(() => { exportStarting = false; alert('Could not start export.'); });
}

function pollExport() {
  if (!exportJob) { clearInterval(exportTimer); exportTimer = null; return; }
  fetch(`/api/job/${exportJob}`).then(r => r.json()).then(j => {
    if (!j || !j.id) { clearInterval(exportTimer); exportTimer = null; return; }
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    jobCount.textContent = j.total ? `${j.done}/${j.total} (${pct}%)` : '';
    jobBar.style.width = pct + '%';
    if (j.message) jobMsg.textContent = j.message;
    if (j.status === 'done' || j.status === 'error') {
      clearInterval(exportTimer); exportTimer = null; exportJob = null;
      if (j.status === 'done' && j.url) {
        jobMsg.textContent = `${j.message}\nDownloading ${j.filename}…`;
        const a = document.createElement('a');
        a.href = j.url; a.download = j.filename || '';
        document.body.appendChild(a); a.click(); a.remove();
      } else if (j.status === 'error') {
        jobMsg.textContent = j.message || 'Export failed.';
      }
    }
  }).catch(() => { clearInterval(exportTimer); exportTimer = null; exportJob = null; });
}

document.getElementById('btn-export-cbz').addEventListener('click', () => startExport('cbz'));
document.getElementById('btn-export-raw').addEventListener('click', () => startExport('raw'));

document.getElementById('btn-manual').addEventListener('click', () => {
  const f = document.getElementById('manual-form');
  post(`/api/series/${SID}/manual_add`, {
    start: f.start.value, end: f.end.value, slug: f.slug.value
  }).then(res => {
    if (res.error) { alert(res.error); return; }
    alert(`Created/updated ${res.added} episode entries. Use "Check availability" and then "Save".`);
    location.reload();
  });
});

/* ---------- boot + per-row buttons ---------- */
async function init() {
  document.querySelectorAll('.eprow').forEach(row => {
    const no = +row.dataset.no;
    const readBtn = row.querySelector('.act-read');
    const dlBtn = row.querySelector('.act-dl');
    const devBtn = row.querySelector('.act-device');
    if (readBtn) readBtn.addEventListener('click', () => window.location = `/reader/${SID}/${no}`);
    if (dlBtn) dlBtn.addEventListener('click', () => {
      dlBtn.disabled = true; dlBtn.textContent = '…';
      startJob('/api/download', { series_id: SID, start: no, end: no });
    });
    if (devBtn) devBtn.addEventListener('click', () => deviceSaveEp(row, no));
  });
  refreshDeviceNote();
  try {
    const onDev = await NS.deviceList(SID);
    for (const r of onDev) {
      const row = document.querySelector(`.eprow[data-no="${r.epNo}"]`);
      if (row) deviceMarkRow(row, r.images.length);
    }
  } catch (e) { /* IndexedDB unavailable (e.g. private mode) */ }
}

if (document.readyState === 'loading') {
  addEventListener('DOMContentLoaded', init);
} else {
  init();
}
