"""Opt-in browser flows for document submissions (phase 7): submit → publish, revise → reject → resubmit,
and the admin document page. Fake provider only: real HTTP, seeded temporary SQLite, no network."""
import os
import re

import pytest
from tests.test_chat import chat_app, new_user  # noqa: F401  fixtures
from tests.test_chat_browser import ARTIFACTS, browser_page, serve

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1',
                                reason='Set RUN_BROWSER_TESTS=1 for local headless browser checks')
UPLOAD = ('---\ntitle: 收卷區溫濕度紀錄說明\nsummary: 收卷區溫濕度的記錄頻率、表單與異常通報。\ncategory: SOP\n---\n\n'
          '## 1. 目的\n\n確保收卷區溫濕度在範圍內。\n\n## 2. 記錄方式\n\n- 每 2 小時記錄一次\n- 超出範圍通報班長，詳見 https://wiki.example.invalid\n')


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_submission_review_flows(chat_app, width, height, tmp_path):
    from playwright.sync_api import expect
    client, db, _, _ = chat_app
    _, user = new_user(chat_app)
    _, admin = new_user(chat_app, role='admin')
    upload = tmp_path / 'humidity.md'
    upload.write_text(UPLOAD, encoding='utf-8')
    # Each width revises its own document: the module's database is shared and a resubmission stays pending.
    revise_code = 'MFG-SOP-003' if width > 600 else 'MFG-SOP-004'
    target = db.query('SELECT id,title FROM documents WHERE document_code=%s', (revise_code,), one=True)
    revise_id = target['id']
    shot = lambda page, name: page.screenshot(path=str(ARTIFACTS / f'submission-{name}-{width}.png'), full_page=True)  # noqa: E731
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    def no_horizontal_scroll(page):
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), page.url

    with serve(client.app) as base, browser_page(width, height) as (page, errors):
        page.on('dialog', lambda dialog: dialog.accept())

        def sign_in(headers):
            page.goto(base + '/static/css/index.css')
            page.evaluate('t => localStorage.setItem("knowpilot_token", t)', headers['Authorization'].split()[1])

        # ---- 1. submit a new document from an uploaded .md
        sign_in(user)
        page.goto(base + '/documents')
        page.get_by_role('link', name='新增文件').click()
        expect(page.get_by_role('heading', name='新增文件')).to_be_visible()
        expect(page.locator('[data-revise-only]').first).to_be_hidden()
        page.locator('[data-upload-input]').set_input_files(str(upload))
        expect(page.get_by_label('標題', exact=True)).to_have_value('收卷區溫濕度紀錄說明')
        expect(page.locator('[data-preview] h2').first).to_have_text('1. 目的')
        no_horizontal_scroll(page)
        shot(page, 'form')
        page.get_by_role('button', name='送出審核').click()
        expect(page).to_have_url(re.compile(r'/submissions\?sent=\d+$'))
        card = page.locator('.submission-card').first
        expect(card).to_contain_text('審核中')
        expect(card).to_contain_text('待核發')
        shot(page, 'mine-pending')

        # ---- 2. the admin publishes it
        sign_in(admin)
        page.goto(base + '/admin/submissions')
        # On a phone the sidebar is collapsed, so check the badge was filled rather than on screen.
        expect(page.locator('[data-admin-badge]')).not_to_have_attribute('hidden', '')
        expect(page.locator('[data-admin-badge]')).to_have_text(re.compile(r'^[1-9]\d*$'))
        page.get_by_role('link', name='收卷區溫濕度紀錄說明').first.click()
        expect(page.locator('[data-flags]')).to_contain_text('內容含外部連結')
        expect(page.locator('[data-diff]')).to_be_hidden()
        no_horizontal_scroll(page)
        shot(page, 'review-create')
        page.get_by_role('button', name='發布').click()
        expect(page.locator('[data-review-status]')).to_contain_text('已發布：MFG-SOP-')
        published = page.locator('[data-review-status] a')
        published.click()
        expect(page.locator('#revision-list')).to_contain_text('1.0')
        expect(page.get_by_role('button', name='下架文件')).to_be_visible()
        shot(page, 'admin-document')

        # ---- 3. a revision is rejected, edited and sent again
        sign_in(user)
        page.goto(f'{base}/documents/{revise_id}')
        page.get_by_role('link', name='提出修改').click()
        expect(page.locator('[data-revise-meta]')).to_contain_text(revise_code)
        page.get_by_label('修改說明').fill('補充清潔後的確認項目')
        content = page.get_by_label('內容（Markdown，至少一個「## 」開頭的章節標題）')
        content.fill(content.input_value() + '\n\n## 9. 清潔後確認\n\n清潔後以手電筒斜照確認刮刀無殘膠。')
        page.get_by_role('button', name='送出審核').click()
        expect(page.locator('.submission-card').first).to_contain_text('修改')
        page.goto(f'{base}/documents/{revise_id}')
        expect(page.locator('[data-revision-pending]')).to_be_visible()

        sign_in(admin)
        page.goto(base + '/admin/submissions')
        page.locator('#submission-queue a', has_text=target['title']).first.click()
        expect(page.locator('#content-diff .is-add', has_text='清潔後確認')).to_have_count(1)
        shot(page, 'review-diff')
        page.get_by_label('退回原因（退回時必填）').fill('請寫明殘膠的判定標準')
        page.get_by_role('button', name='退回').click()
        expect(page.locator('[data-review-status]')).to_contain_text('已退回')

        sign_in(user)
        page.goto(base + '/submissions')
        rejected = page.locator('.submission-card.is-rejected').first
        expect(rejected).to_contain_text('請寫明殘膠的判定標準')
        shot(page, 'mine-rejected')
        rejected.get_by_role('link', name='修改後再送審').click()
        expect(page.locator('[data-review-note]')).to_contain_text('請寫明殘膠的判定標準')
        content = page.get_by_label('內容（Markdown，至少一個「## 」開頭的章節標題）')
        content.fill(content.input_value() + '指甲刮過不留痕即合格。')
        page.get_by_role('button', name='送出審核').click()
        expect(page.locator('.submission-card').first).to_contain_text('審核中')
        no_horizontal_scroll(page)
        assert errors == []
