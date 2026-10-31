"""Read-only admin APIs: users, their full conversation history (hidden included), the dashboard figures
and any published document cited in those conversations.

Admins read documents of every department only through /api/admin/documents (require_admin).
document_scope() never widens for them: listings, the RAG index and an admin's own chat stay
on the admin's department.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from core.dependencies import require_admin
from core.views import utc_iso
from llm import settings
from repositories import chat, content, stats, users
from routers.chat import message_data
from routers.memories import listing as memory_listing
from routers.portal import document_data

router=APIRouter(prefix='/api/admin',dependencies=[Depends(require_admin)])


@router.get('/users')
def all_users(request:Request):
    db=request.app.state.db
    activity,spent=chat.user_activity(db),chat.spent_today_by_user(db)
    limit=settings.chat_user_daily_budget_usd()
    data=[]
    for u in users.all_users(db):
        act=activity.get(u['id']) or {}
        data.append({'id':u['id'],'name':u['name'],'email':u['email'],'role':u['role'],'is_active':bool(u['is_active']),
                     'department':{'id':u['department_id'],'name':u['department_name']},'created_at':utc_iso(u['created_at']),
                     'conversations':act.get('conversations',0),
                     'today_percent':round(spent.get(u['id'],0.0)/limit*100) if limit>0 else None,
                     'last_active_at':utc_iso(act.get('last_active_at'))})
    # Most recently active first; users who never asked anything last.
    data.sort(key=lambda u:u['last_active_at'] or '',reverse=True)
    return {'data':data}


@router.get('/users/{user_id}/conversations')
def user_conversations(request:Request,user_id:Annotated[int,Path(ge=1)]):
    db=request.app.state.db
    owner=db.query('SELECT u.id,u.name,u.email,d.name AS department_name FROM users u '
                   'JOIN departments d ON d.id=u.department_id WHERE u.id=%s',(user_id,),one=True)
    if owner is None: raise HTTPException(404,'找不到這位使用者')
    return {'user':{'id':owner['id'],'name':owner['name'],'email':owner['email'],'department':owner['department_name']},
            'data':[{'id':c['id'],'title':c['title'],'department':{'id':c['department_id'],'name':c['department_name']},
                     'hidden_at':utc_iso(c['hidden_at']),'hidden_reason':c['hidden_reason'],'message_count':c['message_count'],
                     'created_at':utc_iso(c['created_at']),'updated_at':utc_iso(c['updated_at'])}
                    for c in chat.user_conversations(db,user_id)]}


@router.get('/users/{user_id}/memories')
def user_memories(request:Request,user_id:Annotated[int,Path(ge=1)]):
    """Read-only: the user's current memory items, judged against the user's own options."""
    db=request.app.state.db
    owner=db.query('SELECT id,department_id FROM users WHERE id=%s',(user_id,),one=True)
    if owner is None: raise HTTPException(404,'找不到這位使用者')
    return {'data':memory_listing(db,owner)['items']}


@router.get('/conversations/{conversation_id}/messages')
def conversation_messages(request:Request,conversation_id:Annotated[int,Path(ge=1)],admin=Depends(require_admin)):
    db=request.app.state.db
    c=chat.conversation_with_owner(db,conversation_id)
    if c is None: raise HTTPException(404,'找不到這段對話')
    rows=chat.messages(db,conversation_id)
    feedback=chat.feedback_for(db,[r['id'] for r in rows if r['kind']=='answer'],user_id=c['user_id'])
    messages=[]
    for r in rows:
        data=message_data(db,admin,r,feedback.get(r['id']),admin_view=True)
        if r['role']=='assistant': data['trace']=chat.loads(r['trace'])
        messages.append(data)
    return {'conversation':{'id':c['id'],'title':c['title'],'user':{'id':c['user_id'],'name':c['user_name'],'email':c['user_email']},
                            'department':{'id':c['department_id'],'name':c['department_name']},
                            'hidden_at':utc_iso(c['hidden_at']),'hidden_reason':c['hidden_reason'],
                            'created_at':utc_iso(c['created_at'])},
            'messages':messages}


@router.get('/documents/{doc_id}')
def any_document(request:Request,doc_id:Annotated[int,Path(ge=1)]):
    doc=content.admin_document(request.app.state.db,doc_id)
    if doc is None: raise HTTPException(404,'找不到這份文件')
    return {'data':document_data(doc,with_content=True)}


@router.get('/stats')
def dashboard_stats(request:Request,days:Annotated[int,Query(ge=1,le=90)]=14):
    data=stats.dashboard(request.app.state.db,days,user_limit=settings.chat_user_daily_budget_usd(),
                         global_limit=settings.daily_budget_usd())
    for item in data['feedback']['down_list']:
        item['created_at']=utc_iso(item['created_at'])
    return {'data':data}
