"""SmartFab employee portal. Docker entry point stays uvicorn app:app."""
import logging
import os
from contextlib import asynccontextmanager

import mysql.connector
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from core.config import ROOT, Settings
from core.database import Database
from core.views import render, taipei_date
from routers import auth, portal

logger=logging.getLogger(__name__)


def create_app(settings=None,database=None):
    if settings is None:
        mode=os.getenv('APP_MODE','mysql' if os.getenv('APP_ENV') in {'production','test'} else 'demo')
        if mode=='demo':
            settings=Settings.for_demo()
        elif mode=='mysql':
            if os.getenv('APP_ENV') != 'test': load_dotenv(ROOT/'.env')
            settings=Settings.from_env()
        else:
            raise ValueError('APP_MODE must be demo or mysql')
    if settings.mode=='demo' and settings.environment!='development':
        raise ValueError('demo mode cannot run outside the demonstration environment')
    if database is None:
        if settings.mode=='demo':
            from core.demo_database import DemoDatabase
            db=DemoDatabase()
        else:
            db=Database()
    else:
        db=database

    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            if database is None and settings.mode=='demo': db.close()

    application=FastAPI(title='SmartFab AI Assistant',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    application.state.settings=settings
    application.state.db=db
    application.state.templates=Jinja2Templates(directory=str(ROOT/'templates'))
    application.state.templates.env.filters['taipei_date']=taipei_date
    application.mount('/static',StaticFiles(directory=str(ROOT/'static')),name='static')
    application.include_router(auth.router)
    application.include_router(portal.router)
    if settings.legacy_posts:
        from legacy.posts import router
        application.include_router(router)
    application.add_middleware(TrustedHostMiddleware,allowed_hosts=list(settings.allowed_hosts))
    if settings.environment=='production':
        application.add_middleware(HTTPSRedirectMiddleware)
    application.add_middleware(SessionMiddleware,secret_key=settings.session_secret,
                               session_cookie=settings.session_cookie,max_age=settings.session_max_age,
                               same_site='lax',https_only=settings.https_only)

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
        if error.status_code==303:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(error.headers['Location'],status_code=303)
        descriptions={404:('找不到這個頁面','此內容不存在，或目前無法存取。'),
                      403:('無法完成這次操作','請重新載入頁面後再試。'),
                      405:('不支援這個操作','請透過頁面提供的按鈕進行操作。')}
        title,message=descriptions.get(error.status_code,('無法完成請求','請確認操作內容後再試。'))
        response=render(request,'error.html',{'status_code':error.status_code,'error_title':title,'error_message':message},error.status_code)
        if error.headers: response.headers.update(error.headers)
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request:Request,error):
        return render(request,'error.html',{'status_code':422,'error_title':'請求格式不正確',
                       'error_message':'請確認頁碼、文件編號或輸入內容後再試。'},422)

    async def database_error(request:Request,error):
        logger.error('Portal database unavailable (%s)',type(error).__name__)
        return render(request,'error.html',{'status_code':503,'error_title':'服務暫時無法使用',
                       'error_message':'資料服務暫時無法連線，請稍後再試或聯絡系統管理員。'},503)

    application.add_exception_handler(mysql.connector.Error,database_error)
    application.add_exception_handler(ValueError,database_error)
    return application


app=create_app()
