"""Environment-only settings; importing this module never loads credentials."""
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in {'true', 'false', '1', '0'}:
        raise ValueError(f'{name} must be true or false')
    return value.lower() in {'true', '1'}


@dataclass(frozen=True)
class Settings:
    environment: str
    session_secret: str
    https_only: bool
    allowed_hosts: tuple[str, ...]
    session_cookie: str = 'smartfab_session'
    session_max_age: int = 28800
    login_limit: int = 20
    login_window: int = 900
    legacy_posts: bool = False
    demo_login: bool = False
    demo_employee_no: str = 'MFG001'
    mode: str = 'mysql'

    @classmethod
    def for_demo(cls):
        """A self-contained fictional demo; never reads database or secret settings."""
        if os.getenv('APP_ENV')=='production':
            raise ValueError('demo mode cannot run with APP_ENV=production')
        return cls(environment='development',session_secret=secrets.token_urlsafe(48),
                   https_only=False,allowed_hosts=('*',),demo_login=True,mode='demo')

    @classmethod
    def from_env(cls):
        environment = os.getenv('APP_ENV', 'development').lower()
        if environment not in {'development', 'test', 'production'}:
            raise ValueError('APP_ENV must be development, test or production')
        secret = os.getenv('SESSION_SECRET', '')
        if len(secret) < 32:
            raise ValueError('SESSION_SECRET must be provided with at least 32 characters')
        secure = env_bool('SESSION_HTTPS_ONLY', environment == 'production')
        hosts = tuple(h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',') if h.strip())
        if not hosts:
            raise ValueError('ALLOWED_HOSTS cannot be empty')
        if environment == 'production' and (not secure or any('*' in h for h in hosts)):
            raise ValueError('Production requires secure cookies and explicit ALLOWED_HOSTS')
        demo_login=env_bool('DEMO_LOGIN_ENABLED',environment=='development')
        if demo_login and environment!='development':
            raise ValueError('DEMO_LOGIN_ENABLED is only permitted in development')
        demo_employee_no=os.getenv('DEMO_EMPLOYEE_NO','MFG001').strip().upper()
        if not demo_employee_no or len(demo_employee_no)>32:
            raise ValueError('DEMO_EMPLOYEE_NO must be a valid employee number')
        max_age = int(os.getenv('SESSION_MAX_AGE', '28800'))
        limit = int(os.getenv('LOGIN_LIMIT', '20'))
        window = int(os.getenv('LOGIN_WINDOW_SECONDS', '900'))
        if not 300 <= max_age <= 86400 or not 1 <= limit <= 100 or not 60 <= window <= 3600:
            raise ValueError('Session or login throttle settings are outside supported bounds')
        return cls(environment, secret, secure, hosts, session_max_age=max_age,
                   login_limit=limit, login_window=window,
                   legacy_posts=env_bool('ENABLE_LEGACY_POSTS', False),
                   demo_login=demo_login,demo_employee_no=demo_employee_no)
