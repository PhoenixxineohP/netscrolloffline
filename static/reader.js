/* Reader: vertical scroll strip; works offline for episodes saved on this device;
   restores & saves progress per profile (queues progress while offline). */
function readerMain() {
  const box = document.getElementById('reader');
  const strip = document.getElementById('strip');
  const errBox = document.getElementById('reader-error');
  const posInfo = document.getElementById('posinfo');
  const { seriesId, epNo, startPx, startFrac } = READER_DATA;

  function showError(msg) {
    strip.style.display = 'none';
    errBox.hidden = false;
    errBox.innerHTML = `<h3>Not readable from here</h3><p class="muted">${msg}</p>
      <p><a href="/series/${seriesId}">Back to episode list</a></p>`;
  }

  function build(urls) {
    strip.textContent = '';
    const imgs = [];
    urls.forEach((src) => {
      const img = document.createElement('img');
      img.loading = 'eager';
      img.src = src;
      img.draggable = false;
      img.alt = '';
      img.addEventListener('load', () => { if (imgs.every(i => i.complete)) applyStart(); });
      img.addEventListener('error', () => { img.style.minHeight = '120px'; img.style.background = '#333'; });
      strip.appendChild(img);
      imgs.push(img);
    });
    applyStart();
  }

  async function load() {
    try {
      const resp = await fetch(`/api/ep/${seriesId}/${epNo}/images`);
      const data = await resp.json();
      if (!data.error) { build(data.images); return; }
      if (data.message) throw new Error(data.message);
      throw new Error('unavailable');
    } catch (netErr) {
      // Server unreachable (offline) or episode refused: check for a device copy.
      try {
        const rec = await NS.deviceGet(seriesId, epNo);
        if (rec && rec.images && rec.images.length) { build(rec.images); return; }
      } catch (dbErr) { /* no IDB either */ }
      showError(String(netErr.message || netErr).includes('Failed to fetch')
        ? 'You appear to be offline and this episode is not saved on this device.'
        : (netErr.message || 'This episode is not available for offline reading.'));
    }
  }

  let startApplied = false;
  function applyStart() {
    if (startApplied || document.body.scrollHeight < 2000) return;
    startApplied = true;
    const max = document.body.scrollHeight - innerHeight;
    const target = startPx ? Math.min(startPx, Math.max(0, max))
                           : Math.max(0, Math.round(max * (startFrac || 0)));
    if (target > 0) scrollTo({ top: target, behavior: 'instant' });
  }

  load();

  /* progress saving — to the PC when online, queued on this device when not */
  let lastSave = 0;
  function save() {
    const now = Date.now();
    if (now - lastSave < 450) return;
    lastSave = now;
    const total = document.body.scrollHeight - innerHeight;
    const frac = total > 0 ? Math.min(1, scrollY / total) : 0;
    if (posInfo) posInfo.textContent = Math.round(frac * 100) + '%' +
      (frac >= 0.995 ? ' — finished ✓' : '');
    NS.saveProgress({
      series_id: seriesId, episode_no: epNo,
      scroll_frac: frac, scroll_px: Math.round(scrollY),
    }).catch(() => {});
  }
  addEventListener('scroll', save, { passive: true });
  addEventListener('beforeunload', () => {
    const total = document.body.scrollHeight - innerHeight;
    NS.saveProgress({
      series_id: seriesId, episode_no: epNo,
      scroll_frac: total > 0 ? scrollY / total : 0,
      scroll_px: Math.round(scrollY),
    }).catch(() => {});
  });
}

if (document.readyState === 'loading') {
  addEventListener('DOMContentLoaded', readerMain);
} else {
  readerMain();
}