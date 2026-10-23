import os
import secrets

# Test imports must never load the project's real .env or contact its RDS.
os.environ['APP_ENV']='test'
os.environ['DB_ENGINE']='sqlite'
os.environ['JWT_SECRET']=secrets.token_urlsafe(48)
import pytest


@pytest.fixture(scope='module')
def seeded(tmp_path_factory):
    from core.database import Database
    from scripts.migrate import migrate
    from scripts.seed import seed
    db=Database.sqlite(tmp_path_factory.mktemp('rag')/'rag.db')
    migrate(db,'sqlite')
    seed(db)
    departments={r['code']:r['id'] for r in db.query('SELECT id,code FROM departments')}
    return db,departments
