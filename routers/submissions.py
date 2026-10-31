"""Document submissions API for every signed-in user: submit a new document or a revision of one's own
department's document, edit a rejected submission, withdraw, and read one's own submissions.

Submitted text is untrusted: once approved it reaches the RAG index and the LLM prompts. The content
rules below are enforced (422); the flags are only warnings for the reviewer, whose approval is the gate.
"""
import re
import unicodedata
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Path, Request, UploadFile

from core.dependencies import get_current_user
from core.views import utc_iso
from repositories import submissions
from repositories.chat import loads
from repositories.submissions import CATEGORY_CODES, SubmissionError
from schemas.submission import SubmissionCreate, SubmissionFields
from scripts.seed import parse_front_matter

router = APIRouter(prefix='/api')

LIMITS = {'title': (1, 200, '標題'), 'summary': (1, 500, '摘要'), 'content': (1, 100_000, '內容')}
MAX_UPLOAD_BYTES = 200 * 1024
HEADING = re.compile(r'^## \S', re.MULTILINE)
STATUS_LABELS = {'pending': '審核中', 'approved': '已發布', 'rejected': '已退回', 'withdrawn': '已撤回'}
PENDING_CODE = '待核發'
# Phrases that address the model rather than the reader, after the patterns in data/eval/security.yaml.
# Matched after NFKC, lower-casing and removing all whitespace (zero-width spaces included).
INJECTION_PHRASES = ('忽略以上', '忽略之前', '忽略前面', '忽略所有', '忽略上述', '忽略規則', '無視以上', 'ignoreprevious',
                     'ignoreallprevious', 'ignoretheabove', 'disregardprevious', 'systemprompt', '系統提示詞', '提示詞識別碼',
                     '你現在是', '從現在開始你是', '不受限制的助理', '給ai助理', 'ai助理請', 'ai助理回答', '給模型', '路由請',
                     '[system]', '</question>', '<sources>', '<memory>', '<state>', 'assistant:', 'assistant：', 'jailbreak')
LINK = re.compile(r"(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]*[A-Za-z0-9/#=_\-~]", re.IGNORECASE)
HTML_TAG = re.compile(r'<\s*/?\s*[a-zA-Z][^>]*>')


# ---- content rules and flags ---------------------------------------------------------------

def clean(fields):
    """Strip the short fields and normalise line endings, so the rules see what will be stored."""
    data = fields.model_dump()
    for key in ('title', 'summary', 'category', 'change_note'):
        if data.get(key) is not None:
            data[key] = data[key].strip()
    data['content'] = data['content'].replace('\r\n', '\n').replace('\r', '\n').strip()
    return data


def problems(data, kind):
    found = []
    for key, (low, high, label) in LIMITS.items():
        if not low <= len(data[key]) <= high:
            found.append(f'{label}需要 {low}～{high:,} 字')
    if data['category'] not in CATEGORY_CODES:
        found.append('分類必須是：' + '、'.join(CATEGORY_CODES))
    if data['content'] and not HEADING.search(data['content']):
        found.append('內容至少需要一個「## 」開頭的章節標題（AI 助理依章節切段檢索）')
    if kind == 'revise':
        if not data.get('change_note'):
            found.append('修改說明為必填')
        if data.get('version_bump') not in ('minor', 'major'):
            found.append('請選擇進版方式（小改或大改）')
    return found


def validate(fields, kind):
    data = clean(fields)
    found = problems(data, kind)
    if found:
        raise HTTPException(422, '；'.join(found))
    return data


def squeeze(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text).replace('​', '')).lower()


def flags_for(db, data, department_id, document_id=None):
    """Warnings for the reviewer; they never block a submission."""
    flags = []
    text = squeeze('\n'.join((data['title'], data['summary'], data['content'])))
    hits = sorted({p for p in INJECTION_PHRASES if p in text})
    if hits:
        flags.append({'code': 'injection', 'message': '疑似寫給 AI 的指令', 'detail': '、'.join(hits)})
    raw = '\n'.join((data['summary'], data['content']))
    if LINK.search(raw):
        flags.append({'code': 'link', 'message': '內容含外部連結', 'detail': '、'.join(sorted(set(LINK.findall(raw))))})
    tags = sorted(set(HTML_TAG.findall(raw)))
    if tags:
        flags.append({'code': 'html', 'message': '內容含 HTML 標籤', 'detail': '、'.join(tags[:5])})
    same = db.query("SELECT document_code FROM documents WHERE owner_department_id=%s AND title=%s AND status='published' "
                    'AND id<>%s', (department_id, data['title'], document_id or 0))
    if same:
        flags.append({'code': 'duplicate_title', 'message': '同部門已有相同標題的文件',
                      'detail': '、'.join(r['document_code'] for r in same)})
    return flags


# ---- responses -----------------------------------------------------------------------------

def submission_data(row, *, with_content=False, with_flags=False):
    data = {'id': row['id'], 'kind': row['kind'], 'status': row['status'], 'status_label': STATUS_LABELS[row['status']],
            'document_id': row['document_id'], 'document_code': row['document_code'] or PENDING_CODE,
            'title': row['title'], 'summary': row['summary'], 'category': row['category'], 'visibility': row['visibility'],
            'department': row['department_name'], 'change_note': row['change_note'], 'version_bump': row['version_bump'],
            'base_version': row['base_version'], 'live_version': row['live_version'],
            'submitted_at': utc_iso(row['submitted_at']), 'reviewed_at': utc_iso(row['reviewed_at']),
            'review_note': row['review_note'], 'submitter': row['submitter_name']}
    if with_content:
        data['content'] = row['content']
    if with_flags:
        data['flags'] = loads(row['flags']) or []
    return data


def raise_for(error):
    raise HTTPException(error.status, error.message)


# ---- endpoints -----------------------------------------------------------------------------

@router.get('/submissions')
def my_submissions(request: Request, user=Depends(get_current_user)):
    return {'data': [submission_data(r) for r in submissions.mine(request.app.state.db, user)]}


@router.get('/submissions/{submission_id}')
def my_submission(request: Request, submission_id: Annotated[int, Path(ge=1)], user=Depends(get_current_user)):
    row = submissions.own(request.app.state.db, user, submission_id)
    if row is None:
        raise HTTPException(404, '找不到這筆提交')
    return {'data': submission_data(row, with_content=True)}


@router.post('/submissions', status_code=201)
def submit(request: Request, body: SubmissionCreate, user=Depends(get_current_user)):
    db = request.app.state.db
    if body.kind == 'revise' and body.document_id is None:
        raise HTTPException(422, '修改提交必須指定文件')
    data = validate(body, body.kind)
    flags = flags_for(db, data, user['department_id'], body.document_id if body.kind == 'revise' else None)
    try:
        submission_id = submissions.create(db, user, data, kind=body.kind, document_id=body.document_id,
                                           change_note=data.get('change_note'), version_bump=data.get('version_bump'),
                                           flags=flags)
    except SubmissionError as error:
        raise_for(error)
    return {'data': submission_data(submissions.own(db, user, submission_id), with_content=True)}


@router.put('/submissions/{submission_id}')
def resubmit(request: Request, submission_id: Annotated[int, Path(ge=1)], body: SubmissionFields,
             user=Depends(get_current_user)):
    db = request.app.state.db
    row = submissions.own(db, user, submission_id)
    if row is None:
        raise HTTPException(404, '找不到這筆提交')
    data = validate(body, row['kind'])
    flags = flags_for(db, data, user['department_id'], row['document_id'])
    try:
        submissions.resubmit(db, user, submission_id, data, change_note=data.get('change_note'),
                             version_bump=data.get('version_bump'), flags=flags)
    except SubmissionError as error:
        raise_for(error)
    return {'data': submission_data(submissions.own(db, user, submission_id), with_content=True)}


@router.post('/submissions/{submission_id}/withdraw')
def withdraw(request: Request, submission_id: Annotated[int, Path(ge=1)], user=Depends(get_current_user)):
    db = request.app.state.db
    try:
        submissions.withdraw(db, user, submission_id)
    except SubmissionError as error:
        raise_for(error)
    return {'data': submission_data(submissions.own(db, user, submission_id))}


@router.post('/submissions/parse-markdown')
async def parse_markdown(file: Annotated[UploadFile, File()], user=Depends(get_current_user)):
    """Read an uploaded .md file into form fields for the editor. Nothing is stored."""
    if not (file.filename or '').lower().endswith('.md'):
        raise HTTPException(422, '只接受 .md 檔')
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(422, '檔案超過 200 KB')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise HTTPException(422, '檔案必須是 UTF-8 編碼')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    fields = {'content': text.strip()}
    if text.startswith('---\n'):
        try:
            meta, body = parse_front_matter(text, file.filename)
        except Exception:
            raise HTTPException(422, '檔案開頭的 front matter 格式不正確')
        meta = meta if isinstance(meta, dict) else {}
        fields = {'content': body}
        for key in ('title', 'summary', 'category', 'visibility'):
            if isinstance(meta.get(key), str):
                fields[key] = meta[key].strip()
        if fields.get('visibility') not in ('company', 'department'):
            fields.pop('visibility', None)
    return {'data': fields}
