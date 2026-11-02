/* Library page: browse another WEBTOON language edition's catalogue and add a title. */
(function () {
  const box = document.getElementById('browsebox');
  if (!box) return;

  const langSel = document.getElementById('br-lang');
  const genreSel = document.getElementById('br-genre');
  const goBtn = document.getElementById('br-go');
  const status = document.getElementById('br-status');
  const results = document.getElementById('br-results');

  function post(url, body) {
    const form = new FormData();
    for (const [k, v] of Object.entries(body || {})) form.append(k, v);
    return fetch(url, { method: 'POST', body: form }).then(r => r.json());
  }

  function addTitle(titleNo, button) {
    button.disabled = true;
    button.textContent = 'Adding…';
    post('/api/sources/add', { title_no: titleNo }).then(res => {
      if (!res || res.error) {
        status.textContent = (res && res.error) || 'Could not resolve that title.';
        button.disabled = false;
        button.textContent = 'Add';
        return;
      }
      const form = new FormData();
      form.append('url', res.url);
      // Use a real navigation so the normal add flow (and its redirect) runs.
      const f = document.createElement('form');
      f.method = 'post';
      f.action = '/api/series/add';
      f.appendChild(form);
      f.submit();
    }).catch(() => {
      status.textContent = 'Could not resolve that title.';
      button.disabled = false;
      button.textContent = 'Add';
    });
  }

  goBtn.addEventListener('click', () => {
    const lang = langSel.value;
    const genre = genreSel.value;
    goBtn.disabled = true;
    status.textContent = 'Loading…';
    results.innerHTML = '';
    fetch(`/api/sources/catalog?language=${encodeURIComponent(lang)}&genre=${encodeURIComponent(genre)}`)
      .then(r => r.json())
      .then(res => {
        goBtn.disabled = false;
        if (!res || res.error) {
          status.textContent = (res && res.error) || 'Could not load that catalogue.';
          return;
        }
        const titles = res.titles || [];
        status.textContent = titles.length
          ? `${titles.length} title(s) in ${lang}/${genre}`
          : 'No titles found for that edition/genre.';
        for (const t of titles) {
          const row = document.createElement('div');
          row.className = 'br-row';
          const name = document.createElement('span');
          name.className = 'br-name';
          name.textContent = t.title;
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'mini primary';
          btn.textContent = 'Add';
          btn.addEventListener('click', () => addTitle(t.title_no, btn));
          row.appendChild(name);
          row.appendChild(btn);
          results.appendChild(row);
        }
      })
      .catch(() => {
        goBtn.disabled = false;
        status.textContent = 'Could not load that catalogue.';
      });
  });
})();
