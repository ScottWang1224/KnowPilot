"""Opt-in browser check (phase 6): markup written by the model or the user is shown as text, never as elements.
Fake provider only: real HTTP, seeded temporary SQLite, no network."""
import json
import os

import pytest
from tests.test_chat import chat_app, fakes, first_chunk, new_user  # noqa: F401  fixtures
from tests.test_chat_browser import browser_page, serve

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1',
                                reason='Set RUN_BROWSER_TESTS=1 for local headless browser checks')
SCRIPT = '<script>window.__pwned = 1</script>'
IMAGE = '<img src=x onerror="window.__pwned = 2">'
LINK = '<a href="javascript:window.__pwned = 3">點我</a>'


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_markup_from_the_model_and_the_user_stays_text(chat_app, fakes, width, height):
    from playwright.sync_api import expect
    client, db, _, _ = chat_app
    _, headers = new_user(chat_app)
    _, admin = new_user(chat_app, role='admin')
    chunk = first_chunk(db, 'MFG-SOP-001')
    route = json.dumps({'action': 'answer', 'intent_summary': IMAGE, 'state': {'topic': SCRIPT},
                        'selected': [{'doc_code': 'MFG-SOP-001', 'sections': [chunk.section_path]}]})
    reply = json.dumps({'status': 'partial', 'missing': LINK,
                        'safety_note': {'text': IMAGE, 'citations': [chunk.chunk_id]},
                        'points': [{'text': SCRIPT, 'condition': IMAGE, 'citations': [chunk.chunk_id]}]}, ensure_ascii=False)
    fakes([route], [reply])
    question = f'{SCRIPT}{IMAGE} 開機前要檢查什麼？'

    with serve(client.app) as base, browser_page(width, height) as (page, errors):
        page.on('dialog', lambda dialog: errors.append('dialog: ' + dialog.message))
        page.goto(base + '/static/css/index.css')
        page.evaluate('t => localStorage.setItem("knowpilot_token", t)', headers['Authorization'].split()[1])
        page.goto(base + '/assistant')
        page.get_by_label('輸入問題').fill(question)
        page.get_by_label('輸入問題').press('Enter')
        answer = page.locator('.message-assistant').last
        expect(answer.locator('.answer-point')).to_contain_text(SCRIPT)
        expect(answer.locator('.safety-note')).to_contain_text(IMAGE)
        expect(answer.locator('.answer-missing')).to_have_text(LINK)
        expect(page.locator('#conversation-list .conversation-title')).to_contain_text('<script>')
        log = page.locator('#chat-log')
        assert log.locator('script, img, iframe').count() == 0
        assert log.locator('a[href^="javascript"]').count() == 0
        assert page.locator('#conversation-list script, #conversation-list img').count() == 0

        page.evaluate('t => localStorage.setItem("knowpilot_token", t)', admin['Authorization'].split()[1])
        page.goto(base + '/admin/users')
        page.locator('.admin-table, .admin-table-wrap').first.wait_for()
        page.goto(base + page.locator('a.admin-link').filter(has_text='測試').first.get_attribute('href'))
        page.locator('#admin-conversation-list button').first.click()
        thread = page.locator('#admin-thread')
        expect(thread).to_contain_text('<script>')
        assert thread.locator('script, img, iframe').count() == 0
        assert page.evaluate('window.__pwned === undefined')
        assert errors == []
