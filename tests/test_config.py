import pytest


def test_database_closes_connection_and_rolls_back_on_failure():
    from core.database import Database
    events=[]
    class Connection:
        def cursor(self,**kwargs): return self
        def execute(self,*args): raise RuntimeError('database error')
        def commit(self): events.append('commit')
        def rollback(self): events.append('rollback')
        def close(self): events.append('close')
    with pytest.raises(RuntimeError):
        Database(connection_factory=Connection).execute('bad statement')
    assert events == ['rollback','close','close']


def test_migration_and_seed_reruns_preserve_existing_data(tmp_path):
    from core.database import Database
    from scripts.migrate import migrate
    from scripts.seed import seed
    db=Database.sqlite(tmp_path/'seed.db')
    assert migrate(db,'sqlite')==['001_init.sql']
    assert migrate(db,'sqlite')==[]
    seed(db)
    db.execute('UPDATE documents SET content=%s WHERE document_code=%s',('edited content','MFG-SOP-001'))
    seed(db)
    assert db.query('SELECT content FROM documents WHERE document_code=%s',('MFG-SOP-001',),one=True)['content']=='edited content'
    assert db.query('SELECT COUNT(*) AS n FROM documents',one=True)['n']==8
    assert db.query('SELECT COUNT(*) AS n FROM users',one=True)['n']==0


def test_seed_admin_is_the_only_admin_path_and_never_overwrites(tmp_path):
    from core.database import Database
    from core.security import verify_password
    from scripts.migrate import migrate
    from scripts.seed import seed
    from scripts.seed_admin import seed_admin
    db=Database.sqlite(tmp_path/'admin.db')
    migrate(db,'sqlite')
    account=dict(email='Owner@Example.com',name='擁有者',password='owner-password',department_code='ADM')
    with pytest.raises(ValueError,match='scripts.seed'):
        seed_admin(db,**account)
    seed(db)
    seed_admin(db,**account)
    row=db.query('SELECT * FROM users WHERE email=%s',('owner@example.com',),one=True)
    assert row['role']=='admin' and verify_password('owner-password',row['password_hash'])
    with pytest.raises(ValueError,match='already exists'):
        seed_admin(db,**{**account,'password':'a-new-password'})
    with pytest.raises(ValueError):
        seed_admin(db,**{**account,'email':'second@example.com','password':'short'})


def test_changed_migration_is_rejected(tmp_path,monkeypatch):
    import scripts.migrate
    from core.database import Database
    (tmp_path/'migrations'/'sqlite').mkdir(parents=True)
    migration=tmp_path/'migrations'/'sqlite'/'001_test.sql'
    migration.write_text('CREATE TABLE t (id INTEGER PRIMARY KEY);',encoding='utf-8')
    monkeypatch.setattr(scripts.migrate,'ROOT',tmp_path)
    db=Database.sqlite(tmp_path/'test.db')
    scripts.migrate.migrate(db,'sqlite')
    migration.write_text('CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);',encoding='utf-8')
    with pytest.raises(RuntimeError,match='changed'):
        scripts.migrate.migrate(db,'sqlite')
