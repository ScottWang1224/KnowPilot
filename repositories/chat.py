"""Conversations, messages, LLM call records and feedback.

What a user may see of their own conversations is decided in one place:
visible_conversation() / visible_conversations(). Admin readers live at the bottom.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import mysql.connector

from llm.guard import taipei_day_bounds
from repositories.content import announcement_scope, document_scope

TITLE_CHARS = 30
VISIBLE = 'c.user_id=%s AND c.hidden_at IS NULL AND c.department_id=%s'


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def loads(value):
    return json.loads(value) if value else None


def dumps(value):
    return None if value is None else json.dumps(value, ensure_ascii=False)


# ---- the user's own conversations ---------------------------------------------------------

def visible_conversation(db, user, conversation_id):
    """The user's conversation when it is theirs, not hidden and from their current department; else None."""
    return db.query('SELECT c.* FROM conversations c WHERE c.id=%s AND ' + VISIBLE,
                    (conversation_id, user['id'], user['department_id']), one=True)


def visible_conversations(db, user):
    return db.query('SELECT c.id,c.title,c.created_at,c.updated_at FROM conversations c WHERE ' + VISIBLE
                    + ' ORDER BY c.updated_at DESC,c.id DESC', (user['id'], user['department_id']))


def create_conversation(db, user, first_message):
    now = utc_now()
    with db.transaction() as cursor:
        cursor.execute('INSERT INTO conversations(user_id,department_id,title,created_at,updated_at) VALUES(%s,%s,%s,%s,%s)',
                       (user['id'], user['department_id'], first_message.strip()[:TITLE_CHARS], now, now))
        conversation_id = cursor.lastrowid
    return db.query('SELECT * FROM conversations WHERE id=%s', (conversation_id,), one=True)


def add_user_message(db, conversation_id, text):
    now = utc_now()
    with db.transaction() as cursor:
        cursor.execute("INSERT INTO messages(conversation_id,role,content,history_text,created_at) VALUES(%s,'user',%s,%s,%s)",
                       (conversation_id, text, text, now))
        message_id = cursor.lastrowid
        cursor.execute('UPDATE conversations SET updated_at=%s WHERE id=%s', (now, conversation_id))
    return message_id


def add_assistant_message(db, conversation_id, *, content, history_text, kind, payload, trace, state=None,
                          clarify_turns=None):
    """Write the reply and, unless state/clarify_turns are None (errors), the conversation's new routing state."""
    now = utc_now()
    with db.transaction() as cursor:
        cursor.execute("INSERT INTO messages(conversation_id,role,content,history_text,kind,payload,trace,created_at) "
                       "VALUES(%s,'assistant',%s,%s,%s,%s,%s,%s)",
                       (conversation_id, content, history_text, kind, dumps(payload), dumps(trace), now))
        message_id = cursor.lastrowid
        if clarify_turns is None:
            cursor.execute('UPDATE conversations SET updated_at=%s WHERE id=%s', (now, conversation_id))
        else:
            cursor.execute('UPDATE conversations SET state=%s,clarify_turns=%s,updated_at=%s WHERE id=%s',
                           (dumps(state), clarify_turns, now, conversation_id))
    return message_id


def history(db, conversation_id, before_id):
    """Prompt history from messages before `before_id`: user/assistant pairs, skipping pairs whose reply was an error."""
    rows = db.query('SELECT role,kind,history_text FROM messages WHERE conversation_id=%s AND id<%s ORDER BY id',
                    (conversation_id, before_id))
    turns = []
    for question, reply in zip(rows, rows[1:]):
        if question['role'] == 'user' and reply['role'] == 'assistant' and reply['kind'] != 'error' and reply['history_text']:
            turns += [{'role': 'user', 'content': question['history_text']},
                      {'role': 'assistant', 'content': reply['history_text']}]
    return turns


def messages(db, conversation_id):
    return db.query('SELECT * FROM messages WHERE conversation_id=%s ORDER BY id', (conversation_id,))


def hide(db, user, conversation_id):
    """Hide the user's visible conversation. Returns False when it is not visible to them."""
    with db.transaction() as cursor:
        cursor.execute("UPDATE conversations SET hidden_at=%s,hidden_reason='user' WHERE id=%s AND "
                       + VISIBLE.replace('c.', ''), (utc_now(), conversation_id, user['id'], user['department_id']))
        return cursor.rowcount == 1


# ---- feedback ----------------------------------------------------------------------------------

def answer_message(db, user, message_id):
    """An answer message in one of the user's visible conversations, else None."""
    return db.query("SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                    "WHERE m.id=%s AND m.kind='answer' AND " + VISIBLE, (message_id, user['id'], user['department_id']), one=True)


def _update_feedback(db, user, message_id, rating, comment, now):
    return db.execute('UPDATE feedback SET rating=%s,comment=%s,updated_at=%s WHERE message_id=%s AND user_id=%s',
                      (rating, comment, now, message_id, user['id'])) > 0


def set_feedback(db, user, message_id, rating, comment):
    now = utc_now()
    if _update_feedback(db, user, message_id, rating, comment, now):
        return
    try:
        db.execute('INSERT INTO feedback(message_id,user_id,rating,comment,created_at,updated_at) '
                   'VALUES(%s,%s,%s,%s,%s,%s)', (message_id, user['id'], rating, comment, now, now))
    except (sqlite3.IntegrityError, mysql.connector.IntegrityError):
        # A second click inserted the row first (unique message_id, user_id): update that row instead.
        _update_feedback(db, user, message_id, rating, comment, now)


def feedback_for(db, message_ids, user_id=None):
    """{message_id: feedback row} for the given messages (optionally only this user's feedback)."""
    if not message_ids:
        return {}
    marks = ','.join(['%s'] * len(message_ids))
    sql, params = f'SELECT * FROM feedback WHERE message_id IN ({marks})', tuple(message_ids)
    if user_id is not None:
        sql, params = sql + ' AND user_id=%s', params + (user_id,)
    return {r['message_id']: r for r in db.query(sql, params)}


# ---- LLM call records and quota ------------------------------------------------------------

def record_call(db, *, user_id, message_id, purpose, result):
    db.execute('INSERT INTO llm_calls(user_id,message_id,purpose,provider,model,prompt_hash,input_tokens,output_tokens,'
               'latency_ms,attempts,cost_usd,budget_cost_usd,error,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
               (user_id, message_id, purpose, result.provider, result.model, result.prompt_hash or '', result.input_tokens,
                result.output_tokens, result.latency_ms, result.attempts, result.cost_usd, result.budget_cost_usd,
                result.error, utc_now()))


def user_spent_today(db, user_id, now=None):
    start, end = taipei_day_bounds(now)
    row = db.query('SELECT COALESCE(SUM(budget_cost_usd),0) AS total FROM llm_calls '
                   'WHERE user_id=%s AND created_at>=%s AND created_at<%s', (user_id, start, end), one=True)
    return float(row['total'])


def spent_today_by_user(db, now=None):
    start, end = taipei_day_bounds(now)
    rows = db.query('SELECT user_id,SUM(budget_cost_usd) AS total FROM llm_calls '
                    'WHERE created_at>=%s AND created_at<%s GROUP BY user_id', (start, end))
    return {r['user_id']: float(r['total']) for r in rows}


def average_question_cost(db, days=7, now=None):
    """All LLM spending over the last `days` days per finished question, or None without finished questions.

    A question is finished when it got an answer or out_of_scope reply. Clarify turns and refused or
    failed requests add their cost but no question, so a multi-turn question counts once at its full cost.
    """
    since = (now or datetime.now(timezone.utc)).replace(tzinfo=None) - timedelta(days=days)
    spent = db.query('SELECT COALESCE(SUM(budget_cost_usd),0) AS total FROM llm_calls WHERE created_at>=%s',
                     (since,), one=True)['total']
    finished = db.query("SELECT COUNT(*) AS n FROM messages WHERE role='assistant' AND kind IN ('answer','out_of_scope') "
                        'AND created_at>=%s', (since,), one=True)['n']
    return float(spent) / finished if finished else None


# ---- citation links ------------------------------------------------------------------------

def link_targets(db, user, doc_codes, *, admin_view=False):
    """{doc_code: url path} for the codes this user may open; unseen codes get no entry.

    admin_view (admin pages behind require_admin): every published document, linked to the admin
    document page instead of /documents, so document_scope() itself never widens for admins.
    """
    codes = sorted(set(doc_codes))
    documents = [c for c in codes if not c.startswith('ANN:')]
    links = {}
    if documents:
        scope, params = ("d.status='published'", ()) if admin_view else document_scope(user)
        prefix = '/admin/documents/' if admin_view else '/documents/'
        marks = ','.join(['%s'] * len(documents))
        for r in db.query(f'SELECT d.id,d.document_code FROM documents d WHERE {scope} AND d.document_code IN ({marks})',
                          params + tuple(documents)):
            links[r['document_code']] = f'{prefix}{r["id"]}'
    if any(c.startswith('ANN:') for c in codes):
        scope, params = announcement_scope()
        for r in db.query('SELECT a.id,a.seed_key FROM announcements a WHERE ' + scope, params):
            code = f'ANN:{r["seed_key"] or "id-" + str(r["id"])}'
            if code in codes:
                links[code] = f'/announcements/{r["id"]}'
    return links


# ---- admin readers -------------------------------------------------------------------------

def user_activity(db):
    """{user_id: {'conversations': n, 'last_active_at': time of the last question}}."""
    rows = db.query("SELECT c.user_id,COUNT(DISTINCT c.id) AS conversations,"
                    "MAX(CASE WHEN m.role='user' THEN m.created_at END) AS last_active_at "
                    "FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id GROUP BY c.user_id")
    return {r['user_id']: r for r in rows}


def user_conversations(db, user_id):
    """Every conversation of the user, hidden ones included."""
    return db.query('SELECT c.*,d.name AS department_name,(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) '
                    'AS message_count FROM conversations c JOIN departments d ON d.id=c.department_id '
                    'WHERE c.user_id=%s ORDER BY c.updated_at DESC,c.id DESC', (user_id,))


def conversation_with_owner(db, conversation_id):
    return db.query('SELECT c.*,d.name AS department_name,u.name AS user_name,u.email AS user_email '
                    'FROM conversations c JOIN departments d ON d.id=c.department_id JOIN users u ON u.id=c.user_id '
                    'WHERE c.id=%s', (conversation_id,), one=True)
