"""Passwords, CSRF and fresh database-backed session authorization."""
import secrets
import time
from urllib.parse import unquote, urlsplit, urlencode

from fastapi import HTTPException, Request
from pwdlib import PasswordHash
from repositories import users

PASSWORDS = PasswordHash.recommended()
# Random dummy hash equalizes missing/disabled-user password verification cost.
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))


def hash_password(password):
    return PASSWORDS.hash(password)


def verify_password(password,hashed):
    try:
        return PASSWORDS.verify(password,hashed)
    except (ValueError, TypeError):
        return False
    except Exception as error:
        # pwdlib rejects unknown hash formats with its own exception type.
        from pwdlib.exceptions import UnknownHashError
        if isinstance(error,UnknownHashError): return False
        raise


def csrf_token(request):
    token=request.session.get('csrf_token')
    if not isinstance(token,str) or len(token)<32:
        token=secrets.token_urlsafe(32)
        request.session['csrf_token']=token
    return token


def verify_csrf(request,token):
    expected=request.session.get('csrf_token')
    if not isinstance(expected,str) or not isinstance(token,str) or not secrets.compare_digest(expected,token):
        raise HTTPException(403,'Invalid CSRF token')


def get_current_user(request: Request):
    if hasattr(request.state,'user'):
        return request.state.user
    request.state.user=None
    settings=request.app.state.settings
    if request.session.get('demo_login') and not (settings.demo_login and settings.environment=='development'):
        request.session.clear()
        return None
    uid=request.session.get('user_id')
    expires=request.session.get('expires_at')
    version=request.session.get('session_version')
    if not isinstance(uid,int) or isinstance(uid,bool):
        return None
    if not isinstance(expires,(int,float)) or expires<=time.time() or not isinstance(version,int):
        request.session.clear()
        return None
    user=users.by_id(request.app.state.db,uid)
    if not user or user['session_version'] != version:
        request.session.clear()
        return None
    request.state.user=user
    return user


def require_user(request: Request):
    user=get_current_user(request)
    if user is None:
        target=request.url.path
        if request.url.query: target+='?'+request.url.query
        raise HTTPException(303,headers={'Location':'/login?'+urlencode({'next':safe_next(target)})})
    return user


def safe_next(value):
    if not isinstance(value,str) or len(value)>1000:
        return '/'
    decoded=unquote(value)
    if any(ord(c)<32 for c in decoded) or '\\' in decoded or not decoded.startswith('/') or decoded.startswith('//'):
        return '/'
    parsed=urlsplit(decoded)
    if parsed.netloc or parsed.scheme or parsed.fragment:
        return '/'
    parts=parsed.path.split('/')
    if parsed.path not in {'/','/documents','/announcements','/assistant'}:
        if len(parts)!=3 or parts[1] not in {'documents','announcements'} or not parts[2].isdigit():
            return '/'
    # Keep the original percent encoding in the query so URL values stay data.
    return value
