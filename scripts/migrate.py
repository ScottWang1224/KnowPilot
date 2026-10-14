"""Run explicitly: python -m scripts.migrate. Never runs during app startup.

Applies migrations/<engine>/*.sql in filename order and records each one.
An applied file must never change; add a new numbered file instead.
"""
import hashlib

from dotenv import load_dotenv
from core.config import ROOT
from core.database import Database, db_engine

HISTORY_DDL = '''CREATE TABLE IF NOT EXISTS schema_migrations (
  version VARCHAR(100) NOT NULL PRIMARY KEY,
  checksum CHAR(64) NOT NULL,
  applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)'''


def migrate(db, engine):
    db.execute(HISTORY_DDL)
    applied={r['version']:r['checksum'] for r in db.query('SELECT version,checksum FROM schema_migrations')}
    done=[]
    for path in sorted((ROOT/'migrations'/engine).glob('*.sql')):
        sql=path.read_text(encoding='utf-8')
        checksum=hashlib.sha256(sql.encode()).hexdigest()
        if path.name in applied:
            if applied[path.name]!=checksum:
                raise RuntimeError(f'Applied migration changed: {path.name}; add a new migration instead')
            continue
        # MySQL commits each DDL statement implicitly; a failed file needs manual review.
        with db.transaction() as cursor:
            for statement in sql.split(';'):
                if statement.strip(): cursor.execute(statement)
            cursor.execute('INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)',(path.name,checksum))
        done.append(path.name)
    return done


if __name__=='__main__':
    load_dotenv(ROOT/'.env')
    result=migrate(Database.from_env(),db_engine())
    print('Applied: '+', '.join(result) if result else 'No pending migrations.')
