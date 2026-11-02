/* Library page: select several series and queue whole-series downloads at once. */
(function () {
  const tools = document.getElementById('queuetools');
  if (!tools) return;

  const all = document.getElementById('qpick-all');
  const count = document.getElementById('qpick-count');
  const status = document.getElementById('qpick-status');
  const queueBtn = document.getElementById('qpick-queue');
  const picks = Array.from(document.querySelectorAll('.qpick'));

  function selected() {
    return picks.filter(p => p.checked).map(p => p.value);
  }

  function sync() {
    const ids = selected();
    const n = ids.length;
    count.textContent = n ? `${n} selected` : 'Nothing selected';
    queueBtn.disabled = n === 0;
    queueBtn.textContent = n ? `⧉ Queue ${n} series` : '⧉ Queue selected';
    if (all) {
      all.checked = n > 0 && n === picks.length;
      all.indeterminate = n > 0 && n < picks.length;
    }
  }

  picks.forEach(p => {
    p.addEventListener('change', () => {
      p.closest('.cardwrap').classList.toggle('picked', p.checked);
      sync();
    });
  });

  if (all) {
    all.addEventListener('change', () => {
      picks.forEach(p => {
        p.checked = all.checked;
        p.closest('.cardwrap').classList.toggle('picked', p.checked);
      });
      sync();
    });
  }

  queueBtn.addEventListener('click', () => {
    const ids = selected();
    if (!ids.length) return;
    queueBtn.disabled = true;
    status.textContent = 'Queueing…';
    const form = new FormData();
    form.append('series_ids', ids.join(','));
    fetch('/api/queue/bulk', { method: 'POST', body: form })
      .then(r => r.json())
      .then(res => {
        if (!res || res.error) {
          status.textContent = (res && res.error) || 'Could not queue.';
          queueBtn.disabled = false;
          return;
        }
        const bits = [`${res.queued || 0} queued`];
        if (res.skipped) bits.push(`${res.skipped} already in queue`);
        if (res.failed) bits.push(`${res.failed} failed`);
        status.innerHTML = bits.join(' · ') +
          ' — <a href="/queue">open the queue</a>';
        picks.forEach(p => {
          p.checked = false;
          p.closest('.cardwrap').classList.remove('picked');
        });
        sync();
      })
      .catch(() => {
        status.textContent = 'Could not queue.';
        queueBtn.disabled = false;
      });
  });

  sync();
})();
