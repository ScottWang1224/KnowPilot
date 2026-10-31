"""Admin review of document submissions: queue, detail with a diff against the live text, publish, reject,
archive and revision history (require_admin).

Publishing and archiving change what document_scope() returns. The RAG index is rebuilt right after
the commit; the router's catalog and glossary are read from the database on every question anyway.
The index lives in process memory, which is why the app runs a single uvicorn worker.
"""
import difflib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from core.dependencies import require_admin
from core.views import utc_iso
from rag import index
from repositories import content, submissions
from repositories.submissions import SubmissionError
from routers.portal import document_data
from routers.submissions import raise_for, submission_data
from schemas.submission import ReviewNote

log = logging.getLogger('knowpilot.submissions')
router = APIRouter(prefix='/api/admin', dependencies=[Depends(require_admin)])
COMPARED = (('title', '標題'), ('summary', '摘要'), ('category', '分類'), ('visibility', '可見範圍'))


def rebuild_index(db):
    """After a publish or archive commit. A failure is logged; the next rebuild (or restart) picks the change up."""
    try:
        index.rebuild(db)
    except Exception:
        log.exception('RAG index rebuild failed after a document change')


def diff(live, submitted):
    """Changed fields and a line diff of the content, both as plain data for the page to render as text."""
    fields = [{'field': key, 'label': label, 'before': live[key], 'after': submitted[key]}
              for key, label in COMPARED if live[key] != submitted[key]]
    lines = []
    for line in difflib.unified_diff(live['content'].splitlines(), submitted['content'].splitlines(), lineterm='', n=2):
        if line.startswith(('---', '+++')):
            continue
        kind = 'hunk' if line.startswith('@@') else {'+': 'add', '-': 'del'}.get(line[:1], 'same')
        lines.append({'type': kind, 'text': line if kind == 'hunk' else line[1:]})
    return {'fields': fields, 'lines': lines}


@router.get('/submissions')
def review_queue(request: Request, status: Annotated[str, Query(pattern='^(pending|approved|rejected|withdrawn)$')] = 'pending'):
    db = request.app.state.db
    return {'data': [submission_data(r, with_flags=True) for r in submissions.queue(db, status)],
            'pending_count': submissions.pending_count(db)}


@router.get('/submissions/count')
def pending_count(request: Request):
    return {'data': {'pending': submissions.pending_count(request.app.state.db)}}


@router.get('/submissions/{submission_id}')
def review_detail(request: Request, submission_id: Annotated[int, Path(ge=1)]):
    db = request.app.state.db
    row = submissions.get(db, submission_id)
    if row is None:
        raise HTTPException(404, '找不到這筆提交')
    data = submission_data(row, with_content=True, with_flags=True)
    data['submitter_email'] = row['submitter_email']
    data['diff'] = None
    if row['kind'] == 'revise':
        live = submissions.document_for_revision(db, row['document_id'])
        data['diff'] = diff(live, row)
        data['conflict'] = live['status'] != 'published' or live['version'] != row['base_version']
    return {'data': data}


@router.post('/submissions/{submission_id}/approve')
def approve(request: Request, submission_id: Annotated[int, Path(ge=1)], admin=Depends(require_admin)):
    db = request.app.state.db
    try:
        document_id, code, version = submissions.approve(db, admin, submission_id)
    except SubmissionError as error:
        raise_for(error)
    rebuild_index(db)
    return {'data': {'document_id': document_id, 'document_code': code, 'version': version}}


@router.post('/submissions/{submission_id}/reject')
def reject(request: Request, submission_id: Annotated[int, Path(ge=1)], body: ReviewNote, admin=Depends(require_admin)):
    note = body.review_note.strip()
    if not note:
        raise HTTPException(422, '退回時必須填寫原因')
    try:
        submissions.reject(request.app.state.db, admin, submission_id, note)
    except SubmissionError as error:
        raise_for(error)
    return {'data': submission_data(submissions.get(request.app.state.db, submission_id))}


@router.post('/documents/{doc_id}/archive')
def archive(request: Request, doc_id: Annotated[int, Path(ge=1)], admin=Depends(require_admin)):
    db = request.app.state.db
    try:
        submissions.archive(db, admin, doc_id)
    except SubmissionError as error:
        raise_for(error)
    rebuild_index(db)
    return {'data': document_data(content.admin_document(db, doc_id))}


@router.get('/documents/{doc_id}/revisions')
def revisions(request: Request, doc_id: Annotated[int, Path(ge=1)]):
    db = request.app.state.db
    if content.admin_document(db, doc_id) is None:
        raise HTTPException(404, '找不到這份文件')
    return {'data': [{'id': r['id'], 'version': r['version'], 'title': r['title'], 'category': r['category'],
                      'visibility': r['visibility'], 'kind': r['kind'], 'change_note': r['change_note'],
                      'submitter': r['submitter_name'], 'published_at': utc_iso(r['document_updated_at'])}
                     for r in submissions.revisions(db, doc_id)]}
