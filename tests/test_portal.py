"""Contract tests: real app, SQL repositories, hashing, JWT and the SQLite migration.

Numbers in test docstrings refer to the acceptance cases in .agent-local/auth-reference.md §7.
SQLite does not replace the opt-in MySQL migration/integration checks.
"""
import pytest
from fastapi.testclient import TestClient

PASSWORD = 'test-only-password'


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_ENV', 'test')
    from app import create_app
    from core.config import Settings
    from core.database import Database
    from core.security import hash_password
    from scripts.migrate import migrate

    db = Database.sqlite(tmp_path / 'portal.db')
    migrate(db, 'sqlite')
    pw = hash_password(PASSWORD)
    now = '2026-01-01 00:00:00'
    with db.transaction() as cursor:
        for row in [(1,'MFG','製造部'),(2,'RD','研發部')]:
            cursor.execute('INSERT INTO departments(id,code,name) VALUES(%s,%s,%s)', row)
        for row in [(1,'m001@example.com','林員工',pw,1,'user',1), (2,'r001@example.com','研發員工',pw,2,'user',1),
                    (3,'a001@example.com','管理員',pw,1,'admin',1), (4,'off@example.com','停用員工',pw,1,'user',0)]:
            cursor.execute('INSERT INTO users(id,email,name,password_hash,department_id,role,is_active) '
                           'VALUES(%s,%s,%s,%s,%s,%s,%s)', row)
        for row in [
            (1,'GEN-001','公司規章','摘要','全公司內容','規章',None,'company','1.0','published'),
            (2,'MFG-001','塗布 SOP','摘要','製造機密內容','SOP',1,'department','1.2','published'),
            (3,'RD-001','研發 SOP','摘要','研發機密內容','SOP',2,'department','2.0','published'),
            (4,'SEC-001','配方管制','摘要','研發配方內容','規章',2,'department','1.0','published'),
            (5,'DRAFT-001','草稿文件','摘要','草稿內容','SOP',None,'company','0.1','draft'),
            (6,'XSS-001','安全文字','摘要','<script>alert(1)</script>','訓練',None,'company','1.0','published')]:
            cursor.execute('INSERT INTO documents(id,document_code,title,summary,content,category,department_id,'
                           'visibility,version,status,created_at,updated_at) '
                           'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', row + (now, now))
        for row in [(1,'教育訓練公告','請參加訓練','教育訓練',1,'published',now,3),
                    (2,'未發布公告','草稿公告內容','行政',0,'draft',now,3),
                    (3,'未來公告','未來公告內容','行政',0,'published','2099-01-01 00:00:00',3)]:
            cursor.execute('INSERT INTO announcements(id,title,content,category,is_pinned,status,published_at,created_by) '
                           'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', row)

    settings = Settings.from_env()
    with TestClient(create_app(settings, db), base_url='http://testserver') as client:
        yield client, db, settings


def login(client, email='m001@example.com', password=PASSWORD):
    return client.post('/api/auth/login', json={'email': email, 'password': password})


def bearer(client, email='m001@example.com'):
    response = login(client, email)
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}


def visible_documents(client, headers, **params):
    return {d['id'] for d in client.get('/api/documents', headers=headers, params=params).json()['data']}


def assert_error(response, status):
    assert response.status_code == status, response.text
    body = response.json()
    assert body['error'] is True and body['message']
    return body['message']


def test_login_and_uniform_failure_messages(portal):
    """1. Correct credentials work; wrong password, unknown and inactive accounts share one 401 message."""
    client, _, _ = portal
    assert login(client).status_code == 200
    assert login(client, email='  M001@Example.com ').status_code == 200
    messages = {assert_error(login(client, password='wrong-password'), 401),
                assert_error(login(client, email='nobody@example.com'), 401),
                assert_error(login(client, email='off@example.com'), 401),
                assert_error(login(client, email='not-an-email'), 401)}
    assert messages == {'電子信箱或密碼錯誤'}


def test_register_hashes_password_and_never_grants_admin(portal):
    """2, 6. Passwords are hashed; a role in the request is ignored; bad input is rejected without writes."""
    client, db, _ = portal
    body = {'name': '新同事', 'email': 'New@Example.com', 'password': 'a-strong-password', 'department_id': 2, 'role': 'admin'}
    assert client.post('/api/auth/register', json=body).status_code == 201
    row = db.query('SELECT * FROM users WHERE email=%s', ('new@example.com',), one=True)
    assert row['role'] == 'user' and row['department_id'] == 2
    assert row['password_hash'].startswith('$argon2id$') and 'a-strong-password' not in row['password_hash']
    assert_error(client.post('/api/auth/register', json={**body, 'email': 'NEW@example.com'}), 409)
    count = db.query('SELECT COUNT(*) AS n FROM users', one=True)['n']
    assert_error(client.post('/api/auth/register', json={**body, 'email': 'x@example.com', 'department_id': 99}), 400)
    assert '密碼' in assert_error(client.post('/api/auth/register', json={**body, 'email': 'y@example.com', 'password': 'short'}), 400)
    assert_error(client.post('/api/auth/register', json={**body, 'email': 'not-an-email'}), 400)
    assert_error(client.post('/api/auth/register', json={**body, 'email': 'z@example.com', 'name': '   '}), 400)
    assert db.query('SELECT COUNT(*) AS n FROM users', one=True)['n'] == count
    assert login(client, 'new@example.com', 'a-strong-password').status_code == 200


def test_me_rejects_missing_invalid_expired_and_tampered_tokens(portal):
    """3. Without a valid token /api/auth/me is 401."""
    import jwt
    from datetime import datetime, timedelta, timezone
    client, _, settings = portal
    headers = bearer(client)
    me = client.get('/api/auth/me', headers=headers).json()['data']
    assert me == {'id': 1, 'name': '林員工', 'email': 'm001@example.com', 'role': 'user', 'department': {'id': 1, 'name': '製造部'}}
    good = headers['Authorization'].split()[1]
    expired = jwt.encode({'sub': '1', 'exp': datetime.now(timezone.utc) - timedelta(seconds=1)}, settings.jwt_secret, algorithm='HS256')
    forged = jwt.encode({'sub': '3', 'exp': datetime.now(timezone.utc) + timedelta(hours=1)}, 'another-secret-' + 'x' * 40, algorithm='HS256')
    no_expiry = jwt.encode({'sub': '1'}, settings.jwt_secret, algorithm='HS256')
    missing_user = jwt.encode({'sub': '999', 'exp': datetime.now(timezone.utc) + timedelta(hours=1)}, settings.jwt_secret, algorithm='HS256')
    assert_error(client.get('/api/auth/me'), 401)
    for value in ['garbage', good[:-4] + 'AAAA', expired, forged, no_expiry, missing_user]:
        assert_error(client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + value}), 401)


def test_deactivated_user_or_department_invalidates_existing_token(portal):
    """4. is_active is read on every request."""
    client, db, _ = portal
    headers = bearer(client)
    assert client.get('/api/documents', headers=headers).status_code == 200
    db.execute('UPDATE departments SET is_active=0 WHERE id=1')
    assert_error(client.get('/api/documents', headers=headers), 401)
    db.execute('UPDATE departments SET is_active=1 WHERE id=1')
    db.execute('UPDATE users SET is_active=0 WHERE id=1')
    assert_error(client.get('/api/auth/me', headers=headers), 401)


def test_missing_jwt_secret_refuses_to_start(monkeypatch):
    """5."""
    from app import create_app
    from core.config import Settings
    monkeypatch.setenv('APP_ENV', 'test')
    for value in (None, 'too-short'):
        if value is None: monkeypatch.delenv('JWT_SECRET', raising=False)
        else: monkeypatch.setenv('JWT_SECRET', value)
        with pytest.raises(ValueError, match='JWT_SECRET'): Settings.from_env()
        with pytest.raises(ValueError, match='JWT_SECRET'): create_app()


@pytest.mark.parametrize('email,allowed,denied', [
    ('m001@example.com', {1,2,6}, {3,4,5}), ('r001@example.com', {1,3,4,6}, {2,5}),
    ('a001@example.com', {1,2,6}, {3,4,5})])
def test_documents_follow_department_only(portal, email, allowed, denied):
    """15. Admin sees exactly what a same-department user sees."""
    client, _, _ = portal
    headers = bearer(client, email)
    result = client.get('/api/documents', headers=headers).json()
    assert {d['id'] for d in result['data']} == allowed and result['total'] == len(allowed)
    for doc in allowed:
        assert client.get(f'/api/documents/{doc}', headers=headers).status_code == 200
    for doc in denied:
        assert_error(client.get(f'/api/documents/{doc}', headers=headers), 404)


def test_department_switch_applies_to_same_token(portal):
    """7. A new department applies on the next request without a new token."""
    client, db, _ = portal
    headers = bearer(client)
    assert visible_documents(client, headers) == {1, 2, 6}
    response = client.patch('/api/auth/me/department', headers=headers, json={'department_id': 2, 'confirm_clear_history': True})
    assert response.status_code == 200
    assert response.json()['data']['department'] == {'id': 2, 'name': '研發部'}
    assert response.json()['hidden_conversations'] == 0
    assert visible_documents(client, headers) == {1, 3, 4, 6}
    assert db.query('SELECT department_id FROM users WHERE id=1', one=True)['department_id'] == 2


def test_request_department_or_role_parameters_are_ignored(portal):
    """8."""
    client, _, _ = portal
    headers = bearer(client)
    assert visible_documents(client, headers, department_id=2, role='admin') == {1, 2, 6}
    assert_error(client.get('/api/documents/3', headers=headers, params={'department_id': 2}), 404)
    response = client.patch('/api/auth/me/department', headers=headers,
                            json={'department_id': 1, 'confirm_clear_history': True, 'role': 'admin', 'user_id': 2})
    assert response.json()['data']['role'] == 'user' and response.json()['data']['id'] == 1


def test_department_switch_requires_confirmation_and_valid_department(portal):
    """6, 9. Nothing changes without confirm_clear_history or with an unknown department."""
    client, db, _ = portal
    headers = bearer(client)
    for body in [{'department_id': 2}, {'department_id': 2, 'confirm_clear_history': False},
                 {'department_id': 99, 'confirm_clear_history': True}]:
        assert_error(client.patch('/api/auth/me/department', headers=headers, json=body), 400)
    assert db.query('SELECT department_id FROM users WHERE id=1', one=True)['department_id'] == 1
    assert_error(client.patch('/api/auth/me/department', json={'department_id': 2, 'confirm_clear_history': True}), 401)


def test_admin_api_requires_admin_role(portal):
    """13, 14 (users list; conversation and feedback APIs wait for the chat tables)."""
    client, _, _ = portal
    assert_error(client.get('/api/admin/users'), 401)
    assert_error(client.get('/api/admin/users', headers=bearer(client)), 403)
    users = client.get('/api/admin/users', headers=bearer(client, 'a001@example.com')).json()['data']
    assert {u['email'] for u in users} >= {'m001@example.com', 'off@example.com'}
    assert all('password_hash' not in u for u in users)


def test_departments_list_is_public_and_active_only(portal):
    client, db, _ = portal
    db.execute('UPDATE departments SET is_active=0 WHERE id=2')
    assert client.get('/api/departments').json()['data'] == [{'id': 1, 'code': 'MFG', 'name': '製造部'}]


def test_content_apis_require_login_and_hide_unpublished(portal):
    client, _, _ = portal
    for path in ['/api/documents', '/api/documents/1', '/api/announcements', '/api/announcements/1']:
        assert_error(client.get(path), 401)
    headers = bearer(client)
    result = client.get('/api/documents', headers=headers, params={'q': '研發', 'category': 'SOP'}).json()
    assert result['data'] == [] and result['categories'] == ['SOP', '規章', '訓練']
    assert [a['id'] for a in client.get('/api/announcements', headers=headers).json()['data']] == [1]
    item = client.get('/api/announcements/1', headers=headers).json()['data']
    assert item['author_name'] == '管理員' and item['published_at'] == '2026-01-01T00:00:00+00:00'
    assert_error(client.get('/api/announcements/2', headers=headers), 404)
    assert_error(client.get('/api/announcements/3', headers=headers), 404)
    assert visible_documents(client, headers, q='%') == set()
    assert visible_documents(client, headers, q="' OR 1=1 --") == set()


def test_pages_are_data_free_shells(portal):
    client, _, _ = portal
    for path in ['/', '/documents', '/documents/3', '/announcements', '/announcements/2', '/assistant', '/profile', '/login', '/register',
                 '/admin/dashboard', '/admin/users', '/admin/users/2', '/admin/documents/3']:
        page = client.get(path)
        assert page.status_code == 200
        assert '研發機密內容' not in page.text and '草稿公告內容' not in page.text
        assert 'r001@example.com' not in page.text  # admin shells carry no user data either
    assert client.get('/documents/-1').status_code == 422
    assert client.get('/missing').status_code == 404


def test_api_validation_and_unknown_routes_use_json_errors(portal):
    client, _, _ = portal
    headers = bearer(client)
    assert '頁碼' in assert_error(client.get('/api/documents', headers=headers, params={'page': 0}), 400)
    assert_error(client.get('/api/documents/-1', headers=headers), 400)
    assert_error(client.get('/api/documents', headers=headers, params={'q': 'x' * 101}), 400)
    assert_error(client.post('/api/auth/login', content='not json', headers={'Content-Type': 'application/json'}), 400)
    assert assert_error(client.get('/api/nothing-here'), 404) == '找不到資源'


def test_throttle_persists_between_app_instances(portal):
    client, db, settings = portal
    for _ in range(settings.login_limit):
        assert_error(login(client, password='wrong-password'), 401)
    response = login(client)
    assert_error(response, 429)
    assert response.headers['retry-after'] == str(settings.login_window)
    from app import create_app
    with TestClient(create_app(settings, db)) as other:
        assert_error(login(other), 429)
        # Registration has its own budget.
        body = {'name': 'n', 'email': 'q@example.com', 'password': 'long-enough', 'department_id': 1}
        assert other.post('/api/auth/register', json=body).status_code == 201


def test_database_failure_returns_generic_errors(portal):
    import mysql.connector
    client, db, _ = portal
    headers = bearer(client)
    def unavailable():
        raise mysql.connector.InterfaceError('private database error text')
    db.connection_factory = unavailable
    response = client.get('/api/documents', headers=headers)
    assert assert_error(response, 503) == '資料服務暫時無法使用，請稍後再試'
    assert 'private database error text' not in response.text


def test_production_configuration_and_headers(portal, monkeypatch):
    from dataclasses import replace
    from app import create_app
    from core.config import Settings
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('ALLOWED_HOSTS', 'portal.example.com')
    for name in ('DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME', 'DB_SSL_CA'):
        monkeypatch.setenv(name, 'configured')
    monkeypatch.setenv('DB_ENGINE', 'sqlite')
    with pytest.raises(ValueError, match='DB_ENGINE'): Settings.from_env()
    monkeypatch.setenv('DB_ENGINE', 'mysql')
    assert Settings.from_env().environment == 'production'
    monkeypatch.delenv('DB_SSL_CA')
    with pytest.raises(ValueError, match='DB_SSL_CA'): Settings.from_env()
    monkeypatch.setenv('DB_SSL_CA', 'configured')
    monkeypatch.setenv('ALLOWED_HOSTS', '*')
    with pytest.raises(ValueError, match='ALLOWED_HOSTS'): Settings.from_env()

    _, db, settings = portal
    settings = replace(settings, environment='production', allowed_hosts=('testserver',))
    with TestClient(create_app(settings, db), base_url='https://testserver') as client:
        response = client.get('/login')
        assert 'set-cookie' not in response.headers
        assert response.headers['cache-control'] == 'no-store'
        assert "script-src 'self'" in response.headers['content-security-policy']
        assert response.headers['strict-transport-security'] == 'max-age=31536000'
        assert client.get('http://testserver/login', follow_redirects=False).status_code == 307
        assert client.get('https://evil.example/login').status_code == 400
