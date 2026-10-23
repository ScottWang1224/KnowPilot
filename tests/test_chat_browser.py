"""Opt-in browser flow for /assistant with the fake provider: real HTTP, seeded temporary SQLite, no network."""
import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from tests.test_chat import (ROUTE_OUT_OF_SCOPE, answer_reply, chat_app, fakes, first_chunk,  # noqa: F401  fixtures
                             new_user, route_answer, route_clarify)

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1',
                                reason='Set RUN_BROWSER_TESTS=1 for local headless browser checks')
ARTIFACTS = Path('.test-artifacts/screenshots')


@contextmanager
def serve(application):
    import uvicorn
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=port, log_level='warning', proxy_headers=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.05)
    assert server.started
    try:
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@contextmanager
def browser_page(width, height):
    from playwright.sync_api import sync_playwright
    default_chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    executable = os.getenv('BROWSER_EXECUTABLE') or (str(default_chrome) if default_chrome.exists() else None)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=executable)
        context = browser.new_context(viewport={'width': width, 'height': height}, locale='zh-TW')
        errors = []
        context.on('weberror', lambda error: errors.append(str(error.error)))
        page = context.new_page()
        page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' and 'status of 4' not in msg.text else None)
        yield page, errors
        browser.close()


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_chat_page_flow(chat_app, fakes, width, height):
    from playwright.sync_api import expect
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)
    chunk = first_chunk(db, 'MFG-SOP-002')
    fakes([route_clarify(safety_note='清潔前先停機並上鎖掛牌。'), route_answer(chunk, {'line': 'CL-01'}), ROUTE_OUT_OF_SCOPE],
          [answer_reply([chunk.chunk_id], text='依 SOP 第 2 節清潔', condition='CL-01', safety=[chunk.chunk_id])])
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    shot = lambda page, name: page.screenshot(path=str(ARTIFACTS / f'chat-{name}-{width}.png'))  # noqa: E731

    def no_horizontal_scroll(page):
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), page.url

    with serve(client.app) as base, browser_page(width, height) as (page, errors):
        page.goto(base + '/login')
        page.evaluate('token => localStorage.setItem("knowpilot_token", token)', headers['Authorization'].split()[1])
        page.goto(base + '/assistant')
        expect(page.get_by_role('heading', name='想查什麼？')).to_be_visible()
        expect(page.locator('[data-quota-text]')).to_contain_text('約可再問')
        expect(page.locator('.nav-soon')).to_have_count(0)
        no_horizontal_scroll(page)
        shot(page, 'empty')

        box = page.get_by_label('輸入問題')
        send = page.get_by_role('button', name='送出', exact=True)
        expect(send).to_be_disabled()
        box.fill('一' * 501)
        expect(page.locator('.char-count')).to_have_class('char-count is-over')
        expect(send).to_be_disabled()
        box.fill('塗布頭怎麼清潔？')
        box.press('Shift+Enter')
        expect(box).to_have_value('塗布頭怎麼清潔？\n')
        box.press('Enter')
        expect(page.locator('.message-user')).to_have_text('塗布頭怎麼清潔？\n')
        expect(page.get_by_text('哪一條產線？')).to_be_visible()
        expect(page.locator('.safety-note').first).to_contain_text('清潔前先停機並上鎖掛牌。')
        shot(page, 'clarify')

        page.get_by_role('button', name='CL-01', exact=True).click()
        answer = page.locator('.message-assistant').last
        expect(answer.locator('.answer-point')).to_contain_text('依 SOP 第 2 節清潔')
        expect(answer.locator('.condition')).to_have_text('CL-01')
        expect(page.get_by_role('button', name='CL-02', exact=True)).to_be_disabled()  # old options are spent
        expect(page.locator('#conversation-list button')).to_have_count(1)
        assert '/assistant?c=' in page.url
        no_horizontal_scroll(page)
        shot(page, 'answer')

        citation = answer.locator('.answer-point a.citation')
        expect(citation).to_have_attribute('target', '_blank')
        with page.context.expect_page() as opened:
            citation.click()
        document = opened.value
        heading = document.locator('.section-flash')
        expect(heading).to_have_text(chunk.section_path.split(' › ')[-1])
        expect(heading).to_be_in_viewport()
        document.screenshot(path=str(ARTIFACTS / f'chat-citation-{width}.png'))
        document.close()

        answer.get_by_role('button', name='沒幫助').click()
        expect(answer.get_by_role('button', name='沒幫助')).to_have_attribute('aria-pressed', 'true')
        answer.get_by_label('回饋意見').fill('想知道 CL-02 的做法')
        answer.get_by_role('button', name='送出意見').click()
        expect(answer.locator('.feedback-status')).to_have_text('已收到你的意見，謝謝！')
        assert db.query('SELECT rating,comment FROM feedback WHERE user_id=%s', (user_id,)) == [
            {'rating': 'down', 'comment': '想知道 CL-02 的做法'}]

        page.reload()
        expect(page.locator('.message')).to_have_count(4)
        expect(page.get_by_role('button', name='CL-01', exact=True)).to_be_disabled()
        expect(page.locator('.message-assistant').last.get_by_role('button', name='沒幫助')).to_have_attribute('aria-pressed', 'true')

        if width < 901:
            toggle = page.get_by_role('button', name='對話紀錄', exact=True)
            toggle.click()
            expect(page.locator('#conversation-list')).to_be_visible()
            shot(page, 'conversations')
            toggle.click()
            expect(page.locator('#conversation-list')).to_be_hidden()

        page.on('dialog', lambda dialog: dialog.accept())
        page.get_by_role('button', name='隱藏此對話').click()
        expect(page.get_by_role('heading', name='想查什麼？')).to_be_visible()
        expect(page.locator('#conversation-list button')).to_have_count(0)
        assert page.url.endswith('/assistant')
        assert not errors, errors


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_admin_pages_flow(chat_app, fakes, width, height):
    from playwright.sync_api import expect
    client, db, _, departments = chat_app
    user_id, user_headers = new_user(chat_app)
    _, admin_headers = new_user(chat_app, 'ADM', role='admin')
    chunk = first_chunk(db, 'MFG-SOP-002')
    fakes([route_answer(chunk)], [answer_reply([chunk.chunk_id])])
    sent = client.post('/api/chat', headers=user_headers, json={'message': f'燈管多久換？（{width}）'}).json()
    client.put(f'/api/messages/{sent["message"]["id"]}/feedback', headers=user_headers, json={'rating': 'down', 'comment': '太簡略'})
    client.post(f'/api/conversations/{sent["conversation"]["id"]}/hide', headers=user_headers)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    shot = lambda page, name: page.screenshot(path=str(ARTIFACTS / f'admin-{name}-{width}.png'), full_page=True)  # noqa: E731

    def no_horizontal_scroll(page):
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), page.url

    def sign_in(page, headers):
        # A static file on the same origin: /login would redirect a signed-in user while the token is being set.
        page.goto(base + '/static/css/index.css')
        page.evaluate('token => localStorage.setItem("knowpilot_token", token)', headers['Authorization'].split()[1])

    with serve(client.app) as base, browser_page(width, height) as (page, errors):
        sign_in(page, user_headers)
        page.goto(base + '/admin/dashboard')
        expect(page.get_by_role('heading', name='無法完成請求')).to_be_visible()
        expect(page.get_by_text('沒有權限執行此操作')).to_be_visible()
        expect(page.locator('[data-admin-link]')).to_be_hidden()

        sign_in(page, admin_headers)
        page.goto(base + '/admin/dashboard')
        expect(page.get_by_role('heading', name='統計儀表板')).to_be_visible()
        expect(page.get_by_role('button', name='最近 14 天')).to_have_attribute('aria-pressed', 'true')
        expect(page.locator('.stat-tile')).to_have_count(5)
        expect(page.locator('#cost-chart .column')).to_have_count(14)
        expect(page.locator('#daily-table tbody tr')).to_have_count(14)
        down_link = page.locator('#down-list').get_by_role('link', name=f'燈管多久換？（{width}）')
        expect(down_link).to_be_visible()
        no_horizontal_scroll(page)
        shot(page, 'dashboard')
        page.get_by_role('button', name='最近 7 天').click()
        expect(page.locator('#cost-chart .column')).to_have_count(7)
        assert page.url.endswith('?days=7')

        down_link.click()
        page.wait_for_url(f'**/admin/users/{user_id}?c={sent["conversation"]["id"]}')
        thread = page.locator('#admin-thread')
        expect(thread.locator('.hidden-badge')).to_contain_text('使用者隱藏')
        expect(thread.locator('.admin-feedback')).to_have_text('回饋：沒幫助「太簡略」')
        thread.locator('.trace summary').click()
        expect(thread.locator('.trace dd').first).to_have_text('answer')
        # The ADM admin opens an MFG citation through the admin document page, at the cited section.
        with page.context.expect_page() as opened:
            thread.locator('.admin-sources a.citation').first.click()
        document = opened.value
        expect(document.locator('.admin-document-notice')).to_be_visible()
        expect(document.locator('.document-code')).to_have_text('MFG-SOP-002')
        expect(document.locator('.section-flash')).to_have_text(chunk.section_path.split(' › ')[-1])
        document.screenshot(path=str(ARTIFACTS / f'admin-document-{width}.png'))
        document.close()
        no_horizontal_scroll(page)
        shot(page, 'user')

        page.goto(base + '/admin/users')
        expect(page.get_by_role('heading', name='使用者', exact=True)).to_be_visible()
        expect(page.locator('#user-table').get_by_role('link', name=db.query('SELECT name FROM users WHERE id=%s', (user_id,), one=True)['name'])).to_be_visible()
        if width < 901:
            page.get_by_role('button', name='開啟導覽選單').click()
        expect(page.locator('[data-admin-link]')).to_be_visible()
        no_horizontal_scroll(page)
        shot(page, 'users')
        assert not errors, errors
