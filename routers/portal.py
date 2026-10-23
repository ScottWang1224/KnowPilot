"""Page shells plus the JSON APIs that fill them.

Pages carry no data and do no login check; the browser loads data from /api/*,
where get_current_user and document_scope protect every response.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from core.dependencies import get_current_user
from core.views import render, utc_iso
from llm import settings as llm_settings
from repositories import content

pages=APIRouter()
api=APIRouter(prefix='/api',dependencies=[Depends(get_current_user)])
PAGE_SIZE=12
PAGE_LABELS={'dashboard':'工作總覽','announcement':'公司公告','document':'文件中心','assistant':'AI 助理','profile':'個人資料','admin':'後台'}


def page(request,name,active,**context):
    return render(request,name,{'active':active,'page_label':PAGE_LABELS[active],**context})


@pages.get('/')
def dashboard_page(request:Request):
    return page(request,'index.html','dashboard')


@pages.get('/announcements')
def announcements_page(request:Request):
    return page(request,'announcements.html','announcement')


@pages.get('/announcements/{item_id}')
def announcement_page(request:Request,item_id:Annotated[int,Path(ge=1)]):
    return page(request,'announcement_detail.html','announcement',item_id=item_id)


@pages.get('/documents')
def documents_page(request:Request):
    return page(request,'documents.html','document')


@pages.get('/documents/{doc_id}')
def document_page(request:Request,doc_id:Annotated[int,Path(ge=1)]):
    return page(request,'document_detail.html','document',doc_id=doc_id)


@pages.get('/assistant')
def assistant_page(request:Request):
    # The input limit is shown in the page; the chat API enforces it again.
    return page(request,'assistant.html','assistant',max_chars=llm_settings.chat_max_input_chars())


@pages.get('/profile')
def profile_page(request:Request):
    return page(request,'profile.html','profile')


# Admin page shells: like every page they carry no data; /api/admin/* (require_admin) decides who sees anything.
@pages.get('/admin/dashboard')
def admin_dashboard_page(request:Request):
    return page(request,'admin_dashboard.html','admin')


@pages.get('/admin/users')
def admin_users_page(request:Request):
    return page(request,'admin_users.html','admin')


@pages.get('/admin/users/{user_id}')
def admin_user_page(request:Request,user_id:Annotated[int,Path(ge=1)]):
    return page(request,'admin_user.html','admin',user_id=user_id)


@pages.get('/admin/documents/{doc_id}')
def admin_document_page(request:Request,doc_id:Annotated[int,Path(ge=1)]):
    return page(request,'admin_document.html','admin',doc_id=doc_id)


@pages.get('/login')
def login_page(request:Request):
    return render(request,'login.html')


@pages.get('/register')
def register_page(request:Request):
    return render(request,'register.html')


def paged(items,page,total):
    return {'data':items,'page':page,'total':total,'total_pages':max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)}


def clamp(page,total):
    return min(page,max(1,(total+PAGE_SIZE-1)//PAGE_SIZE))


def announcement_data(item):
    data={k:item[k] for k in ('id','title','content','category')}
    data.update(is_pinned=bool(item['is_pinned']),published_at=utc_iso(item['published_at']))
    if 'author_name' in item: data['author_name']=item['author_name']
    return data


def document_data(doc,with_content=False):
    keys=('id','document_code','title','summary','category','visibility','version','department_name')
    data={k:doc[k] for k in keys}
    data['updated_at']=utc_iso(doc['updated_at'])
    if with_content: data['content']=doc['content']
    return data


@api.get('/announcements')
def announcements(request:Request,page:Annotated[int,Query(ge=1,le=100000)]=1):
    db=request.app.state.db
    total=content.announcement_count(db)
    page=clamp(page,total)
    items=content.announcements(db,limit=PAGE_SIZE,offset=(page-1)*PAGE_SIZE)
    return paged([announcement_data(a) for a in items],page,total)


@api.get('/announcements/{item_id}')
def announcement(request:Request,item_id:Annotated[int,Path(ge=1)]):
    item=content.announcement(request.app.state.db,item_id)
    if item is None: raise HTTPException(404,'找不到這則公告')
    return {'data':announcement_data(item)}


@api.get('/documents')
def documents(request:Request,user=Depends(get_current_user),q:Annotated[str,Query(max_length=100)]='',
              category:Annotated[str,Query(max_length=32)]='',page:Annotated[int,Query(ge=1,le=100000)]=1):
    db=request.app.state.db
    q=q.strip()
    total=content.document_count(db,user,q,category)
    page=clamp(page,total)
    items=content.documents(db,user,q=q,category=category,limit=PAGE_SIZE,offset=(page-1)*PAGE_SIZE)
    return {**paged([document_data(d) for d in items],page,total),'categories':content.document_categories(db,user)}


@api.get('/documents/{doc_id}')
def document(request:Request,doc_id:Annotated[int,Path(ge=1)],user=Depends(get_current_user)):
    doc=content.document(request.app.state.db,user,doc_id)
    if doc is None: raise HTTPException(404,'找不到這份文件')
    return {'data':document_data(doc,with_content=True)}
