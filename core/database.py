"""Short-lived connections with deterministic cleanup and transactions.

MySQL is the production database. SQLite is for local development and tests;
its adapter translates the few MySQL-only phrases the repositories use.
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from core.config import ROOT

MYSQL_SETTINGS = ('DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME')


def db_engine():
    engine = os.getenv('DB_ENGINE', 'sqlite').lower()
    if engine not in {'sqlite', 'mysql'}:
        raise ValueError('DB_ENGINE must be sqlite or mysql')
    return engine


def get_db_connection():
    import mysql.connector
    missing = [name for name in MYSQL_SETTINGS if not os.getenv(name)]
    if missing:
        raise ValueError('Missing database settings: ' + ', '.join(missing))
    options = dict(host=os.environ['DB_HOST'], port=int(os.getenv('DB_PORT','3306')),
                   user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'],
                   database=os.environ['DB_NAME'], charset='utf8mb4',
                   connection_timeout=10, time_zone='+00:00',use_pure=True)
    if os.getenv('DB_SSL_CA'):
        options.update(ssl_ca=os.environ['DB_SSL_CA'], ssl_verify_cert=True, ssl_verify_identity=True)
    if os.getenv('APP_ENV') == 'production' and not os.getenv('DB_SSL_CA'):
        raise ValueError('Production requires DB_SSL_CA for verified MySQL TLS')
    return mysql.connector.connect(**options)


class SqliteCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, sql, params=()):
        sql = sql.replace('%s', '?').replace(' FOR UPDATE', '').replace('INSERT IGNORE', 'INSERT OR IGNORE')
        self.cursor.execute(sql, params)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self): return self.cursor.rowcount

    @property
    def lastrowid(self): return self.cursor.lastrowid

    def close(self): self.cursor.close()


class SqliteConnection:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')

    def cursor(self, **kwargs): return SqliteCursor(self.connection.cursor())
    def commit(self): self.connection.commit()
    def rollback(self): self.connection.rollback()
    def close(self): self.connection.close()


class Database:
    def __init__(self, connection_factory=None):
        self.connection_factory = connection_factory or get_db_connection

    @classmethod
    def sqlite(cls, path):
        def connect():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            return SqliteConnection(path)
        return cls(connect)

    @classmethod
    def from_env(cls):
        if db_engine() == 'sqlite':
            return cls.sqlite(os.getenv('SQLITE_PATH') or ROOT/'.data'/'knowpilot.db')
        return cls()

    @contextmanager
    def transaction(self):
        connection = self.connection_factory()
        cursor = None
        try:
            cursor = connection.cursor(dictionary=True)
            yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            try:
                if cursor is not None:
                    cursor.close()
            finally:
                connection.close()

    def query(self, sql, params=(), *, one=False):
        with self.transaction() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone() if one else cursor.fetchall()

    def execute(self, sql, params=()):
        with self.transaction() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount
