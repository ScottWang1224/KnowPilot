"""Opt-in browser checks for agent memory (phase 5): profile editor, chat memory card, used-memory tag, admin page.
Fake provider only: real HTTP, seeded temporary SQLite, no network."""
import json
import os

import pytest
from tests.test_chat import answer_reply, chat_app, fakes, first_chunk, new_user, route_answer  # noqa: F401  fixtures
from tests.test_chat_browser import ARTIFACTS, browser_page, serve

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1',
                                reason='Set RUN_BROWSER_TESTS=1 for local headless browser checks')


def stored(db, user_id):
    return {r['mem_key']: r['mem_value'] for r in db.query('SELECT * FROM user_memories WHERE user_id=%s', (user_id,))}


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_memory_flow(chat_app, fakes, width, height):
    from playwright.sync_api import expect
    client, db, _, _ = chat_app
    user_id, headers = new_user(chat_app)
    _, admin = new_user(chat_app, role='admin')
    chunk = first_chunk(db, 'MFG-SOP-003')
    remember = json.dumps({'action': 'remember', 'intent_summary': '記住產線', 'state': {}, 'selected': [],
                           'memory_proposal': [{'key': 'line', 'value': 'CL-02'}]}, ensure_ascii=False)
    uses = json.dumps({**json.loads(route_answer(chunk, {'line': 'CL-02'})), 'used_memory': ['line'],
                       'memory_proposal': [{'key': 'shift', 'value': '夜班'}]}, ensure_ascii=False)
    fakes([remember, uses], [answer_reply([chunk.chunk_id], text='依 CL-02 微凹版塗布頭清潔 SOP 操作')])
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    shot = lambda page, name: page.screenshot(path=str(ARTIFACTS / f'memory-{name}-{width}.png'), full_page=True)  # noqa: E731

    def no_horizontal_scroll(page):
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), page.url

    def sign_in(page, auth):
        # A static file on the same origin: /login would redirect a signed-in user while the token is being set.
        page.goto(base + '/static/css/index.css')
        page.evaluate('token => localStorage.setItem("knowpilot_token", token)', auth['Authorization'].split()[1])

    with serve(client.app) as base, browser_page(width, height) as (page, errors):
        # ---- profile: add, change, delete, invalid value, clear all
        sign_in(page, headers)
        page.goto(base + '/profile')
        section = page.locator('#memory')
        expect(section.get_by_role('heading', name='我的記憶')).to_be_visible()
        expect(section.locator('.memory-row')).to_have_count(5)
        line = section.get_by_label('產線', exact=True)
        line.select_option('CL-01')
        expect(section.locator('[data-memory-status]')).to_have_text('已記住產線：CL-01。')
        line.select_option('CL-02')
        expect(section.locator('[data-memory-status]')).to_have_text('已記住產線：CL-02。')
        section.get_by_label('班別', exact=True).select_option('夜班')
        section.get_by_label('回答方式', exact=True).select_option('簡短')
        expect(section.locator('[data-memory-status]')).to_have_text('已記住回答方式：簡短。')
        assert stored(db, user_id) == {'line': 'CL-02', 'shift': '夜班', 'answer_style': '簡短'}
        section.get_by_role('button', name='刪除班別').click()
        expect(section.locator('[data-memory-status]')).to_have_text('已刪除班別。')
        db.execute("INSERT INTO user_memories(user_id,mem_key,mem_value,source,created_at,updated_at) "
                   "VALUES(%s,'product','HC-999','manual','2026-10-24 00:00:00','2026-10-24 00:00:00')", (user_id,))
        page.reload()
        expect(section.locator('.memory-row.is-invalid')).to_contain_text('已失效，請重新選擇')
        expect(section.get_by_label('產線', exact=True)).to_have_value('CL-02')
        expect(section.locator('.memory-row', has_text='產線')).to_contain_text('手動設定')
        no_horizontal_scroll(page)
        shot(page, 'profile')
        page.once('dialog', lambda dialog: dialog.accept())
        section.get_by_role('button', name='全部清除').click()
        expect(section.locator('[data-memory-status]')).to_have_text('已清除全部記憶。')
        assert stored(db, user_id) == {}
        expect(section.get_by_role('button', name='全部清除')).to_be_hidden()

        # ---- chat: memory card → save; next answer shows the used-memory tag and a second card → skip
        page.goto(base + '/assistant')
        box = page.get_by_label('輸入問題')
        box.fill('請記住我負責 CL-02')
        box.press('Enter')
        card = page.locator('.memory-card').first
        expect(card).to_contain_text('產線：CL-02')
        assert stored(db, user_id) == {}
        page.wait_for_timeout(800)
        shot(page, 'card')
        card.get_by_role('button', name='儲存').click()
        expect(card.locator('.memory-card-done')).to_have_text('已記住')
        assert stored(db, user_id) == {'line': 'CL-02'}

        box.fill('以後都是夜班，塗布頭要怎麼清潔？')
        box.press('Enter')
        answer = page.locator('.message-assistant').last
        expect(answer.locator('.memory-used')).to_contain_text('已套用你的記憶：產線 CL-02')
        expect(answer.locator('.memory-used a')).to_have_attribute('href', '/profile#memory')
        expect(answer.locator('.answer-point')).to_contain_text('依 CL-02 微凹版塗布頭清潔 SOP 操作')
        expect(answer.locator('.memory-card')).to_contain_text('班別：夜班')
        no_horizontal_scroll(page)
        page.wait_for_timeout(1500)   # let the reply blocks finish fading in
        shot(page, 'used')
        answer.locator('.memory-card').get_by_role('button', name='略過').click()
        expect(answer.locator('.memory-card')).to_have_text('已略過，不會記住。')
        assert stored(db, user_id) == {'line': 'CL-02'}

        page.reload()   # the confirmed card stays confirmed after reloading the conversation
        expect(page.locator('.memory-card').first.locator('.memory-card-done')).to_have_text('已記住')

        # ---- admin: read-only memory table on the user page
        sign_in(page, admin)
        page.goto(base + f'/admin/users/{user_id}')
        memory = page.locator('.admin-memories')
        expect(memory).to_contain_text('產線')
        expect(memory).to_contain_text('CL-02')
        expect(memory).to_contain_text('對話中設定')
        no_horizontal_scroll(page)
        shot(page, 'admin')
        assert errors == []
