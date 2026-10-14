"""Disposable, seeded SQLite demo. No disk files, MySQL or AWS connections.

Repositories still enforce the same SQL access predicates. A process-local lock
serializes SQLite transactions; production MySQL uses its own row locks instead.
"""
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from core.config import ROOT
from core.database import Database


class DemoCursor:
    def __init__(self,cursor):
        self.cursor=cursor

    def execute(self,sql,params=()):
        sql=sql.replace('%s','?').replace(' FOR UPDATE','').replace('INSERT IGNORE','INSERT OR IGNORE')
        self.cursor.execute(sql,params)

    def fetchone(self):
        row=self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self): return self.cursor.rowcount

    @property
    def lastrowid(self): return self.cursor.lastrowid

    def close(self): self.cursor.close()


class DemoConnection:
    def __init__(self,uri):
        self.connection=sqlite3.connect(uri,uri=True,check_same_thread=False)
        self.connection.row_factory=sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')

    def cursor(self,**kwargs): return DemoCursor(self.connection.cursor())
    def commit(self): self.connection.commit()
    def rollback(self): self.connection.rollback()
    def close(self): self.connection.close()


class DemoDatabase(Database):
    def __init__(self):
        self.uri=f'file:smartfab-{uuid.uuid4().hex}?mode=memory&cache=shared'
        self.lock=threading.RLock()
        self.anchor=sqlite3.connect(self.uri,uri=True,check_same_thread=False)
        try:
            self.anchor.executescript((ROOT/'core/demo_schema.sql').read_text(encoding='utf-8'))
            super().__init__(connection_factory=lambda:DemoConnection(self.uri))
            from scripts.seed_demo import seed
            seed(self,secrets.token_urlsafe(32))
        except Exception:
            self.anchor.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            with super().transaction() as cursor:
                yield cursor

    def close(self):
        with self.lock:
            self.anchor.close()
