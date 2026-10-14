"""Read-only admin APIs. Conversation and feedback endpoints wait for the chat tables."""
from fastapi import APIRouter, Depends, Request

from core.dependencies import require_admin
from core.views import utc_iso
from repositories import users

router=APIRouter(prefix='/api/admin',dependencies=[Depends(require_admin)])


@router.get('/users')
def all_users(request:Request):
    return {'data':[{'id':u['id'],'name':u['name'],'email':u['email'],'role':u['role'],'is_active':bool(u['is_active']),
                     'department':{'id':u['department_id'],'name':u['department_name']},'created_at':utc_iso(u['created_at'])}
                    for u in users.all_users(request.app.state.db)]}
