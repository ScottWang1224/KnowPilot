"""Opt-in checks require a pre-created EMPTY knowpilot_test_* MySQL database.

These tests never read .env and never use DB_* variables from the application.
They leave their dedicated test tables in place for inspection; use a new empty
test database on the next run.
"""
import os
import secrets

import pytest

pytestmark=pytest.mark.skipif(not os.getenv('KNOWPILOT_TEST_DB_NAME'),reason='No isolated MySQL test database configured')


@pytest.fixture(scope='module')
def mysql_db():
    import mysql.connector
    from core.database import Database
    name=os.environ['KNOWPILOT_TEST_DB_NAME']
    if not name.startswith('knowpilot_test_'):
        pytest.fail('Integration database must start with knowpilot_test_')
    options=dict(host=os.environ['KNOWPILOT_TEST_DB_HOST'],
                 port=int(os.getenv('KNOWPILOT_TEST_DB_PORT','3306')),
                 user=os.environ['KNOWPILOT_TEST_DB_USER'],password=os.environ['KNOWPILOT_TEST_DB_PASSWORD'],
                 database=name,time_zone='+00:00',use_pure=True,connection_timeout=10)
    if os.getenv('KNOWPILOT_TEST_DB_SSL_CA'):
        options.update(ssl_ca=os.environ['KNOWPILOT_TEST_DB_SSL_CA'],ssl_verify_cert=True,ssl_verify_identity=True)
    db=Database(connection_factory=lambda:mysql.connector.connect(**options))
    if db.query('SHOW TABLES'):
        pytest.fail('Use an empty dedicated MySQL test database; existing tables will not be deleted')
    return db


def test_mysql_migration_seed_and_authorization(mysql_db):
    from scripts.migrate import migrate
    from scripts.seed import seed
    from scripts.seed_admin import seed_admin
    from repositories import content,users
    db=mysql_db
    assert migrate(db,'mysql')==['001_init.sql','002_document_metadata.sql','003_chat.sql']
    assert migrate(db,'mysql')==[]
    seed(db)
    seed(db)
    assert db.query('SELECT COUNT(*) AS n FROM documents',one=True)['n']==78
    password=secrets.token_urlsafe(24)
    seed_admin(db,email='owner@example.com',name='Owner',password=password,department_code='ADM')
    rd=db.query('SELECT id FROM departments WHERE code=%s',('RD',),one=True)['id']
    users.create(db,email='rd@example.com',name='RD',password_hash='x',department_id=rd)
    admin=users.by_email(db,'owner@example.com')
    employee=users.by_email(db,'rd@example.com')
    assert admin['role']=='admin'
    assert content.document_count(db,admin)==18+5
    assert content.document_count(db,employee)==18+10
    mfg=db.query('SELECT id FROM documents WHERE document_code=%s',('MFG-SOP-001',),one=True)
    assert content.document(db,employee,mfg['id']) is None
    assert db.query('SELECT COUNT(*) AS n FROM schema_migrations',one=True)['n']==3
    with pytest.raises(Exception):
        db.execute('UPDATE documents SET department_id=NULL WHERE document_code=%s',('MFG-SOP-001',))
    with pytest.raises(Exception):
        users.create(db,email='RD@example.com'.lower(),name='dup',password_hash='x',department_id=rd)
