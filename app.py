"""KnowPilot employee portal. Docker entry point stays uvicorn app:app."""
import logging
import os
from http import HTTPStatus
import sqlite3

import mysql.connector
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from core.config import ROOT, Settings
from core.database import Database
from core.views import render
from routers import admin, auth, chat, memories, portal

logger=logging.getLogger(__name__)
FIELD_LABELS={'name':'名稱','email':'電子信箱','password':'密碼','department_id':'部門',
              'confirm_clear_history':'確認選項','q':'搜尋關鍵字','category':'分類','page':'頁碼'}


def is_api(request):
    return request.url.path.startswith('/api/')


def api_error(status_code,message,headers=None):
    return JSONResponse({'error':True,'message':message},status_code=status_code,headers=headers)


def create_app(settings=None,database=None):
    if settings is None:
        if os.getenv('APP_ENV') != 'test': load_dotenv(ROOT/'.env')
        settings=Settings.from_env()
    application=FastAPI(title='KnowPilot',docs_url=None,redoc_url=None,openapi_url=None)
    application.state.settings=settings
    application.state.db=database or Database.from_env()
    application.state.templates=Jinja2Templates(directory=str(ROOT/'templates'))
    application.state.chat_clients=chat.default_clients
    chat.warm_up()
    application.mount('/static',StaticFiles(directory=str(ROOT/'static')),name='static')
    application.include_router(auth.router)
    application.include_router(admin.router)
    application.include_router(chat.router)
    application.include_router(memories.router)
    application.include_router(portal.api)
    application.include_router(portal.pages)
    application.add_middleware(TrustedHostMiddleware,allowed_hosts=list(settings.allowed_hosts))
    if settings.environment=='production':
        application.add_middleware(HTTPSRedirectMiddleware)

    @application.middleware('http')
    async def security_headers(request:Request,call_next):
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        if not request.url.path.startswith('/static/'):
            response.headers['Cache-Control']='no-store'
        if settings.environment=='production':
            response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    @application.exception_handler(HTTPException)
    async def http_error(request:Request,error):
        if is_api(request):
            # Our own HTTPExceptions carry Chinese messages; framework ones carry the HTTP phrase.
            default=error.detail==HTTPStatus(error.status_code).phrase
            message={404:'找不到資源',405:'不支援這個操作'}.get(error.status_code,'無法完成請求') if default else error.detail
            return api_error(error.status_code,message,error.headers)
        descriptions={404:('找不到這個頁面','此內容不存在，或目前無法存取。'),
                      403:('無法完成這次操作','請重新載入頁面後再試。'),
                      405:('不支援這個操作','請透過頁面提供的按鈕進行操作。')}
        title,message=descriptions.get(error.status_code,('無法完成請求','請確認操作內容後再試。'))
        response=render(request,'error.html',{'status_code':error.status_code,'error_title':title,'error_message':message},error.status_code)
        if error.headers: response.headers.update(error.headers)
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request:Request,error):
        if is_api(request):
            fields=[FIELD_LABELS.get(str(e['loc'][-1]),str(e['loc'][-1])) for e in error.errors() if e.get('loc')]
            return api_error(400,'欄位格式不正確：'+'、'.join(dict.fromkeys(fields)) if fields else '請求格式不正確')
        return render(request,'error.html',{'status_code':422,'error_title':'請求格式不正確',
                       'error_message':'請確認頁碼、文件編號或輸入內容後再試。'},422)

    async def database_error(request:Request,error):
        logger.error('Portal database unavailable (%s)',type(error).__name__)
        if is_api(request):
            return api_error(503,'資料服務暫時無法使用，請稍後再試')
        return render(request,'error.html',{'status_code':503,'error_title':'服務暫時無法使用',
                       'error_message':'資料服務暫時無法連線，請稍後再試或聯絡系統管理員。'},503)

    application.add_exception_handler(mysql.connector.Error,database_error)
    application.add_exception_handler(sqlite3.Error,database_error)
    return application


app=create_app()
