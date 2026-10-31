"""Document submissions: create / revise, review, publish and archive.

documents holds only published and archived rows, so document_scope(), the document center and
the RAG index never see a draft. A submission becomes a document row only when an admin approves
it, inside one transaction that also issues the code or bumps the version and keeps the previous
text in document_revisions. Callers rebuild the RAG index after the commit (rag.index.rebuild).
"""
import re
import sqlite3

import mysql.connector

from llm.guard import taipei_day_bounds
from rag.glossary import GLOSSARY_DOC_CODE
from repositories.chat import dumps, loads, utc_now

CATEGORY_CODES = {'SOP': 'SOP', '規章': 'RULE', '表單': 'FORM', '報告': 'RPT', '教育訓練': 'TRN', '規格': 'SPEC',
                  '計畫': 'PLAN', '8D 報告': '8D', '變更通知': 'ECN'}
# Documents that reach more than their own readers: the glossary is parsed into every department's router prompt.
LOCKED_CODES = {GLOSSARY_DOC_CODE}
MAX_PENDING, MAX_PER_DAY = 5, 10
FIELDS = ('title', 'summary', 'content', 'category', 'visibility')
SUBMISSION_SELECT = '''SELECT s.*,u.name AS submitter_name,u.email AS submitter_email,dep.code AS department_code,
                              dep.name AS department_name,d.document_code,d.version AS live_version,d.status AS document_status,
                              r.name AS reviewer_name
                       FROM document_submissions s JOIN users u ON u.id=s.submitted_by
                       JOIN departments dep ON dep.id=s.department_id
                       LEFT JOIN documents d ON d.id=s.document_id LEFT JOIN users r ON r.id=s.reviewed_by'''


class SubmissionError(Exception):
    """A rule the request broke; status is the HTTP status the API answers with."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def next_version(version, bump):
    """'1.3' → '1.4' (minor) or '2.0' (major)."""
    major, _, minor = str(version).partition('.')
    return f'{int(major) + 1}.0' if bump == 'major' else f'{major}.{int(minor or 0) + 1}'


def _next_code(cursor, department_code, category):
    prefix = f'{department_code}-{CATEGORY_CODES[category]}-'
    cursor.execute('SELECT document_code FROM documents WHERE document_code LIKE %s', (prefix + '%',))
    numbers = [int(m.group(1)) for r in cursor.fetchall() if (m := re.fullmatch(re.escape(prefix) + r'(\d+)', r['document_code']))]
    return f'{prefix}{max(numbers, default=0) + 1:03d}'


# ---- what a user may revise ----------------------------------------------------------------

def editable(doc, user):
    """A published document of the user's department that is open to revision (pending revisions aside)."""
    return (doc is not None and doc['status'] == 'published' and doc['owner_department_id'] == user['department_id']
            and doc['document_code'] not in LOCKED_CODES)


def document_for_revision(db, doc_id):
    return db.query('SELECT id,document_code,status,owner_department_id,version,title,summary,content,category,visibility '
                    'FROM documents WHERE id=%s', (doc_id,), one=True)


def pending_revision(db, doc_id):
    return db.query("SELECT id,submitted_by FROM document_submissions WHERE document_id=%s AND kind='revise' "
                    "AND status='pending'", (doc_id,), one=True)


# ---- the submitter's side ------------------------------------------------------------------

def _check_limits(cursor, user_id, now):
    cursor.execute("SELECT COUNT(*) AS n FROM document_submissions WHERE submitted_by=%s AND status='pending'", (user_id,))
    if cursor.fetchone()['n'] >= MAX_PENDING:
        raise SubmissionError(429, f'同時最多 {MAX_PENDING} 筆待審提交，請等審核完成或撤回後再送')
    start, end = taipei_day_bounds(now)
    cursor.execute('SELECT COUNT(*) AS n FROM document_submissions WHERE submitted_by=%s AND submitted_at>=%s AND submitted_at<%s',
                   (user_id, start, end))
    if cursor.fetchone()['n'] >= MAX_PER_DAY:
        raise SubmissionError(429, f'今天已送出 {MAX_PER_DAY} 次，明天再送吧')


def _lock_revisable(cursor, user, doc_id, exclude_submission=None):
    """Lock the document for a revision and check it is still the user's to revise, with no other pending revision."""
    cursor.execute('SELECT id,document_code,status,owner_department_id,version FROM documents WHERE id=%s FOR UPDATE', (doc_id,))
    doc = cursor.fetchone()
    if not editable(doc, user):
        raise SubmissionError(403, '只能修改自己部門的已發布文件')
    cursor.execute("SELECT id FROM document_submissions WHERE document_id=%s AND kind='revise' AND status='pending' AND id<>%s",
                   (doc_id, exclude_submission or 0))
    if cursor.fetchone():
        raise SubmissionError(409, '這份文件已有修改在審核中')
    return doc


def create(db, user, fields, *, kind, document_id=None, change_note=None, version_bump=None, flags=None):
    """A new pending submission; returns its id. fields: title, summary, content, category, visibility."""
    now = utc_now()
    with db.transaction() as cursor:
        _check_limits(cursor, user['id'], now)
        base_version = None
        if kind == 'revise':
            base_version = _lock_revisable(cursor, user, document_id)['version']
        cursor.execute('INSERT INTO document_submissions(kind,document_id,base_version,department_id,title,summary,content,'
                       'category,visibility,change_note,version_bump,status,submitted_by,submitted_at,flags,created_at,updated_at) '
                       "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s)",
                       (kind, document_id if kind == 'revise' else None, base_version, user['department_id'],
                        *(fields[f] for f in FIELDS), change_note, version_bump if kind == 'revise' else None,
                        user['id'], now, dumps(flags), now, now))
        return cursor.lastrowid


def own(db, user, submission_id):
    """The user's own submission, else None (other people's submissions are not found)."""
    return db.query(SUBMISSION_SELECT + ' WHERE s.id=%s AND s.submitted_by=%s', (submission_id, user['id']), one=True)


def mine(db, user):
    return db.query(SUBMISSION_SELECT + ' WHERE s.submitted_by=%s ORDER BY s.updated_at DESC,s.id DESC', (user['id'],))


def resubmit(db, user, submission_id, fields, *, change_note=None, version_bump=None, flags=None):
    """Edit a rejected submission and send it back to pending. The base version stays: approval still detects
    that the document changed since the user started editing."""
    now = utc_now()
    with db.transaction() as cursor:
        cursor.execute('SELECT * FROM document_submissions WHERE id=%s AND submitted_by=%s FOR UPDATE', (submission_id, user['id']))
        row = cursor.fetchone()
        if row is None:
            raise SubmissionError(404, '找不到這筆提交')
        if row['status'] != 'rejected':
            raise SubmissionError(409, '只有被退回的提交可以修改後再送審')
        if row['department_id'] != user['department_id']:
            raise SubmissionError(403, '切換部門後不能再送審原部門的提交')
        _check_limits(cursor, user['id'], now)
        if row['kind'] == 'revise':
            _lock_revisable(cursor, user, row['document_id'], exclude_submission=submission_id)
        cursor.execute("UPDATE document_submissions SET title=%s,summary=%s,content=%s,category=%s,visibility=%s,change_note=%s,"
                       "version_bump=%s,flags=%s,status='pending',submitted_at=%s,updated_at=%s WHERE id=%s",
                       (*(fields[f] for f in FIELDS), change_note,
                        version_bump if row['kind'] == 'revise' else None, dumps(flags), now, now, submission_id))


def withdraw(db, user, submission_id):
    with db.transaction() as cursor:
        cursor.execute("UPDATE document_submissions SET status='withdrawn',updated_at=%s "
                       "WHERE id=%s AND submitted_by=%s AND status='pending'", (utc_now(), submission_id, user['id']))
        if cursor.rowcount != 1:
            raise SubmissionError(409, '只有審核中的提交可以撤回') if own(db, user, submission_id) else \
                SubmissionError(404, '找不到這筆提交')


def withdraw_pending(cursor, user_id, now):
    """Inside the department-switch transaction: the user's pending submissions belong to the old department."""
    cursor.execute("UPDATE document_submissions SET status='withdrawn',updated_at=%s WHERE submitted_by=%s AND status='pending'",
                   (now, user_id))
    return cursor.rowcount


# ---- the reviewer's side -------------------------------------------------------------------

def queue(db, status='pending'):
    return db.query(SUBMISSION_SELECT + ' WHERE s.status=%s ORDER BY s.submitted_at,s.id', (status,))


def pending_count(db):
    return db.query("SELECT COUNT(*) AS n FROM document_submissions WHERE status='pending'", one=True)['n']


def get(db, submission_id):
    return db.query(SUBMISSION_SELECT + ' WHERE s.id=%s', (submission_id,), one=True)


def _snapshot(cursor, doc, submission_id):
    cursor.execute('INSERT INTO document_revisions(document_id,submission_id,title,summary,content,category,visibility,version,'
                   'document_updated_at,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                   (doc['id'], submission_id, doc['title'], doc['summary'], doc['content'], doc['category'], doc['visibility'],
                    doc['version'], doc['updated_at'], utc_now()))


def approve(db, admin, submission_id):
    """Publish a pending submission. Returns (document id, document code, version). Rebuild the index afterwards."""
    now = utc_now()
    try:
        with db.transaction() as cursor:
            cursor.execute('SELECT s.*,dep.code AS department_code FROM document_submissions s '
                           'JOIN departments dep ON dep.id=s.department_id WHERE s.id=%s FOR UPDATE', (submission_id,))
            sub = cursor.fetchone()
            if sub is None:
                raise SubmissionError(404, '找不到這筆提交')
            if sub['status'] != 'pending':
                raise SubmissionError(409, '只有審核中的提交可以發布')
            department_id = sub['department_id'] if sub['visibility'] == 'department' else None
            if sub['kind'] == 'create':
                code, version = _next_code(cursor, sub['department_code'], sub['category']), '1.0'
                cursor.execute('INSERT INTO documents(document_code,title,summary,content,category,department_id,owner_department_id,'
                               "visibility,version,status,created_by,created_at,updated_at) "
                               "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'published',%s,%s,%s)",
                               (code, sub['title'], sub['summary'], sub['content'], sub['category'], department_id,
                                sub['department_id'], sub['visibility'], version, sub['submitted_by'], now, now))
                document_id = cursor.lastrowid
                cursor.execute('SELECT * FROM documents WHERE id=%s', (document_id,))
                _snapshot(cursor, cursor.fetchone(), submission_id)   # first published text, for the history page
            else:
                cursor.execute('SELECT * FROM documents WHERE id=%s FOR UPDATE', (sub['document_id'],))
                doc = cursor.fetchone()
                if doc['status'] != 'published' or doc['version'] != sub['base_version']:
                    raise SubmissionError(409, f'線上版本已變更（提交時 {sub["base_version"]}，現在 {doc["version"]}'
                                               f'{"，已下架" if doc["status"] != "published" else ""}），請退回請提交者依最新版本重新編輯')
                cursor.execute('SELECT id FROM document_revisions WHERE document_id=%s AND version=%s', (doc['id'], doc['version']))
                if not cursor.fetchone():
                    _snapshot(cursor, doc, None)   # the text being replaced (a seeded document has no earlier revision)
                code, version, document_id = doc['document_code'], next_version(doc['version'], sub['version_bump']), doc['id']
                cursor.execute('UPDATE documents SET title=%s,summary=%s,content=%s,category=%s,visibility=%s,department_id=%s,'
                               'version=%s,updated_at=%s WHERE id=%s',
                               (sub['title'], sub['summary'], sub['content'], sub['category'], sub['visibility'], department_id,
                                version, now, document_id))
                cursor.execute('SELECT * FROM documents WHERE id=%s', (document_id,))
                _snapshot(cursor, cursor.fetchone(), submission_id)
            cursor.execute("UPDATE document_submissions SET status='approved',document_id=%s,reviewed_by=%s,reviewed_at=%s,"
                           'updated_at=%s WHERE id=%s', (document_id, admin['id'], now, now, submission_id))
            return document_id, code, version
    except (sqlite3.IntegrityError, mysql.connector.IntegrityError):
        # Two approvals raced for the same code; nothing was written.
        raise SubmissionError(409, '文件代碼衝突，請重新發布')


def reject(db, admin, submission_id, note):
    now = utc_now()
    with db.transaction() as cursor:
        cursor.execute("UPDATE document_submissions SET status='rejected',review_note=%s,reviewed_by=%s,reviewed_at=%s,updated_at=%s "
                       "WHERE id=%s AND status='pending'", (note, admin['id'], now, now, submission_id))
        if cursor.rowcount != 1:
            raise SubmissionError(409, '只有審核中的提交可以退回') if get(db, submission_id) else SubmissionError(404, '找不到這筆提交')


def archive(db, admin, document_id):
    with db.transaction() as cursor:
        cursor.execute("UPDATE documents SET status='archived',updated_at=%s WHERE id=%s AND status='published'",
                       (utc_now(), document_id))
        if cursor.rowcount != 1:
            raise SubmissionError(404, '找不到這份已發布的文件')


def revisions(db, document_id):
    return db.query('SELECT r.id,r.submission_id,r.title,r.version,r.category,r.visibility,r.document_updated_at,r.created_at,'
                    's.change_note,s.kind,u.name AS submitter_name FROM document_revisions r '
                    'LEFT JOIN document_submissions s ON s.id=r.submission_id LEFT JOIN users u ON u.id=s.submitted_by '
                    'WHERE r.document_id=%s ORDER BY r.id DESC', (document_id,))


def submission_flags(row):
    return loads(row['flags']) or []
