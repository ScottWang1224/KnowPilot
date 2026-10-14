import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse

from core.security import DUMMY_HASH, get_current_user, require_user, safe_next, verify_csrf, verify_password
from core.views import render
from repositories import users

router=APIRouter()


@router.get('/login')
def login_page(request:Request,next:Annotated[str,Query(max_length=1000)]='/'):
    if get_current_user(request):
        return RedirectResponse(safe_next(next),status_code=303)
    return render(request,'login.html',{'next_path':safe_next(next)})


@router.post('/login')
def login(request:Request,employee_no:Annotated[str,Form(max_length=32)]='',
          password:Annotated[str,Form(max_length=256)]='',csrf_token:Annotated[str,Form(max_length=128)]='',
          next:Annotated[str,Form(max_length=1000)]='/'):
    verify_csrf(request,csrf_token)
    settings=request.app.state.settings
    db=request.app.state.db
    address=request.client.host if request.client else 'unknown'
    if not users.consume_login_attempt(db,address,settings):
        response=render(request,'login.html',{'next_path':safe_next(next),'employee_no':employee_no,
                       'error':'登入嘗試過於頻繁，請稍後再試。'},status_code=429)
        response.headers['Retry-After']=str(settings.login_window)
        return response
    employee_no=employee_no.strip().upper()
    demo_login=settings.demo_login and settings.environment=='development'
    if demo_login:
        if not employee_no or not password.strip():
            return render(request,'login.html',{'next_path':safe_next(next),'employee_no':employee_no,
                          'error':'請輸入任意非空的帳號與密碼。'},status_code=400)
        # The entered account never chooses identity, role or department.
        user=users.by_employee_no(db,settings.demo_employee_no)
        if not user or user['role']!='employee':
            return render(request,'login.html',{'next_path':safe_next(next),'employee_no':employee_no,
                          'error':'展示員工帳號尚未準備完成，請聯絡系統管理員。'},status_code=503)
    else:
        user=users.by_employee_no(db,employee_no)
        valid=verify_password(password,user['password_hash'] if user else DUMMY_HASH)
        if not user or not valid:
            return render(request,'login.html',{'next_path':safe_next(next),'employee_no':employee_no,
                          'error':'員工編號或密碼不正確，或帳號尚未啟用。'},status_code=400)
    request.session.clear()
    request.session.update(user_id=user['id'],session_version=user['session_version'],
                           expires_at=int(time.time())+settings.session_max_age,csrf_token=secrets.token_urlsafe(32),
                           demo_login=demo_login)
    return RedirectResponse(safe_next(next),status_code=303)


@router.post('/logout')
def logout(request:Request,csrf_token:Annotated[str,Form(max_length=128)]=''):
    verify_csrf(request,csrf_token)
    user=require_user(request)
    users.revoke_sessions(request.app.state.db,user['id'])
    request.session.clear()
    return RedirectResponse('/login',status_code=303)
