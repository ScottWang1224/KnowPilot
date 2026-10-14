"""Password hashing and identity-only JWTs. Permissions are always read from the database."""
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

PASSWORDS = PasswordHash.recommended()
# Random dummy hash equalizes missing/disabled-user password verification cost.
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))
ALGORITHM = 'HS256'


def hash_password(password):
    return PASSWORDS.hash(password)


def verify_password(password,hashed):
    try:
        return PASSWORDS.verify(password,hashed)
    except (ValueError, TypeError, UnknownHashError):
        return False


def create_token(user_id,settings):
    expires=datetime.now(timezone.utc)+timedelta(hours=settings.token_hours)
    return jwt.encode({'sub':str(user_id),'exp':expires},settings.jwt_secret,algorithm=ALGORITHM)


def token_user_id(token,settings):
    """Return the user id from a valid token, otherwise None."""
    try:
        payload=jwt.decode(token,settings.jwt_secret,algorithms=[ALGORITHM],options={'require':['sub','exp']})
        return int(payload['sub'])
    except (jwt.InvalidTokenError, ValueError):
        return None
