"""Database for command-line tools: a fresh temporary SQLite built from data/ (migrate + seed), or .env's."""
import tempfile
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from core.config import ROOT
from core.database import Database, db_engine
from scripts.migrate import migrate
from scripts.seed import seed


@contextmanager
def open_database(use_env_db=False):
    """Yield (db, source description). The temporary database is removed on exit."""
    with tempfile.TemporaryDirectory() as tmp:
        if use_env_db:
            load_dotenv(ROOT/'.env')
            yield Database.from_env(), db_engine()
        else:
            db = Database.sqlite(Path(tmp)/'eval.db')
            migrate(db, 'sqlite')
            seed(db)
            yield db, 'temporary sqlite from data/'

