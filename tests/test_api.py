from fastapi.testclient import TestClient
from app.main import app


def test_health():
    with TestClient(app) as client:
        r=client.get('/api/health'); assert r.status_code==200; assert r.json()['ok'] is True


def test_dashboard():
    with TestClient(app) as client:
        r=client.get('/api/dashboard'); assert r.status_code==200; assert r.json()['payments_analyzed'] >= 5000


def test_payment_review_and_safe_execute():
    with TestClient(app) as client:
        rows=client.get('/api/payments?limit=50&status=failed').json(); assert rows
        pid=rows[0]['id']; review=client.post(f'/api/recovery/{pid}/review'); assert review.status_code==200
        out=client.post(f'/api/recovery/{pid}/execute'); assert out.status_code==200
        assert 'status' in out.json()


def test_strategy_lab_and_insights():
    with TestClient(app) as client:
        r=client.get('/api/strategy-lab'); assert r.status_code==200; assert 'incremental_revenue' in r.json()
        r=client.get('/api/insights'); assert r.status_code==200; assert r.json()['root_causes']
