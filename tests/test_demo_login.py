from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from tests.test_portal import portal, login


def test_development_accepts_arbitrary_credentials_without_granting_admin(portal):
    from app import create_app
    _,db,settings=portal
    settings=replace(settings,environment='development',demo_login=True,demo_employee_no='M001')
    with TestClient(create_app(settings,db)) as client:
        for account,password in [('hello','anything'),('ADMIN001','123'),('隨便輸入','測試密碼')]:
            client.cookies.clear()
            assert login(client,account,password).status_code==303
            home=client.get('/')
            assert '林員工' in home.text
            assert client.get('/documents/3').status_code==404
            assert client.get('/documents/4').status_code==404


@pytest.mark.parametrize('account,password',[('','anything'),('anything',''),('  ','  ')])
def test_demo_login_requires_nonempty_fields(portal,account,password):
    from app import create_app
    _,db,settings=portal
    settings=replace(settings,environment='development',demo_login=True,demo_employee_no='M001')
    with TestClient(create_app(settings,db)) as client:
        assert login(client,account,password).status_code==400
        assert client.get('/',follow_redirects=False).status_code==303


def test_demo_mode_defaults_to_development_and_cannot_enable_in_production(monkeypatch):
    from core.config import Settings
    monkeypatch.setenv('SESSION_SECRET','test-secret-'+'x'*40)
    monkeypatch.delenv('DEMO_LOGIN_ENABLED',raising=False)
    monkeypatch.setenv('APP_ENV','development')
    assert Settings.from_env().demo_login
    monkeypatch.setenv('DEMO_LOGIN_ENABLED','false')
    assert not Settings.from_env().demo_login
    monkeypatch.setenv('APP_ENV','production')
    monkeypatch.setenv('SESSION_HTTPS_ONLY','true')
    monkeypatch.setenv('ALLOWED_HOSTS','portal.example.com')
    monkeypatch.setenv('DEMO_LOGIN_ENABLED','true')
    with pytest.raises(ValueError,match='DEMO_LOGIN_ENABLED'):
        Settings.from_env()


def test_demo_requires_active_demo_employee_and_retains_csrf(portal):
    from app import create_app
    _,db,settings=portal
    settings=replace(settings,environment='development',demo_login=True,demo_employee_no='OFF')
    with TestClient(create_app(settings,db)) as client:
        assert login(client,'arbitrary','arbitrary').status_code==503
        assert client.post('/login',data={'employee_no':'anything','password':'anything'}).status_code==403


def test_demo_cookie_is_rejected_when_demo_mode_is_disabled(portal):
    from app import create_app
    _,db,settings=portal
    demo=replace(settings,environment='development',demo_login=True,demo_employee_no='M001')
    with TestClient(create_app(demo,db)) as client:
        login(client,'anything','anything')
        copied=client.cookies.get(settings.session_cookie)
    production=replace(settings,environment='production',https_only=True,demo_login=False)
    with TestClient(create_app(production,db),base_url='https://testserver') as client:
        client.cookies.set(settings.session_cookie,copied)
        assert client.get('/documents',follow_redirects=False).status_code==303
