"""Environment-only settings; importing this module never loads credentials."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    environment: str
    jwt_secret: str
    allowed_hosts: tuple[str, ...]
    token_hours: int = 8
    login_limit: int = 20
    login_window: int = 900

    @classmethod
    def from_env(cls):
        from core.database import MYSQL_SETTINGS, db_engine
        environment = os.getenv('APP_ENV', 'development').lower()
        if environment not in {'development', 'test', 'production'}:
            raise ValueError('APP_ENV must be development, test or production')
        secret = os.getenv('JWT_SECRET', '')
        if len(secret) < 32:
            raise ValueError('JWT_SECRET must be provided with at least 32 characters')
        hosts = tuple(h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',') if h.strip())
        if not hosts:
            raise ValueError('ALLOWED_HOSTS cannot be empty')
        if environment == 'production' and any('*' in h for h in hosts):
            raise ValueError('Production requires explicit ALLOWED_HOSTS')
        engine = db_engine()
        if environment == 'production' and engine != 'mysql':
            raise ValueError('Production requires DB_ENGINE=mysql')
        missing = [name for name in MYSQL_SETTINGS if not os.getenv(name)]
        if engine == 'mysql' and missing:
            raise ValueError('Missing database settings: ' + ', '.join(missing))
        if environment == 'production' and not os.getenv('DB_SSL_CA'):
            raise ValueError('Production requires DB_SSL_CA for verified MySQL TLS')
        hours = int(os.getenv('JWT_EXPIRE_HOURS', '8'))
        limit = int(os.getenv('LOGIN_LIMIT', '20'))
        window = int(os.getenv('LOGIN_WINDOW_SECONDS', '900'))
        if not 8 <= hours <= 12 or not 1 <= limit <= 100 or not 60 <= window <= 3600:
            raise ValueError('Token lifetime or login throttle settings are outside supported bounds')
        return cls(environment, secret, hosts, token_hours=hours, login_limit=limit, login_window=window)
