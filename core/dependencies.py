"""Shared authentication dependencies for every protected API."""
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.security import token_user_id
from repositories import users

bearer = HTTPBearer(auto_error=False)


def get_current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    """Fresh database user for the bearer token; department, role and is_active apply immediately."""
    user_id = token_user_id(credentials.credentials, request.app.state.settings) if credentials else None
    user = users.by_id(request.app.state.db, user_id) if user_id else None
    if user is None:
        raise HTTPException(401, '請先登入', headers={'WWW-Authenticate': 'Bearer'})
    return user


def require_admin(user=Depends(get_current_user)):
    if user['role'] != 'admin':
        raise HTTPException(403, '沒有權限執行此操作')
    return user
