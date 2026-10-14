"""Employee records and atomic, database-backed login throttling."""
import hashlib
from datetime import datetime, timedelta, timezone

USER_SELECT = '''SELECT u.id,u.employee_no,u.display_name,u.department_id,u.role,
                        u.is_active,u.session_version,d.name AS department_name
                 FROM users u JOIN departments d ON d.id=u.department_id
                 WHERE u.is_active=1 AND d.is_active=1'''


def by_id(db,user_id):
    return db.query(USER_SELECT+' AND u.id=%s',(user_id,),one=True)


def by_employee_no(db,employee_no):
    return db.query(USER_SELECT.replace('u.id,u.employee_no','u.password_hash,u.id,u.employee_no')+
                    ' AND u.employee_no=%s',(employee_no,),one=True)


def revoke_sessions(db,user_id):
    db.execute('UPDATE users SET session_version=session_version+1 WHERE id=%s',(user_id,))


def consume_login_attempt(db,address,settings):
    now=datetime.now(timezone.utc).replace(tzinfo=None,microsecond=0)
    key=hashlib.sha256(('ip:'+address).encode()).hexdigest()
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
