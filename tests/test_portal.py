"""Contract tests: real app, SQL repositories and hashing; isolated SQLite boundary.

SQLite does not replace the opt-in MySQL migration/integration checks.
"""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, sql, params=()):
        sql = sql.replace('%s', '?').replace(' FOR UPDATE', '')
        sql = sql.replace('INSERT IGNORE', 'INSERT OR IGNORE')
        self.cursor.execute(sql, params)
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self):
        return self.cursor.rowcount

    @property
    def lastrowid(self):
        return self.cursor.lastrowid

    def close(self):
        self.cursor.close()


@pytest.fixture
def portal(tmp_path, monkeypatch):
    # Prevent the production app export from reading local .env secrets.
    monkeypatch.setenv('SESSION_SECRET', 'isolated-test-secret-' + 'x' * 40)
    monkeypatch.setenv('APP_ENV', 'test')
    from app import create_app
    from core.config import Settings
    from core.database import Database
    from core.security import hash_password

    path = tmp_path / 'portal.db'
    conn = sqlite3.connect(path)
    conn.executescript('''
    CREATE TABLE departments(id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, is_active INTEGER DEFAULT 1);
    CREATE TABLE users(id INTEGER PRIMARY KEY,employee_no TEXT UNIQUE,display_name TEXT,password_hash TEXT,
      department_id INTEGER,role TEXT,is_active INTEGER,session_version INTEGER DEFAULT 1);
    CREATE TABLE announcements(id INTEGER PRIMARY KEY,title TEXT,content TEXT,category TEXT,is_pinned INTEGER,
      status TEXT,published_at TEXT,created_by INTEGER,created_at TEXT,updated_at TEXT);
    CREATE TABLE documents(id INTEGER PRIMARY KEY,document_code TEXT UNIQUE,title TEXT,summary TEXT,content TEXT,
      category TEXT,department_id INTEGER,visibility TEXT,version TEXT,status TEXT,created_by INTEGER,created_at TEXT,updated_at TEXT);
    CREATE TABLE login_attempts(key_hash TEXT PRIMARY KEY,attempts INTEGER,window_started_at TEXT);
    ''')
    conn.executemany('INSERT INTO departments VALUES(?,?,?,1)', [(1,'MFG','製造部'),(2,'RD','研發部')])
    pw = hash_password('test-only-password')
    conn.executemany('INSERT INTO users VALUES(?,?,?,?,?,?,?,1)', [
        (1,'M001','林員工',pw,1,'employee',1), (2,'R001','研發員工',pw,2,'employee',1),
        (3,'A001','管理員',pw,1,'admin',1), (4,'M002','製造主管',pw,1,'manager',1),
        (5,'OFF','停用員工',pw,1,'employee',0)])
    now = '2026-01-01 00:00:00'
    conn.executemany('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', [
        (1,'GEN-001','公司規章','摘要','全公司內容','規章',None,'company','1.0','published',3,now,now),
        (2,'MFG-001','塗布 SOP','摘要','製造機密內容','SOP',1,'department','1.2','published',3,now,now),
        (3,'RD-001','研發 SOP','摘要','研發機密內容','SOP',2,'department','2.0','published',3,now,now),
        (4,'SEC-001','配方管制','摘要','限制內容','規章',2,'restricted','1.0','published',3,now,now),
        (5,'DRAFT-001','草稿文件','摘要','草稿內容','SOP',None,'company','0.1','draft',3,now,now),
        (6,'XSS-001','安全文字','摘要','<script>alert(1)</script>','訓練',None,'company','1.0','published',3,now,now)])
    conn.executemany('INSERT INTO announcements VALUES(?,?,?,?,?,?,?,?,?,?)', [
        (1,'教育訓練公告','請參加訓練','教育訓練',1,'published',now,3,now,now),
        (2,'未發布公告','草稿公告內容','行政',0,'draft',now,3,now,now),
        (3,'未來公告','未來公告內容','行政',0,'published','2099-01-01 00:00:00',3,now,now)])
    conn.commit()
    conn.close()

    class Connection:
        def __init__(self):
            self.conn = sqlite3.connect(path, timeout=10)
            self.conn.row_factory = sqlite3.Row
        def cursor(self, **kwargs):
            return Cursor(self.conn.cursor())
        def commit(self): self.conn.commit()
        def rollback(self): self.conn.rollback()
        def close(self): self.conn.close()

    db = Database(connection_factory=Connection)
    settings = Settings.from_env()
    app = create_app(settings, db)
    with TestClient(app, base_url='http://testserver') as client:
        yield client, db, settings


def csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, response.text[:400]
    return match.group(1)


def login(client, employee='M001', password='test-only-password', next_path='/'):
    token = csrf(client.get('/login'))
    return client.post('/login', data={'employee_no':employee,'password':password,
        'csrf_token':token,'next':next_path}, follow_redirects=False)


def test_anonymous_routes_redirect_and_legacy_is_disabled(portal):
    client, _, _ = portal
    for path in ['/', '/documents', '/documents/2', '/announcements', '/assistant']:
        assert client.get(path, follow_redirects=False).status_code == 303
    assert client.get('/api/posts').status_code == 404


@pytest.mark.parametrize('employee,allowed,denied', [
    ('M001',[1,2,6],[3,4,5]), ('R001',[1,3,6],[2,4,5]),
    ('M002',[1,2,6],[3,4,5]), ('A001',[1,2,3,4,6],[5])])
def test_document_list_detail_and_dashboard_share_permissions(portal,employee,allowed,denied):
    client, _, _ = portal
    assert login(client,employee).status_code == 303
    listing = client.get('/documents').text
    for doc in allowed:
        assert f'href="/documents/{doc}"' in listing
        assert client.get(f'/documents/{doc}').status_code == 200
    for doc in denied:
        assert f'href="/documents/{doc}"' not in listing
        assert client.get(f'/documents/{doc}').status_code == 404
    dashboard = client.get('/')
    assert dashboard.status_code == 200
    assert f'data-document-count="{len(allowed)}"' in dashboard.text


def test_wrong_password_inactive_user_and_missing_csrf_fail(portal):
    client, _, _ = portal
    assert login(client,password='wrong').status_code == 400
    assert login(client,employee='OFF').status_code == 400
    assert client.post('/login',data={'employee_no':'M001','password':'test-only-password'}).status_code == 403


def test_logout_revokes_copied_cookie_and_requires_post_csrf(portal):
    client, _, settings = portal
    login(client)
    copied = client.cookies.get(settings.session_cookie)
    assert client.get('/logout').status_code == 405
    assert client.post('/logout', data={}).status_code == 403
    assert client.post('/logout',data={'csrf_token':csrf(client.get('/'))},follow_redirects=False).status_code == 303
    client.cookies.set(settings.session_cookie,copied)
    assert client.get('/documents',follow_redirects=False).status_code == 303


def test_user_changes_are_read_from_database(portal):
    client, db, _ = portal
    login(client,'A001')
    assert client.get('/documents/4').status_code == 200
    db.execute('UPDATE users SET role=%s,department_id=%s WHERE id=%s',('employee',1,3))
    assert client.get('/documents/4').status_code == 404
    db.execute('UPDATE users SET is_active=0 WHERE id=3')
    assert client.get('/',follow_redirects=False).status_code == 303


def test_search_and_published_content_do_not_leak(portal):
    client, _, _ = portal
    login(client)
    response = client.get('/documents',params={'q':'研發','category':'SOP'})
    assert response.status_code == 200
    assert '研發機密內容' not in response.text
    assert 'href="/documents/3"' not in response.text
    assert client.get('/announcements/1').status_code == 200
    assert client.get('/announcements/2').status_code == 404
    assert client.get('/announcements/3').status_code == 404
    assert '草稿公告內容' not in client.get('/announcements').text
    assert '&lt;script&gt;' in client.get('/documents/6').text


@pytest.mark.parametrize('path',['https://evil.example','//evil.example','/\\evil.example','/login','/logout'])
def test_login_redirect_stays_inside_portal(portal,path):
    client, _, _ = portal
    response = login(client,next_path=path)
    assert response.headers['location'] == '/'


def test_throttle_persists_between_app_instances(portal):
    client, db, settings = portal
    for _ in range(settings.login_limit):
        assert login(client,password='wrong').status_code == 400
    assert login(client,password='wrong').status_code == 429
    from app import create_app
    with TestClient(create_app(settings,db)) as other:
        assert login(other,password='wrong').status_code == 429


def test_passwords_and_production_configuration(monkeypatch):
    from core.config import Settings
    from core.security import hash_password, verify_password
    hashed = hash_password('unique password')
    assert hashed.startswith('$argon2id$')
    assert hashed != hash_password('unique password')
    assert verify_password('unique password',hashed)
    assert not verify_password('bad',hashed)
    assert not verify_password('bad','invalid hash')
    monkeypatch.setenv('APP_ENV','production')
    monkeypatch.setenv('SESSION_SECRET','x'*48)
    monkeypatch.setenv('SESSION_HTTPS_ONLY','false')
    with pytest.raises(ValueError): Settings.from_env()
    monkeypatch.setenv('SESSION_HTTPS_ONLY','true')
    monkeypatch.setenv('ALLOWED_HOSTS','portal.example.com')
    assert Settings.from_env().https_only
    monkeypatch.delenv('SESSION_SECRET')
    with pytest.raises(ValueError): Settings.from_env()


def test_expired_or_tampered_session_cannot_read_documents(portal):
    import base64
    import json
    import time
    from itsdangerous import TimestampSigner
    client, _, settings=portal
    login(client)
    cookie=client.cookies.get(settings.session_cookie)
    client.cookies.clear()
    client.cookies.set(settings.session_cookie,cookie[:-8]+'tampered')
    assert client.get('/documents',follow_redirects=False).status_code==303
    payload={'user_id':1,'session_version':1,'expires_at':int(time.time())-1,'csrf_token':'t'*48}
    signed=TimestampSigner(settings.session_secret).sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client.cookies.clear()
    client.cookies.set(settings.session_cookie,signed)
    assert client.get('/documents/2',follow_redirects=False).status_code==303


def test_production_cookie_headers_https_and_host_guard(portal):
    from dataclasses import replace
    from app import create_app
    _, db, settings=portal
    settings=replace(settings,environment='production',https_only=True,allowed_hosts=('testserver',))
    with TestClient(create_app(settings,db),base_url='https://testserver') as client:
        response=client.get('/login')
        cookie=response.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'secure' in cookie and 'samesite=lax' in cookie
        assert response.headers['cache-control']=='no-store'
        assert 'frame-ancestors' in response.headers['content-security-policy']
        assert response.headers['strict-transport-security']=='max-age=31536000'
        assert client.get('http://testserver/login',follow_redirects=False).status_code==307
        assert client.get('https://evil.example/login').status_code==400


def test_department_disabled_unknown_role_and_query_validation(portal):
    client, db, _=portal
    login(client)
    assert client.get('/documents?q=%25').status_code==200
    assert 'href="/documents/2"' not in client.get('/documents?q=%25').text
    assert 'href="/documents/2"' not in client.get('/documents',params={'q':"' OR 1=1 --"}).text
    assert client.get('/documents?page=0').status_code==422
    assert client.get('/documents/-1').status_code==422
    assert client.get('/documents',params={'q':'x'*101}).status_code==422
    db.execute('UPDATE users SET role=%s WHERE id=1',('unrecognized',))
    assert client.get('/documents/2').status_code==404
    db.execute('UPDATE departments SET is_active=0 WHERE id=1')
    assert client.get('/documents',follow_redirects=False).status_code==303


def test_database_failure_returns_generic_service_page(portal):
    import mysql.connector
    client, db, _=portal
    login(client)
    def unavailable():
        raise mysql.connector.InterfaceError('private database error text')
    db.connection_factory=unavailable
    response=client.get('/documents')
    assert response.status_code==503
    assert 'private database error text' not in response.text
    assert '服務暫時無法使用' in response.text
