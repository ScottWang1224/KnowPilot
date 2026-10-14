import sqlite3

import mysql.connector
from fastapi import APIRouter, Depends, HTTPException, Request

from core.dependencies import get_current_user
from core.security import DUMMY_HASH, create_token, hash_password, verify_password
from repositories import users
from schemas.auth import DepartmentChangeRequest, LoginRequest, RegisterRequest

router=APIRouter(prefix='/api')


def throttle(request,action):
    address=request.client.host if request.client else 'unknown'
    settings=request.app.state.settings
    if not users.consume_attempt(request.app.state.db,action,address,settings):
        raise HTTPException(429,'嘗試次數過多，請稍後再試',headers={'Retry-After':str(settings.login_window)})


def user_data(user):
    return {'id':user['id'],'name':user['name'],'email':user['email'],'role':user['role'],
            'department':{'id':user['department_id'],'name':user['department_name']}}


@router.post('/auth/register',status_code=201)
def register(request:Request,body:RegisterRequest):
    throttle(request,'register')
    db=request.app.state.db
    # Demo feature: testers choose their own department to compare document access.
    if not users.department_exists(db,body.department_id):
        raise HTTPException(400,'部門不存在')
    try:
        users.create(db,email=body.email,name=body.name,password_hash=hash_password(body.password),
                     department_id=body.department_id)
    except (mysql.connector.IntegrityError,sqlite3.IntegrityError):
        # The UNIQUE email key also covers two concurrent registrations.
        raise HTTPException(409,'此電子信箱已註冊')
    return {'ok':True}


@router.post('/auth/login')
def login(request:Request,body:LoginRequest):
    throttle(request,'login')
    user=users.by_email(request.app.state.db,body.email.strip().lower())
    valid=verify_password(body.password,user['password_hash'] if user else DUMMY_HASH)
    if not user or not valid:
        raise HTTPException(401,'電子信箱或密碼錯誤')
    return {'token':create_token(user['id'],request.app.state.settings)}


@router.get('/auth/me')
def me(user=Depends(get_current_user)):
    return {'data':user_data(user)}


@router.patch('/auth/me/department')
def change_department(request:Request,body:DepartmentChangeRequest,user=Depends(get_current_user)):
    """Demo feature: switching department hides the user's earlier conversations from them."""
    db=request.app.state.db
    if not body.confirm_clear_history:
        raise HTTPException(400,'請先確認切換部門會隱藏先前的對話紀錄')
    if not users.department_exists(db,body.department_id):
        raise HTTPException(400,'部門不存在')
    hidden=users.change_department(db,user['id'],body.department_id)
    return {'data':user_data(users.by_id(db,user['id'])),'hidden_conversations':hidden}


@router.get('/departments')
def departments(request:Request):
    return {'data':users.departments(request.app.state.db)}
