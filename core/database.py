"""Short-lived MySQL connections with deterministic cleanup and transactions."""
import os
from contextlib import contextmanager


def get_db_connection():
    import mysql.connector
    missing = [name for name in ('DB_HOST','DB_USER','DB_PASSWORD','DB_NAME') if not os.getenv(name)]
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


class Database:
    def __init__(self, connection_factory=None):
        self.connection_factory = connection_factory or get_db_connection

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
