import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'DATA', tmp_path)
    monkeypatch.setattr(app, 'TOKEN', '')
    monkeypatch.setattr(app, 'detail_available', lambda: True)
    def fake_remove(source, target, quality='detailed'):
        with Image.open(source) as img:
            img.putalpha(100)
            img.save(target)
    monkeypatch.setattr(app, 'remove_background', fake_remove)
    with TestClient(app.app) as c:
        yield c


def image_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (24, 24), 'red').save(buf, 'PNG')
    return buf.getvalue()


def batch(client):
    r = client.post('/api/batches', json={'name': 'Products'})
    assert r.status_code == 201
    return r.json()['id']


def wait_finished(client, bid):
    for _ in range(100):
        result = client.get(f'/api/batches/{bid}').json()
        if result['state'] == 'finished':
            return result
        time.sleep(.03)
    pytest.fail('Worker did not finish')


def test_batch_workflow_and_zip_duplicate_names(client):
    bid = batch(client)
    for _ in range(3):
        r = client.post(f'/api/batches/{bid}/images', files={'file': ('same.png', image_bytes(), 'image/png')})
        assert r.status_code == 201
    assert client.get(f'/api/batches/{bid}/download/zip').status_code == 409
    assert client.post(f'/api/batches/{bid}/start').status_code == 200
    result = wait_finished(client, bid)
    assert result['counts'] == {'done': 3, 'failed': 0, 'queued': 0, 'processing': 0}
    image_id = result['images'][0]['id']
    png = client.get(f'/api/images/{image_id}/result?download=true')
    assert Image.open(io.BytesIO(png.content)).mode == 'RGBA'
    assert client.get(f'/api/images/{image_id}/thumb').status_code == 200
    archive = client.get(f'/api/batches/{bid}/download/zip')
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert len(z.namelist()) == len(set(z.namelist())) == 3
        assert z.testzip() is None
    assert not list(app.DATA.glob('*.zip'))
    assert client.get(f'/api/batches/{bid}/download/zip?part=2').status_code == 404
    assert client.post(f'/api/batches/{bid}/images', files={'file': ('x.png', image_bytes())}).status_code == 409


def test_invalid_uploads_and_limits(client, monkeypatch):
    bid = batch(client)
    assert client.post(f'/api/batches/{bid}/start').status_code == 409
    assert client.post(f'/api/batches/{bid}/images', files={'file': ('x.png', b'not an image')}).status_code == 400
    monkeypatch.setattr(app, 'MAX_FILES', 1)
    assert client.post(f'/api/batches/{bid}/images', files={'file': ('../../x.png', image_bytes())}).status_code == 201
    assert client.post(f'/api/batches/{bid}/images', files={'file': ('x.png', image_bytes())}).status_code == 409
    result = client.get(f'/api/batches/{bid}').json()
    assert result['total'] == 1 and result['images'][0]['name'] == 'x.png'
    assert len(list((app.DATA / bid).glob('*.input.png'))) == 1
    assert client.get('/api/batches/not-a-batch').status_code == 404


def test_retry_failed_image(client, monkeypatch):
    bid = batch(client)
    client.post(f'/api/batches/{bid}/images', files={'file': ('x.png', image_bytes())})
    original = app.remove_background
    monkeypatch.setattr(app, 'session', object())
    def fail(*args):
        raise RuntimeError('test failure')
    monkeypatch.setattr(app, 'remove_background', fail)
    client.post(f'/api/batches/{bid}/start')
    assert wait_finished(client, bid)['counts']['failed'] == 1
    monkeypatch.setattr(app, 'remove_background', original)
    assert client.post(f'/api/batches/{bid}/retry').status_code == 200
    assert wait_finished(client, bid)['counts']['done'] == 1


def test_auth_and_origin(client, monkeypatch):
    monkeypatch.setattr(app, 'TOKEN', 'test-only-key')
    assert client.get('/api/batches').status_code == 401
    assert client.post('/api/login', json={'token': 'wrong'}).status_code == 401
    assert client.post('/api/login', json={'token': 'test-only-key'}).status_code == 200
    assert client.get('/api/batches').status_code == 200
    assert client.post('/api/batches', json={}, headers={'origin': 'https://foreign.example'}).status_code == 403


def test_restart_recovers_inflight_work(client):
    bid = batch(client)
    result = client.post(f'/api/batches/{bid}/images', files={'file': ('x.png', image_bytes())})
    with app.db() as c:
        c.execute("UPDATE images SET state='processing' WHERE id=?", (result.json()['id'],))
    app.initialize()
    assert client.get(f'/api/batches/{bid}').json()['counts']['queued'] == 1


def test_model_failure_pauses_batch(client, monkeypatch):
    bid = batch(client)
    for _ in range(2):
        client.post(f'/api/batches/{bid}/images', files={'file': ('x.png', image_bytes())})
    monkeypatch.setattr(app, 'session', None)
    def fail(*args):
        raise RuntimeError('model unavailable')
    monkeypatch.setattr(app, 'remove_background', fail)
    client.post(f'/api/batches/{bid}/start')
    for _ in range(100):
        state = client.get(f'/api/batches/{bid}').json()
        if state['state'] == 'paused':
            break
        time.sleep(.03)
    assert state['state'] == 'paused'
    assert state['counts']['failed'] == 1
    assert state['counts']['queued'] == 1


def test_ten_thousand_queue_pagination_and_limit(client):
    bid = batch(client)
    with app.db() as c:
        c.executemany('INSERT INTO images(id,batch_id,name,created) VALUES(?,?,?,?)',
                      [(f'item{i:05d}', bid, f'image{i}.png', i) for i in range(10000)])
    result = client.get(f'/api/batches/{bid}?offset=9940&limit=60').json()
    assert result['total'] == result['counts']['queued'] == 10000
    assert len(result['images']) == 60
    assert result['images'][-1]['name'] == 'image9999.png'
    assert client.post(f'/api/batches/{bid}/images', files={'file': ('overflow.png', image_bytes())}).status_code == 409


def test_zip_parts_and_delete(client):
    bid = batch(client)
    with app.db() as c:
        c.execute("UPDATE batches SET state='finished' WHERE id=?", (bid,))
        for i in range(251):
            image_id = f'file{i:05d}'
            c.execute("INSERT INTO images(id,batch_id,name,state,created) VALUES(?,?,?,'done',?)", (image_id,bid,'same.png',i))
            (app.DATA / bid / (image_id+'.png')).write_bytes(image_bytes())
    for part, count in [(1,250),(2,1)]:
        response = client.get(f'/api/batches/{bid}/download/zip?part={part}')
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            assert len(z.namelist()) == count
    assert client.delete(f'/api/batches/{bid}').status_code == 200
    assert not (app.DATA / bid).exists()
    assert client.get(f'/api/batches/{bid}').status_code == 404


def test_quality_selection_persists(client):
    detailed = client.post('/api/batches', json={'name': 'Detailed'}).json()
    assert detailed['quality'] == 'detailed'
    fast = client.post('/api/batches', json={'quality': 'fast'}).json()
    assert client.get(f"/api/batches/{fast['id']}").json()['quality'] == 'fast'
    assert client.post('/api/batches', json={'quality': 'unknown'}).status_code == 400
    assert client.post('/api/batches', json={'quality': []}).status_code == 400


def test_detail_pipeline_preserves_alpha_and_switches_models(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'detail_available', lambda: True)
    import sys
    from types import SimpleNamespace
    created, calls = [], []
    def new_session(model, **kwargs):
        created.append(model)
        return model
    def remove(image, **kwargs):
        assert image.mode == 'RGB'
        calls.append(kwargs)
        out = image.convert('RGBA')
        out.putalpha(200)
        return out
    monkeypatch.setitem(sys.modules, 'rembg', SimpleNamespace(new_session=new_session, remove=remove))
    monkeypatch.setattr(app, 'session', None)
    monkeypatch.setattr(app, 'session_model', None)
    original = Image.new('RGBA', (3, 1), (20, 40, 60, 255))
    original.putalpha(Image.frombytes('L', (3, 1), bytes([0, 100, 255])))
    source, target = tmp_path / 'source.png', tmp_path / 'result.png'
    original.save(source)
    app.remove_background(source, target, 'detailed')
    with Image.open(target) as result:
        assert list(result.getchannel('A').tobytes()) == [0, 100, 200]
        assert result.size == original.size
    app.remove_background(source, target, 'detailed')
    app.remove_background(source, target, 'fast')
    assert created == [app.DETAIL_MODEL, app.MODEL]
    assert calls[0]['decontaminate'] is True
    assert calls[0]['post_process_mask'] is False
    assert calls[-1]['decontaminate'] is False


def test_existing_batches_keep_fast_quality_during_migration(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(app, 'DATA', tmp_path)
    with sqlite3.connect(tmp_path / 'queue.sqlite3') as c:
        c.execute("CREATE TABLE batches(id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'draft', created REAL NOT NULL)")
        c.execute("INSERT INTO batches VALUES('old','Existing','draft',1)")
    app.initialize()
    app.initialize()
    with app.db() as c:
        assert c.execute("SELECT quality FROM batches WHERE id='old'").fetchone()[0] == 'fast'


def test_small_server_rejects_detail_and_defaults_to_fast(client, monkeypatch):
    monkeypatch.setattr(app, 'detail_available', lambda: False)
    assert client.get('/api/status').json()['detail_available'] is False
    assert client.post('/api/batches', json={'quality':'detailed'}).status_code == 422
    assert client.post('/api/batches', json={}).json()['quality'] == 'fast'
