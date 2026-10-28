const $ = id => document.getElementById(id);
let selected = null, offset = 0, uploading = false, refreshing = false, currentImages = '', toastTimer;
const PAGE = 60;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6500); }
async function api(path, options = {}) {
  const response = await fetch('/api' + path, options);
  if (response.status === 401) { if (!$('loginDialog').open) $('loginDialog').showModal(); throw Error('Unlock your workspace to continue.'); }
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw Error(typeof body.detail === 'string' ? body.detail : 'Request failed. Please try again.'); }
  return response.json();
}
const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });
function element(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; }
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const batches = await api('/batches');
    $('batchCount').textContent = batches.length;
    $('emptyState').hidden = batches.length > 0;
    $('batchList').replaceChildren(...batches.map(batch => {
      const row = element('button', 'batch-row');
      row.append(element('span', 'batch-icon', '▤'));
      const text = element('div'); text.append(element('strong', '', batch.name), element('small', '', `${batch.total.toLocaleString()} images · ${batch.counts.done.toLocaleString()} ready${batch.counts.failed ? ` · ${batch.counts.failed} failed` : ''}`));
      row.append(text, element('span', 'batch-state', batch.state)); row.onclick = () => selectBatch(batch.id); return row;
    }));
    if (selected) await refreshDetail();
    const state = await api('/status');
    $('engineLabel').textContent = state.engine.state === 'loading' ? 'Loading AI model…' : state.engine.state === 'error' ? 'Model needs attention' : 'Local processing';
    if (state.engine.state === 'error') $('engineLabel').title = state.engine.error;
  } catch (err) { if (!$('loginDialog').open) toast(err.message); }
  finally { refreshing = false; }
}
async function selectBatch(id) { selected = id; offset = 0; currentImages = ''; $('uploadStatus').textContent = ''; await refreshDetail(); $('batchDetail').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
async function refreshDetail() {
  const requestId = selected;
  const batch = await api(`/batches/${requestId}?offset=${offset}&limit=${PAGE}`);
  if (requestId !== selected) return;
  $('batchDetail').hidden = false;
  $('detailTitle').textContent = batch.name;
  const c = batch.counts;
  $('detailStatus').textContent = `${c.done.toLocaleString()} of ${batch.total.toLocaleString()} ready · ${c.queued} queued · ${c.processing} processing · ${c.failed} failed · ${batch.state}`;
  $('progressFill').style.width = `${batch.total ? (c.done + c.failed) / batch.total * 100 : 0}%`;
  $('pauseButton').hidden = batch.state !== 'running';
  $('deleteButton').disabled = uploading || batch.state === 'running' || c.processing > 0;
  $('retryButton').hidden = c.failed === 0;
  $('startButton').hidden = batch.state === 'running' || batch.state === 'finished';
  $('startButton').textContent = batch.state === 'paused' ? 'Resume processing →' : 'Remove backgrounds →';
  $('startButton').disabled = uploading || batch.total === 0;
  $('retryButton').disabled = uploading;
  const parts = batch.state === 'finished' ? Math.ceil(c.done / 250) : 0;
  $('downloadLinks').replaceChildren(...Array.from({length: parts}, (_, i) => { const a = element('a', '', `↓ Download ${parts > 1 ? `ZIP part ${i+1} / ${parts}` : 'all results (.zip)'}`); a.href = `/api/batches/${batch.id}/download/zip?part=${i+1}`; return a; }));
  const signature = JSON.stringify(batch.images);
  if (signature !== currentImages) {
    currentImages = signature;
    $('imageGrid').replaceChildren(...batch.images.map(img => {
      const card = element('article', 'image-card'); const pic = element('img', 'checker'); pic.src = `/api/images/${img.id}/thumb?v=${img.state}`; pic.alt = img.name; pic.loading = 'lazy';
      const caption = element('div', 'caption'); caption.append(element('strong', '', img.name), element('small', img.state === 'failed' ? 'failed' : '', img.state === 'done' ? '✓ Background removed' : img.state));
      if (img.error) caption.title = img.error;
      if (img.state === 'done') { const a = element('a', '', 'Download PNG →'); a.href = `/api/images/${img.id}/result?download=true`; caption.append(a); }
      card.append(pic, caption); return card;
    }));
  }
  $('prevPage').disabled = offset === 0; $('nextPage').disabled = offset + PAGE >= batch.total;
  $('pageLabel').textContent = batch.total ? `${offset+1}–${Math.min(offset+PAGE,batch.total)} of ${batch.total}` : 'No images yet';
}
async function uploadFiles(fileList) {
  if (uploading) return toast('Please wait for the current upload to finish.');
  const all = Array.from(fileList);
  const files = all.filter(f => /\.(jpe?g|png|webp)$/i.test(f.name) && f.size <= 25 * 1024 * 1024);
  if (!files.length) return toast('Choose JPG, PNG or WebP images up to 25 MB each.');
  if (files.length > 10000) return toast('Choose up to 10,000 images per batch.');
  uploading = true; $('chooseFiles').disabled = true; $('chooseFolder').disabled = true;
  let uploaded = 0, failed = 0, next = 0, lastError = '';
  try {
    const batch = await post('/batches', { name: `Collection · ${new Date().toLocaleDateString(undefined, {month:'short',day:'numeric'})} · ${new Date().toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})}` });
    await selectBatch(batch.id);
    const uploadOne = async () => {
      while (next < files.length) {
        const file = files[next++]; const data = new FormData(); data.append('file', file);
        try { await api(`/batches/${batch.id}/images`, { method:'POST', body:data }); uploaded++; }
        catch (err) { failed++; lastError = err.message; }
        if (selected === batch.id) $('uploadStatus').textContent = `Uploading ${uploaded+failed} / ${files.length} · ${uploaded} uploaded${failed ? ` · ${failed} failed` : ''}. Keep this tab open until uploads finish.`;
      }
    };
    await Promise.all([uploadOne(), uploadOne(), uploadOne()]);
    if (selected === batch.id) $('uploadStatus').textContent = `${uploaded} uploaded${failed ? ` · ${failed} failed: ${lastError}` : ''}${all.length !== files.length ? ` · ${all.length-files.length} unsupported or oversized files skipped` : ''}. ${uploaded ? 'Ready to remove backgrounds.' : ''}`;
    toast(`${uploaded} images uploaded. ${failed ? `${failed} uploads failed.` : 'Your batch is ready.'}`);
  } catch (err) { toast(err.message); }
  finally { uploading = false; $('chooseFiles').disabled = false; $('chooseFolder').disabled = false; $('fileInput').value = ''; $('folderInput').value = ''; await refresh(); }
}
$('chooseFiles').onclick = () => $('fileInput').click(); $('chooseFolder').onclick = () => $('folderInput').click();
$('fileInput').onchange = event => uploadFiles(event.target.files); $('folderInput').onchange = event => uploadFiles(event.target.files);
for (const name of ['dragenter','dragover']) $('dropZone').addEventListener(name, e => { e.preventDefault(); $('dropZone').classList.add('drag'); });
for (const name of ['dragleave','drop']) $('dropZone').addEventListener(name, e => { e.preventDefault(); $('dropZone').classList.remove('drag'); });
$('dropZone').addEventListener('drop', e => uploadFiles(e.dataTransfer.files));
window.addEventListener('dragover', e => e.preventDefault()); window.addEventListener('drop', e => e.preventDefault());
window.addEventListener('beforeunload', e => { if (uploading) { e.preventDefault(); e.returnValue = ''; } });
for (const [id, action] of [['startButton','start'],['pauseButton','pause'],['retryButton','retry']]) $(id).onclick = async () => { try { await post(`/batches/${selected}/${action}`); await refresh(); } catch(err) { toast(err.message); } };
$('prevPage').onclick = async () => { offset = Math.max(0,offset-PAGE); await refreshDetail(); };
$('nextPage').onclick = async () => { offset += PAGE; await refreshDetail(); };
$('compareSlider').oninput = e => { $('afterLayer').style.clipPath = `inset(0 0 0 ${e.target.value}%)`; $('compareLine').style.left = e.target.value+'%'; };
$('batchesNav').onclick = () => $('batchSection').scrollIntoView({behavior:'smooth'});
$('studioNav').onclick = () => window.scrollTo({top:0,behavior:'smooth'});
$('helpButton').onclick = () => $('helpDialog').showModal(); $('closeHelp').onclick = () => $('helpDialog').close();
$('loginForm').onsubmit = async e => { e.preventDefault(); try { await post('/login',{token:$('accessKey').value}); $('accessKey').value = ''; $('loginDialog').close(); await refresh(); } catch(err) { $('loginError').textContent = err.message; } };
refresh(); setInterval(refresh, 2500);

$('deleteButton').onclick = async () => { if (!confirm('Permanently delete this batch and all its images?')) return; try { await api(`/batches/${selected}`, {method:'DELETE'}); selected = null; $('batchDetail').hidden = true; await refresh(); toast('Batch deleted.'); } catch (err) { toast(err.message); } };
