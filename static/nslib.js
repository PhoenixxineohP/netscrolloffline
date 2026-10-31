/* Shared client-side offline helpers:
   - IndexedDB records of which episodes are saved on THIS device
   - saving episode images into Cache Storage (works offline via the service worker)
   - queueing reading progress while offline, syncing when back online
*/
const NS_CACHE_NAME = 'nso-media';

const NS = (() => {
  let dbPromise = null;

  function open() {
    if (!dbPromise) {
      dbPromise = new Promise((res, rej) => {
        const rq = indexedDB.open('netscroll', 1);
        rq.onupgradeneeded = () => {
          const db = rq.result;
          if (!db.objectStoreNames.contains('device_eps'))
            db.createObjectStore('device_eps', { keyPath: ['seriesId', 'epNo'] });
          if (!db.objectStoreNames.contains('pending_progress'))
            db.createObjectStore('pending_progress', { keyPath: 'idx', autoIncrement: true });
        };
        rq.onsuccess = () => res(rq.result);
        rq.onerror = () => rej(rq.error);
      });
    }
    return dbPromise;
  }

  async function call(store, mode, fn) {
    const db = await open();
    return new Promise((res, rej) => {
      const t = db.transaction(store, mode);
      const rq = fn(t.objectStore(store));
      rq.onsuccess = () => res(rq.result);
      rq.onerror = () => rej(rq.error);
    });
  }

  return {
    deviceHas: async (sid, ep) => !!(await call('device_eps', 'readonly', s => s.get([sid, ep]))),
    deviceGet: async (sid, ep) => (await call('device_eps', 'readonly', s => s.get([sid, ep]))),
    devicePut: (rec) => call('device_eps', 'readwrite', s => s.put(rec)),
    deviceDelete: (sid, ep) => call('device_eps', 'readwrite', s => s.delete([sid, ep])),
    deviceList: async (sid) => {
      const all = await call('device_eps', 'readonly', s => s.getAll());
      return (sid == null) ? all : all.filter(r => r.seriesId === sid);
    },

    /* Download an episode's pages into THIS device (Cache Storage + IDB record). */
    saveToDevice: async function (sid, ep, urls, onProgress) {
      const cache = await caches.open(NS_CACHE_NAME);
      let done = 0;
      for (const u of urls) {
        const hit = await cache.match(u);
        if (!hit) {
          const rr = await fetch(u, { cache: 'reload' });
          if (!rr.ok) throw new Error('page fetch failed: ' + u);
          await cache.put(u, rr);
        }
        done += 1;
        if (onProgress && done % 5 === 0) onProgress(done, urls.length);
      }
      await NS.devicePut({ seriesId: sid, epNo: ep, images: urls, savedAt: Date.now() });
      if (onProgress) onProgress(urls.length, urls.length);
      return urls.length;
    },

    /* Progress: send to the PC archive if possible, else queue for later. */
    saveProgress: async function (p) {
      try {
        const resp = await fetch('/api/progress', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(p), keepalive: true,
        });
        if (resp.ok) return true;
      } catch (e) { /* offline */ }
      try {
        await call('pending_progress', 'readwrite', s => s.add({ ...p, at: Date.now() }));
      } catch (e) { /* idb unavailable */ }
      return false;
    },

    flushPending: async function () {
      let rows;
      try { rows = await call('pending_progress', 'readonly', s => s.getAll()); }
      catch (e) { return; }
      if (!rows || !rows.length) return;
      const left = [];
      for (const r of rows) {
        try {
          const resp = await fetch('/api/progress', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ series_id: r.series_id, episode_no: r.episode_no,
                                   scroll_frac: r.scroll_frac, scroll_px: r.scroll_px }),
          });
          if (!resp.ok) left.push(r);
        } catch (e) { left.push(r); }
      }
      try {
        await call('pending_progress', 'readwrite', s => {
          s.clear();
          for (const r of left) s.add(r);
        });
      } catch (e) { /* ignore */ }
    },
  };
})();

addEventListener('online', () => NS.flushPending().catch(() => {}));
if (document.readyState === 'loading') {
  addEventListener('DOMContentLoaded', () => NS.flushPending().catch(() => {}));
} else {
  NS.flushPending().catch(() => {});
}
