"""A user's own memory items (user_memories). Callers validate keys and values with rag.memory first."""
import sqlite3

import mysql.connector

from repositories.chat import dumps, utc_now


def stored(db, user_id):
    """Every stored row of the user, valid or not."""
    return db.query('SELECT mem_key,mem_value,source,source_message_id,created_at,updated_at FROM user_memories '
                    'WHERE user_id=%s ORDER BY mem_key', (user_id,))


def values(db, user_id):
    return {r['mem_key']: r['mem_value'] for r in stored(db, user_id)}


def _update(cursor, user_id, key, value, source, message_id, now):
    cursor.execute('UPDATE user_memories SET mem_value=%s,source=%s,source_message_id=%s,updated_at=%s '
                   'WHERE user_id=%s AND mem_key=%s', (value, source, message_id, now, user_id, key))
    return cursor.rowcount > 0


def put(cursor, user_id, key, value, source, message_id=None):
    """Insert or replace one item inside the caller's transaction (one value per key)."""
    now = utc_now()
    if _update(cursor, user_id, key, value, source, message_id, now):
        return
    cursor.execute('INSERT INTO user_memories(user_id,mem_key,mem_value,source,source_message_id,created_at,updated_at) '
                   'VALUES(%s,%s,%s,%s,%s,%s,%s)', (user_id, key, value, source, message_id, now, now))


def set_value(db, user_id, key, value, source, message_id=None):
    try:
        with db.transaction() as cursor:
            put(cursor, user_id, key, value, source, message_id)
    except (sqlite3.IntegrityError, mysql.connector.IntegrityError):
        # A concurrent write inserted the same key first (unique user_id, mem_key): replace that row.
        with db.transaction() as cursor:
            _update(cursor, user_id, key, value, source, message_id, utc_now())


def save_proposal(db, user_id, message_id, items, payload):
    """Write the confirmed items and the message's updated payload (confirmed: true) together."""
    with db.transaction() as cursor:
        for key, value in items:
            put(cursor, user_id, key, value, 'chat', message_id)
        cursor.execute('UPDATE messages SET payload=%s WHERE id=%s', (dumps(payload), message_id))


def delete(db, user_id, key=None):
    """Delete one key, or every item when key is None. Returns the number of rows deleted."""
    with db.transaction() as cursor:
        if key is None:
            cursor.execute('DELETE FROM user_memories WHERE user_id=%s', (user_id,))
        else:
            cursor.execute('DELETE FROM user_memories WHERE user_id=%s AND mem_key=%s', (user_id, key))
        return cursor.rowcount
