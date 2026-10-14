"""Opt-in browser flow with real HTTP and isolated test SQL, never production RDS."""
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from tests.test_portal import portal


@pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS')!='1',reason='Set RUN_BROWSER_TESTS=1 for local headless browser checks')
@pytest.mark.parametrize('width,height',[(1440,1000),(390,844)])
@pytest.mark.parametrize('demo',[False,True,'standalone'])
def test_responsive_employee_flow(portal,width,height,demo):
    import uvicorn
    from playwright.sync_api import sync_playwright
    client,db,settings=portal
    application=client.app
    if demo=='standalone':
        from app import create_app
        from core.config import Settings
        application=create_app(Settings.for_demo())
    elif demo:
        from dataclasses import replace
        from app import create_app
        application=create_app(replace(settings,environment='development',demo_login=True,demo_employee_no='M001'),db)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port=probe.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(application,host='127.0.0.1',port=port,log_level='warning',proxy_headers=False))
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline:
        time.sleep(.05)
    assert server.started
    artifacts=Path('.test-artifacts/screenshots')
    artifacts.mkdir(parents=True,exist_ok=True)
    default_chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    executable=os.getenv('BROWSER_EXECUTABLE') or (str(default_chrome) if default_chrome.exists() else None)
    suffix=f'{"standalone-" if demo=="standalone" else "demo-" if demo else ""}{width}'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=executable)
            page=browser.new_page(viewport={'width':width,'height':height},locale='zh-TW')
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda msg:errors.append(msg.text) if msg.type=='error' and '404' not in msg.text else None)
            base=f'http://127.0.0.1:{port}'
            page.goto(base+'/')
            assert '/login' in page.url
            assert page.locator('.login-story').count()==0
            form=page.locator('.login-form-wrap').bounding_box()
            assert abs(form['x']+form['width']/2-width/2)<2
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(artifacts/f'login-{suffix}.png'),full_page=True)
            page.get_by_label('帳號' if demo else '員工編號',exact=True).fill('任意帳號' if demo else 'M001')
            page.get_by_label('密碼',exact=True).fill('任意密碼' if demo else 'test-only-password')
            page.get_by_role('button',name='登入',exact=True).click()
            page.wait_for_url(base+'/')
            assert page.get_by_role('heading',name='工作總覽',exact=True).is_visible()
            page.screenshot(path=str(artifacts/f'dashboard-{suffix}.png'),full_page=True)
            if width<901:
                menu=page.locator('.menu-button')
                menu.click()
                assert menu.get_attribute('aria-expanded')=='true'
                page.keyboard.press('Escape')
                assert menu.get_attribute('aria-expanded')=='false'
            document_path,document_title=('/documents/4','精密塗布設備開機與清潔 SOP') if demo=='standalone' else ('/documents/2','塗布 SOP')
            announcement_title='SmartFab 內部工作平台正式開放展示' if demo=='standalone' else '教育訓練公告'
            for path,name in [('/documents','文件中心'),(document_path,document_title),('/announcements','公司公告'),('/announcements/1',announcement_title),('/assistant','AI 助理')]:
                response=page.goto(base+path)
                assert response.status==200
                assert page.get_by_role('heading',name=name,exact=True).is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),path
                page.screenshot(path=str(artifacts/f'{path.strip("/").replace("/","-")}-{suffix}.png'),full_page=True)
            assert page.goto(base+('/documents/6' if demo=='standalone' else '/documents/3')).status==404
            page.goto(base+'/documents')
            page.get_by_label('搜尋文件',exact=True).fill('不存在')
            page.get_by_role('button',name='搜尋',exact=True).click()
            assert page.get_by_role('heading',name='沒有符合條件的文件').is_visible()
            if width<901: page.get_by_role('button',name='開啟導覽選單').click()
            page.get_by_role('button',name='登出系統').click()
            page.wait_for_url(base+'/login')
            assert not errors,errors
            browser.close()
    finally:
        server.should_exit=True
        thread.join(timeout=10)
