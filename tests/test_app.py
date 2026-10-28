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
    def fake_remove(source, target):
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
