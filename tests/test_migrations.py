import pytest


def test_schema_check_rejects_incompatible_existing_columns():
    from scripts.migrate import validate_table
    class Cursor:
        def execute(self,*args): pass
        def fetchall(self):
            return [{'COLUMN_NAME':'id','COLUMN_TYPE':'varchar(10)','IS_NULLABLE':'NO'}]
    with pytest.raises(RuntimeError,match='departments.id'):
        validate_table(Cursor(),'departments',{'id':('bigint unsigned',False)},(),())


def test_schema_check_rejects_missing_unique_constraint():
    from scripts.migrate import validate_table
    class Cursor:
        def execute(self,sql,*args): self.sql=sql
        def fetchall(self):
            if 'COLUMNS' in self.sql:
                return [{'COLUMN_NAME':'code','COLUMN_TYPE':'varchar(24)','IS_NULLABLE':'NO'}]
            return []
    with pytest.raises(RuntimeError,match='index'):
        validate_table(Cursor(),'departments',{'code':('varchar(24)',False)},(('code',),),())


@pytest.mark.parametrize('definition,row',[
    ('id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT',{'COLUMN_NAME':'id','COLUMN_TYPE':'bigint unsigned','IS_NULLABLE':'NO','COLUMN_DEFAULT':None,'EXTRA':''}),
    ('is_active BOOLEAN NOT NULL DEFAULT TRUE',{'COLUMN_NAME':'is_active','COLUMN_TYPE':'tinyint(1)','IS_NULLABLE':'NO','COLUMN_DEFAULT':None,'EXTRA':''}),
    ('session_version INT UNSIGNED NOT NULL DEFAULT 1',{'COLUMN_NAME':'session_version','COLUMN_TYPE':'int unsigned','IS_NULLABLE':'NO','COLUMN_DEFAULT':'0','EXTRA':''})])
def test_schema_drift_rejects_missing_identity_or_defaults(definition,row):
    from scripts.migrate import ddl_contract,validate_table
    class Cursor:
        def execute(self,sql,*args): self.sql=sql
        def fetchall(self): return [row] if 'COLUMNS' in self.sql else []
    ddl='CREATE TABLE IF NOT EXISTS users (\n  '+definition+'\n) ENGINE=InnoDB'
    with pytest.raises(RuntimeError,match='users.'):
        validate_table(Cursor(),*ddl_contract(ddl)[0])


def test_seed_rerun_preserves_employee_password_and_edited_document(tmp_path):
    from scripts.seed_demo import seed
    # Seed uses only ordinary SQL. SQLite adapter isolates the real repository writes.
    import sqlite3
    from core.database import Database
    from tests.test_portal import Cursor
    path=tmp_path/'seed.db'
    connection=sqlite3.connect(path)
    connection.executescript('''
    CREATE TABLE departments(id INTEGER PRIMARY KEY,code TEXT UNIQUE,name TEXT);
    CREATE TABLE users(id INTEGER PRIMARY KEY,employee_no TEXT UNIQUE,display_name TEXT,password_hash TEXT,department_id INTEGER,role TEXT);
    CREATE TABLE announcements(id INTEGER PRIMARY KEY,seed_key TEXT UNIQUE,title TEXT,content TEXT,category TEXT,is_pinned INTEGER,status TEXT,published_at TEXT,created_by INTEGER);
    CREATE TABLE documents(id INTEGER PRIMARY KEY,document_code TEXT UNIQUE,title TEXT,summary TEXT,content TEXT,category TEXT,department_id INTEGER,visibility TEXT,version TEXT,status TEXT,created_by INTEGER);
    ''')
    connection.close()
    class Connection:
        def __init__(self):
            self.conn=sqlite3.connect(path)
            self.conn.row_factory=sqlite3.Row
        def cursor(self,**kwargs): return Cursor(self.conn.cursor())
        def commit(self): self.conn.commit()
        def rollback(self): self.conn.rollback()
        def close(self): self.conn.close()
    db=Database(connection_factory=Connection)
    seed(db,'test-password-long-enough')
    first=db.query('SELECT password_hash FROM users WHERE employee_no=%s',('MFG001',),one=True)
    db.execute('UPDATE documents SET content=%s WHERE document_code=%s',('edited content','MFG-SOP-001'))
    seed(db,'a-different-long-password')
    assert db.query('SELECT password_hash FROM users WHERE employee_no=%s',('MFG001',),one=True)==first
    assert db.query('SELECT content FROM documents WHERE document_code=%s',('MFG-SOP-001',),one=True)['content']=='edited content'
    assert db.query('SELECT COUNT(*) AS n FROM users',one=True)['n']==5
    assert db.query('SELECT COUNT(*) AS n FROM documents',one=True)['n']==8
