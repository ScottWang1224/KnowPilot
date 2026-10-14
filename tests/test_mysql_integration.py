"""Opt-in checks require a pre-created EMPTY smartfab_test_* MySQL database.

These tests never read .env and never use DB_* variables from the application.
They leave their dedicated test tables in place for inspection; use a new empty
test database on the next run.
"""
import os
import secrets

import pytest

pytestmark=pytest.mark.skipif(not os.getenv('SMARTFAB_TEST_DB_NAME'),reason='No isolated MySQL test database configured')


@pytest.fixture(scope='module')
def mysql_db():
    import mysql.connector
    from core.database import Database
    name=os.environ['SMARTFAB_TEST_DB_NAME']
    if not name.startswith('smartfab_test_'):
        pytest.fail('Integration database must start with smartfab_test_')
    options=dict(host=os.environ['SMARTFAB_TEST_DB_HOST'],
                 port=int(os.getenv('SMARTFAB_TEST_DB_PORT','3306')),
                 user=os.environ['SMARTFAB_TEST_DB_USER'],password=os.environ['SMARTFAB_TEST_DB_PASSWORD'],
                 database=name,time_zone='+00:00',use_pure=True,connection_timeout=10)
    if os.getenv('SMARTFAB_TEST_DB_SSL_CA'):
        options.update(ssl_ca=os.environ['SMARTFAB_TEST_DB_SSL_CA'],ssl_verify_cert=True,ssl_verify_identity=True)
    db=Database(connection_factory=lambda:mysql.connector.connect(**options))
    if db.query('SHOW TABLES'):
        pytest.fail('Use an empty dedicated MySQL test database; existing tables will not be deleted')
    return db


def test_mysql_migration_seed_and_authorization(mysql_db):
    from scripts.migrate import migrate
    from scripts.seed_demo import seed
    from repositories import content,users
    db=mysql_db
    db.execute('CREATE TABLE posts(id INT PRIMARY KEY,content TEXT,image_url TEXT,created_at DATETIME)')
    db.execute('INSERT INTO posts VALUES(%s,%s,%s,%s)',(91,'existing post','https://example.invalid/existing.png','2026-01-01'))
    connection=db.connection_factory()
    try:
        assert migrate(connection)==['001_smartfab.sql']
        assert migrate(connection)==[]
    finally:
        connection.close()
    password=secrets.token_urlsafe(24)
    seed(db,password)
    before=db.query('SELECT employee_no,password_hash FROM users ORDER BY id')
    seed(db,secrets.token_urlsafe(24))
    assert db.query('SELECT employee_no,password_hash FROM users ORDER BY id')==before
    assert db.query('SELECT COUNT(*) AS n FROM documents',one=True)['n']==8
    employee=users.by_employee_no(db,'MFG001')
    admin=users.by_employee_no(db,'ADMIN001')
    assert content.document_count(db,employee)==5
    assert content.document_count(db,admin)==8
    rd=db.query('SELECT id FROM documents WHERE document_code=%s',('RD-SOP-001',),one=True)
    assert content.document(db,employee,rd['id']) is None
    assert content.document(db,admin,rd['id']) is not None
    assert db.query('SELECT content FROM posts WHERE id=91',one=True)['content']=='existing post'
    assert db.query('SELECT COUNT(*) AS n FROM schema_migrations',one=True)['n']==1
    with pytest.raises(Exception):
        db.execute('UPDATE documents SET department_id=NULL WHERE document_code=%s',('MFG-SOP-001',))
