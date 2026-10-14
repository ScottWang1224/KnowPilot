"""Opt-in browser flow with real HTTP and isolated test SQL, never production RDS."""
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from tests.test_portal import PASSWORD, portal


@pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS')!='1',reason='Set RUN_BROWSER_TESTS=1 for local headless browser checks')
@pytest.mark.parametrize('width,height',[(1440,1000),(390,844)])
def test_responsive_employee_flow(portal,width,height):
    import uvicorn
    from playwright.sync_api import expect, sync_playwright
    application=portal[0].app
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
    suffix=str(width)
    base=f'http://127.0.0.1:{port}'

    def heading(page,name):
        expect(page.get_by_role('heading',name=name,exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),page.url

    def shot(page,name):
        page.screenshot(path=str(artifacts/f'{name}-{suffix}.png'),full_page=True)

    def open_menu(page):
        if width<901: page.get_by_role('button',name='開啟導覽選單').click()

    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=executable)
            page=browser.new_page(viewport={'width':width,'height':height},locale='zh-TW')
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda msg:errors.append(msg.text) if msg.type=='error' and 'status of 4' not in msg.text else None)

            # Signed-out visitors are sent to login, then back to where they were going.
            page.goto(base+'/documents/2')
            page.wait_for_url(base+'/login?next=%2Fdocuments%2F2')
            heading(page,'登入工作平台')
            form=page.locator('.login-form-wrap').bounding_box()
            assert abs(form['x']+form['width']/2-width/2)<2
            shot(page,'login')
            page.get_by_label('電子信箱',exact=True).fill('m001@example.com')
            page.get_by_label('密碼',exact=True).fill('wrong-password')
            page.get_by_role('button',name='登入',exact=True).click()
            expect(page.get_by_role('alert')).to_have_text('電子信箱或密碼錯誤')
            page.get_by_label('密碼',exact=True).fill(PASSWORD)
            page.get_by_role('button',name='登入',exact=True).click()
            page.wait_for_url(base+'/documents/2')
            heading(page,'塗布 SOP')

            page.goto(base+'/')
            heading(page,'工作總覽')
            expect(page.locator('[data-document-count="3"]')).to_be_visible()
            shot(page,'dashboard')
            if width<901:
                menu=page.locator('.menu-button')
                menu.click()
                assert menu.get_attribute('aria-expanded')=='true'
                page.keyboard.press('Escape')
                assert menu.get_attribute('aria-expanded')=='false'
            for path,name in [('/documents','文件中心'),('/documents/2','塗布 SOP'),('/announcements','公司公告'),
                              ('/announcements/1','教育訓練公告'),('/assistant','AI 助理'),('/profile','林員工')]:
                page.goto(base+path)
                heading(page,name)
                shot(page,path.strip('/').replace('/','-') or 'home')
            page.goto(base+'/documents/3')
            heading(page,'找不到這個頁面')
            page.goto(base+'/documents')
            heading(page,'文件中心')
            page.get_by_label('搜尋文件',exact=True).fill('不存在')
            page.get_by_role('button',name='搜尋',exact=True).click()
            heading(page,'沒有符合條件的文件')
            open_menu(page)
            page.get_by_role('button',name='登出系統').click()
            page.wait_for_url(base+'/login')
            page.goto(base+'/')
            page.wait_for_url(base+'/login?next=%2F')

            # Registration signs the new user in with the department they chose.
            page.get_by_role('link',name='註冊新帳號').click()
            heading(page,'註冊帳號')
            page.get_by_label('名稱',exact=True).fill('測試者')
            page.get_by_label('電子信箱',exact=True).fill(f'tester{width}@example.com')
            page.get_by_label('密碼',exact=True).fill('short')
            page.get_by_label('部門',exact=True).select_option(label='研發部')
            page.get_by_role('button',name='註冊並登入').click()
            expect(page.get_by_role('alert')).to_have_text('密碼至少需要 8 個字元。')
            shot(page,'register')
            page.get_by_label('密碼',exact=True).fill('tester-password')
            page.get_by_role('button',name='註冊並登入').click()
            page.wait_for_url(base+'/')
            expect(page.locator('.department-stat')).to_have_text('研發部')
            expect(page.locator('[data-document-count="4"]')).to_be_visible()

            # Department switch: unchanged sends nothing; cancel sends nothing; accept switches.
            patches=[]
            page.on('request',lambda request:patches.append(request) if request.method=='PATCH' else None)
            dialogs=[]
            answers=[]
            page.on('dialog',lambda dialog:(dialogs.append(dialog.message),dialog.accept() if answers.pop(0) else dialog.dismiss()))
            page.goto(base+'/profile')
            heading(page,'測試者')
            page.get_by_role('button',name='儲存部門').click()
            page.wait_for_timeout(300)
            assert dialogs==[] and patches==[]
            answers.append(False)
            page.get_by_label('所屬部門',exact=True).select_option(label='製造部')
            page.get_by_role('button',name='儲存部門').click()
            expect(page.get_by_label('所屬部門',exact=True)).to_have_value('2')
            assert len(dialogs)==1 and '對話紀錄將從對話清單中移除' in dialogs[0]
            page.wait_for_timeout(300)
            assert patches==[]
            answers.append(True)
            page.get_by_label('所屬部門',exact=True).select_option(label='製造部')
            page.get_by_role('button',name='儲存部門').click()
            expect(page.get_by_role('status')).to_have_text('已切換至製造部。')
            assert len(dialogs)==2 and len(patches)==1
            expect(page.locator('.sidebar-profile small')).to_have_text('製造部 · 員工')
            shot(page,'profile-switched')
            assert not errors,errors
            browser.close()
    finally:
        server.should_exit=True
        thread.join(timeout=10)
