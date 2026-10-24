"""Accounts, departments and atomic, database-backed login throttling."""
import hashlib
from datetime import datetime, timedelta, timezone

USER_SELECT = '''SELECT u.id,u.email,u.name,u.department_id,u.role,u.is_active,u.created_at,
                        d.name AS department_name
                 FROM users u JOIN departments d ON d.id=u.department_id'''
ACTIVE = ' WHERE u.is_active=1 AND d.is_active=1'


def by_id(db,user_id):
    return db.query(USER_SELECT+ACTIVE+' AND u.id=%s',(user_id,),one=True)


def by_email(db,email):
    return db.query(USER_SELECT.replace('SELECT u.id','SELECT u.password_hash,u.id')+ACTIVE+' AND u.email=%s',(email,),one=True)


def create(db,*,email,name,password_hash,department_id,role='user'):
    with db.transaction() as cursor:
        cursor.execute('INSERT INTO users(email,name,password_hash,department_id,role) VALUES(%s,%s,%s,%s,%s)',
                       (email,name,password_hash,department_id,role))
        return cursor.lastrowid


def change_department(db,user_id,department_id):
    """Demo feature: move the user, hide their conversations and delete their memory in one transaction.

    Memory may describe the old department's work, so none of it carries over.
    Returns (conversations hidden, memory items deleted).
    """
    now=datetime.now(timezone.utc).replace(tzinfo=None,microsecond=0)
    with db.transaction() as cursor:
        cursor.execute('UPDATE users SET department_id=%s WHERE id=%s',(department_id,user_id))
        cursor.execute("UPDATE conversations SET hidden_at=%s,hidden_reason='department_change' "
                       'WHERE user_id=%s AND hidden_at IS NULL',(now,user_id))
        hidden=cursor.rowcount
        cursor.execute('DELETE FROM user_memories WHERE user_id=%s',(user_id,))
        return hidden,cursor.rowcount


def all_users(db):
    return db.query(USER_SELECT+' ORDER BY u.id')


def departments(db):
    return db.query('SELECT id,code,name FROM departments WHERE is_active=1 ORDER BY id')


def department_exists(db,department_id):
    return db.query('SELECT id FROM departments WHERE id=%s AND is_active=1',(department_id,),one=True) is not None


def consume_attempt(db,action,address,settings):
    now=datetime.now(timezone.utc).replace(tzinfo=None,microsecond=0)
    key=hashlib.sha256(f'{action}:ip:{address}'.encode()).hexdigest()
    with db.transaction() as cursor:
        cursor.execute('''INSERT IGNORE INTO login_attempts(key_hash,attempts,window_started_at)
                          VALUES(%s,%s,%s)''',(key,0,now))
        cursor.execute('SELECT attempts,window_started_at FROM login_attempts WHERE key_hash=%s FOR UPDATE',(key,))
        row=cursor.fetchone()
        started=row['window_started_at']
        if isinstance(started,str): started=datetime.fromisoformat(started)
        count=row['attempts'] if started > now-timedelta(seconds=settings.login_window) else 0
        if count >= settings.login_limit:
            return False
        if count==0: started=now
        cursor.execute('UPDATE login_attempts SET attempts=%s,window_started_at=%s WHERE key_hash=%s',(count+1,started,key))
    return True
