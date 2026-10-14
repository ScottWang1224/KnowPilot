from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from core.security import require_user
from core.views import pagination, render
from repositories import content

router=APIRouter(dependencies=[Depends(require_user)])
PAGE_SIZE=12


@router.get('/')
def dashboard(request:Request):
    db,user=request.app.state.db,request.state.user
    return render(request,'index.html',{'active':'dashboard','page_label':'工作總覽',
        'announcement_count':content.announcement_count(db),'document_count':content.document_count(db,user),
        'announcements':content.announcements(db,limit=3),'documents':content.documents(db,user,limit=4)})


@router.get('/announcements')
def announcements(request:Request,page:Annotated[int,Query(ge=1,le=100000)]=1):
    db=request.app.state.db
    total=content.announcement_count(db)
    page=min(page,max(1,(total+PAGE_SIZE-1)//PAGE_SIZE))
    return render(request,'announcements.html',{'active':'announcement','page_label':'公司公告',
        'announcements':content.announcements(db,limit=PAGE_SIZE,offset=(page-1)*PAGE_SIZE),
        **pagination('/announcements',page,total,PAGE_SIZE)})


@router.get('/announcements/{item_id}')
def announcement(request:Request,item_id:Annotated[int,Path(ge=1)]):
    item=content.announcement(request.app.state.db,item_id)
    if item is None: raise HTTPException(404)
    return render(request,'announcement_detail.html',{'item':item,'active':'announcement','page_label':'公司公告'})


@router.get('/documents')
def documents(request:Request,q:Annotated[str,Query(max_length=100)]='',
              category:Annotated[str,Query(max_length=32)]='',page:Annotated[int,Query(ge=1,le=100000)]=1):
    db,user=request.app.state.db,request.state.user
    q=q.strip()
    total=content.document_count(db,user,q,category)
    page=min(page,max(1,(total+PAGE_SIZE-1)//PAGE_SIZE))
    return render(request,'documents.html',{'active':'document','page_label':'文件中心','q':q,'category':category,
        'categories':content.document_categories(db,user),
        'documents':content.documents(db,user,q=q,category=category,limit=PAGE_SIZE,offset=(page-1)*PAGE_SIZE),
        **pagination('/documents',page,total,PAGE_SIZE,q=q,category=category)})


@router.get('/documents/{doc_id}')
def document(request:Request,doc_id:Annotated[int,Path(ge=1)]):
    doc=content.document(request.app.state.db,request.state.user,doc_id)
    if doc is None: raise HTTPException(404)
    return render(request,'document_detail.html',{'doc':doc,'active':'document','page_label':'文件中心'})


@router.get('/assistant')
def assistant(request:Request):
    return render(request,'assistant.html',{'active':'assistant','page_label':'AI 助理'})
