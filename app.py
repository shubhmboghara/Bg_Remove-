"""Single-workspace background-removal service with a durable SQLite queue."""
import asyncio
import contextlib
import hashlib
import hmac
import io
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.background import BackgroundTask

ROOT = Path(__file__).parent
DATA = Path(os.getenv('DATA_DIR', str(ROOT / '.data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
os.environ.setdefault('U2NET_HOME', str(DATA / 'models'))
os.environ.setdefault('OMP_NUM_THREADS', '2')
MODEL = os.getenv('REMBG_MODEL', 'isnet-general-use')
MAX_FILES = 10_000
MAX_BYTES = 25 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000
TOKEN = os.getenv('APP_ACCESS_TOKEN', '')
stop = threading.Event()
session = None
engine = {'state': 'idle', 'model': MODEL, 'error': None}


def db():
    conn = sqlite3.connect(DATA / 'queue.sqlite3', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def initialize():
    with contextlib.closing(db()) as c, c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS batches (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'draft',
          created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS images (
          id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
          name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
          error TEXT, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS images_batch ON images(batch_id, state);
        ''')
        c.execute("UPDATE images SET state='queued' WHERE state='processing'")


def remove_background(source, target):
    global session
    from rembg import new_session, remove
    if session is None:
        engine.update(state='loading', error=None)
        session = new_session(MODEL, providers=['CPUExecutionProvider'])
    engine.update(state='ready', error=None)
    with Image.open(source) as image:
        result = remove(image, session=session)
        result.save(target, format='PNG')


def worker():
    while not stop.is_set():
        with contextlib.closing(db()) as c, c:
            row = c.execute("SELECT i.* FROM images i JOIN batches b ON b.id=i.batch_id WHERE i.state='queued' AND b.state='running' ORDER BY i.created LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE images SET state='processing', error=NULL WHERE id=?", (row['id'],))
        if not row:
            stop.wait(.5)
            continue
        folder = DATA / row['batch_id']
        try:
            remove_background(folder / (row['id'] + '.input.png'), folder / (row['id'] + '.png'))
            state, error = 'done', None
        except Exception as exc:
            state, error = 'failed', 'Processing failed. Check the server log, then retry.'
            print(f"Image {row['id']} failed: {exc}", flush=True)
            if session is None:
                engine.update(state='error', error='Model could not load. Check model download and server logs.')
                # A missing model should not fail thousands of queued images.
                with contextlib.closing(db()) as c, c:
                    c.execute("UPDATE batches SET state='paused' WHERE id=?", (row['batch_id'],))
        with contextlib.closing(db()) as c, c:
            c.execute('UPDATE images SET state=?, error=? WHERE id=?', (state, error, row['id']))
            remaining = c.execute("SELECT count(*) FROM images WHERE batch_id=? AND state IN ('queued','processing')", (row['batch_id'],)).fetchone()[0]
            if remaining == 0:
                c.execute("UPDATE batches SET state='finished' WHERE id=? AND state='running'", (row['batch_id'],))


@asynccontextmanager
async def lifespan(app):
    import fcntl
    lock = open(DATA / 'worker.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('This data directory already has a worker. Run one server process only.')
    initialize()
    stop.clear()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=30)
    lock.close()


app = FastAPI(title='Clearcut', lifespan=lifespan)


@app.middleware('http')
async def authentication(request: Request, call_next):
    if request.url.path.endswith('/images') and request.method == 'POST':
        try:
            if int(request.headers.get('content-length', '0')) > MAX_BYTES + 1024 * 1024:
                return JSONResponse({'detail': 'Each image must be 25 MB or smaller'}, status_code=413)
        except ValueError:
            return JSONResponse({'detail': 'Invalid content length'}, status_code=400)
    if TOKEN and request.url.path.startswith('/api/') and request.url.path != '/api/login':
        supplied = request.cookies.get('clearcut_access', '')
        expected = hashlib.sha256(TOKEN.encode()).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({'detail': 'Workspace access key required.'}, status_code=401)
    # Reject browser requests from other origins, even for local deployments.
    origin = request.headers.get('origin')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin != str(request.base_url).rstrip('/'):
        return JSONResponse({'detail': 'Cross-origin request rejected.'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


@app.post('/api/login')
async def login(request: Request):
    body = await request.json()
    if not TOKEN or not hmac.compare_digest(str(body.get('token', '')), TOKEN):
        raise HTTPException(401, 'Invalid access key')
    response = JSONResponse({'ok': True})
    response.set_cookie('clearcut_access', hashlib.sha256(TOKEN.encode()).hexdigest(), httponly=True, samesite='strict', secure=request.url.scheme == 'https')
    return response


def batch_or_404(c, batch_id):
    row = c.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Batch not found')
    return dict(row)


def summary(c, row):
    result = dict(row)
    result['counts'] = {key: 0 for key in ('queued', 'processing', 'done', 'failed')}
    for item in c.execute('SELECT state,count(*) AS n FROM images WHERE batch_id=? GROUP BY state', (row['id'],)):
        result['counts'][item['state']] = item['n']
    result['total'] = sum(result['counts'].values())
    return result


@app.get('/api/status')
def status():
    import shutil
    return {'engine': engine, 'max_files': MAX_FILES, 'max_file_mb': 25, 'free_bytes': shutil.disk_usage(DATA).free}


@app.get('/api/batches')
def batches():
    with contextlib.closing(db()) as c:
        return [summary(c, r) for r in c.execute('SELECT * FROM batches ORDER BY created DESC LIMIT 100')]


@app.post('/api/batches', status_code=201)
async def create_batch(request: Request):
    body = await request.json()
    name = str(body.get('name', 'Untitled batch')).strip()[:100] or 'Untitled batch'
    batch_id = uuid.uuid4().hex
    with contextlib.closing(db()) as c, c:
        c.execute('INSERT INTO batches(id,name,created) VALUES (?,?,?)', (batch_id, name, time.time()))
    (DATA / batch_id).mkdir()
    return {'id': batch_id, 'name': name}


@app.get('/api/batches/{batch_id}')
def get_batch(batch_id: str, offset: int = 0, limit: int = 60):
    with contextlib.closing(db()) as c:
        result = summary(c, batch_or_404(c, batch_id))
        result['images'] = [dict(r) for r in c.execute('SELECT id,name,state,error FROM images WHERE batch_id=? ORDER BY created LIMIT ? OFFSET ?', (batch_id, max(1, min(limit, 100)), max(0, offset)))]
        return result


@app.post('/api/batches/{batch_id}/images', status_code=201)
def upload(batch_id: str, file: UploadFile):
    import shutil
    with contextlib.closing(db()) as c:
        batch_or_404(c, batch_id)
    content = file.file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(413, 'Each image must be 25 MB or smaller')
    if shutil.disk_usage(DATA).free < max(512 * 1024 * 1024, len(content) * 5):
        raise HTTPException(507, 'Storage is nearly full. Free disk space before uploading.')
    image_id = uuid.uuid4().hex
    path = DATA / batch_id / (image_id + '.input.png')
    try:
        with Image.open(io.BytesIO(content)) as img:
            if img.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Unsupported format')
            if img.width * img.height > 25_000_000:
                raise ValueError('Image exceeds 25 megapixels')
            ImageOps.exif_transpose(img).convert('RGBA').save(path, format='PNG')
    except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError):
        path.unlink(missing_ok=True)
        raise HTTPException(400, 'Use a valid JPG, PNG or WebP image up to 25 megapixels')
    try:
        with contextlib.closing(db()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            batch = batch_or_404(c, batch_id)
            if batch['state'] != 'draft':
                raise HTTPException(409, 'Only draft batches accept uploads')
            count = c.execute('SELECT count(*) FROM images WHERE batch_id=?', (batch_id,)).fetchone()[0]
            if count >= MAX_FILES:
                raise HTTPException(409, 'Batch limit is 10,000 images')
            c.execute('INSERT INTO images(id,batch_id,name,created) VALUES(?,?,?,?)', (image_id, batch_id, Path((file.filename or 'image').replace('\\', '/')).name[:200], time.time()))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {'id': image_id}


@app.post('/api/batches/{batch_id}/{action}')
def action(batch_id: str, action: str):
    if action not in ('start', 'pause', 'retry'):
        raise HTTPException(404, 'Unknown action')
    with contextlib.closing(db()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        batch_or_404(c, batch_id)
        if action == 'retry':
            c.execute("UPDATE images SET state='queued',error=NULL WHERE batch_id=? AND state='failed'", (batch_id,))
        pending = c.execute("SELECT count(*) FROM images WHERE batch_id=? AND state IN ('queued','processing')", (batch_id,)).fetchone()[0]
        if action != 'pause' and pending == 0:
            raise HTTPException(409, 'No pending images in this batch')
        c.execute('UPDATE batches SET state=? WHERE id=?', ('paused' if action == 'pause' else 'running', batch_id))
    return {'ok': True}


@app.delete('/api/batches/{batch_id}')
def delete_batch(batch_id: str):
    import shutil
    with contextlib.closing(db()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        batch = batch_or_404(c, batch_id)
        active = c.execute("SELECT count(*) FROM images WHERE batch_id=? AND state='processing'", (batch_id,)).fetchone()[0]
        if batch['state'] == 'running' or active:
            raise HTTPException(409, 'Pause the batch and wait for the active image before deleting')
        c.execute('DELETE FROM images WHERE batch_id=?', (batch_id,))
        c.execute('DELETE FROM batches WHERE id=?', (batch_id,))
    shutil.rmtree(DATA / batch_id, ignore_errors=True)
    return {'ok': True}


@app.get('/api/images/{image_id}/{kind}')
def image_file(image_id: str, kind: str, download: bool = False):
    if kind not in ('original', 'result', 'thumb'):
        raise HTTPException(404, 'Unknown image type')
    with contextlib.closing(db()) as c:
        row = c.execute('SELECT * FROM images WHERE id=?', (image_id,)).fetchone()
    if row is None or (kind == 'result' and row['state'] != 'done'):
        raise HTTPException(404, 'Image unavailable')
    suffix = '.png' if kind == 'result' or (kind == 'thumb' and row['state'] == 'done') else '.input.png'
    path = DATA / row['batch_id'] / (image_id + suffix)
    if kind == 'thumb':
        with Image.open(path) as img:
            img.thumbnail((360, 300))
            buf = io.BytesIO()
            img.save(buf, 'PNG')
        return StreamingResponse(iter([buf.getvalue()]), media_type='image/png')
    return FileResponse(path, media_type='image/png', filename=Path(row['name']).stem + '-clearcut.png' if download else None)


@app.get('/api/batches/{batch_id}/download/zip')
def download_zip(batch_id: str, part: int = 1):
    # Disk-backed archives avoid buffering thousands of full-resolution images in RAM.
    if part < 1:
        raise HTTPException(400, 'Part must be at least 1')
    with contextlib.closing(db()) as c:
        batch = batch_or_404(c, batch_id)
        if batch['state'] != 'finished':
            raise HTTPException(409, 'Finish processing before downloading ZIP parts')
        rows = c.execute("SELECT id,name FROM images WHERE batch_id=? AND state='done' ORDER BY created LIMIT 250 OFFSET ?", (batch_id, (part - 1) * 250)).fetchall()
    if not rows:
        raise HTTPException(404, 'No completed images in this part')
    import tempfile
    handle, archive = tempfile.mkstemp(suffix='.zip', dir=DATA)
    os.close(handle)
    try:
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as z:
            for i, row in enumerate(rows):
                z.write(DATA / batch_id / (row['id'] + '.png'), f"{(part-1)*250+i+1:05d}-{Path(row['name']).stem}.png")
    except Exception:
        Path(archive).unlink(missing_ok=True)
        raise
    return FileResponse(archive, media_type='application/zip', filename=f'clearcut-{part:02d}.zip', background=BackgroundTask(Path(archive).unlink, missing_ok=True))


app.mount('/', StaticFiles(directory=ROOT / 'static', html=True), name='website')
