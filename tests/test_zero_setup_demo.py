from fastapi.testclient import TestClient
from tests.test_portal import login, csrf


def test_zero_setup_demo_ignores_dotenv_and_never_connects_to_mysql(monkeypatch):
    import app
    import mysql.connector
    for key in ('APP_ENV','APP_MODE','SESSION_SECRET','DEMO_LOGIN_ENABLED','DB_HOST','DB_USER','DB_PASSWORD','DB_NAME'):
        monkeypatch.delenv(key,raising=False)
    def forbidden(*args,**kwargs):
        raise AssertionError('Zero-setup demo must not load .env or connect to MySQL')
    monkeypatch.setattr(app,'load_dotenv',forbidden)
    monkeypatch.setattr(mysql.connector,'connect',forbidden)
    application=app.create_app()
    assert application.state.settings.mode=='demo'
    with TestClient(application) as client:
        assert login(client,'whatever','123').status_code==303
        home=client.get('/')
        assert home.status_code==200
        assert '<h2 id="welcome-title">歡迎回來</h2>' in home.text
        assert 'data-document-count="5"' in home.text
        assert client.get('/announcements').status_code==200
        assert client.get('/assistant').status_code==200
        listing=client.get('/documents')
        assert '精密塗布設備開機與清潔 SOP' in listing.text
        assert '研發樣品管理與測試紀錄 SOP' not in listing.text
        assert client.get('/documents/6').status_code==404
        assert client.get('/documents/8').status_code==404
        assert client.get('/documents/4').status_code==200
        assert client.post('/logout',data={'csrf_token':csrf(client.get('/'))},follow_redirects=False).status_code==303
        assert client.get('/',follow_redirects=False).status_code==303


def test_demo_generates_independent_secrets_and_data(monkeypatch):
    from app import create_app
    monkeypatch.delenv('APP_ENV',raising=False)
    monkeypatch.delenv('APP_MODE',raising=False)
    first,second=create_app(),create_app()
    assert first.state.settings.session_secret!=second.state.settings.session_secret
    with TestClient(first),TestClient(second):
        first.state.db.execute('UPDATE documents SET title=%s WHERE id=4',('changed only here',))
        assert second.state.db.query('SELECT title FROM documents WHERE id=4',one=True)['title']=='精密塗布設備開機與清潔 SOP'


def test_production_does_not_silently_fall_back_to_demo(monkeypatch):
    import pytest
    from app import create_app
    monkeypatch.setenv('APP_ENV','production')
    monkeypatch.setenv('APP_MODE','demo')
    with pytest.raises(ValueError,match='demo'):
        create_app()
