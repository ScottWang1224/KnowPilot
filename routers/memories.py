"""Agent memory API: a user reads, sets and deletes only their own memory items.

Keys and values are checked against rag.memory on every write; nothing outside the user's
options is stored. Saving a proposal from the chat reads it from the stored message, never
from the request, so the client cannot choose what gets remembered.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from core.dependencies import get_current_user
from core.views import utc_iso
from rag import memory
from rag.router import memory_options
from repositories import chat, memories
from schemas.memory import ConfirmRequest, MemoryValue

router = APIRouter(prefix='/api')

UNKNOWN_KEY = '無法記住這個項目'
INVALID_VALUE = '請從可選的項目中選擇'
NO_PROPOSAL = '找不到可儲存的記憶'
KeyPath = Annotated[str, Path(max_length=32)]


def listing(db, user):
    """The user's items in LABELS order, each marked valid or not, and the options of every available key."""
    options = memory_options(db, user)
    rows = {r['mem_key']: r for r in memories.stored(db, user['id'])}
    items = [{**memory.described(key, rows[key]['mem_value']), 'source': rows[key]['source'],
              'valid': memory.is_valid(options, key, rows[key]['mem_value']),
              'updated_at': utc_iso(rows[key]['updated_at'])}
             for key in memory.LABELS if key in rows]
    return {'items': items, 'options': options, 'labels': {key: memory.LABELS[key] for key in options}}


def user_memory(db, user):
    """The user's valid memory for the prompts (rag.memory.Memory)."""
    return memory.split(memories.values(db, user['id']), memory_options(db, user))


@router.get('/memories')
def get_memories(request: Request, user=Depends(get_current_user)):
    return {'data': listing(request.app.state.db, user)}


@router.put('/memories/{key}')
def put_memory(request: Request, key: KeyPath, body: MemoryValue, user=Depends(get_current_user)):
    db = request.app.state.db
    options = memory_options(db, user)
    if key not in options:
        raise HTTPException(400, UNKNOWN_KEY)
    if not memory.is_valid(options, key, body.value):
        raise HTTPException(400, INVALID_VALUE)
    memories.set_value(db, user['id'], key, body.value, 'manual')
    return {'data': listing(db, user)}


@router.delete('/memories/{key}')
def delete_memory(request: Request, key: KeyPath, user=Depends(get_current_user)):
    db = request.app.state.db
    deleted = memories.delete(db, user['id'], key)
    return {'deleted': deleted, 'data': listing(db, user)}


@router.delete('/memories')
def clear_memories(request: Request, user=Depends(get_current_user)):
    db = request.app.state.db
    deleted = memories.delete(db, user['id'])
    return {'deleted': deleted, 'data': listing(db, user)}


@router.post('/memories/confirm')
def confirm(request: Request, body: ConfirmRequest, user=Depends(get_current_user)):
    """Save what a memory card proposed. A second confirmation changes nothing and returns the current items."""
    db = request.app.state.db
    row = chat.assistant_message(db, user, body.message_id)
    payload = chat.loads(row['payload']) if row else None
    card = payload if row and row['kind'] == 'memory' else (payload or {}).get('memory_proposal')
    if not card or not card.get('proposal'):
        raise HTTPException(404, NO_PROPOSAL)
    if not card.get('confirmed'):
        options = memory_options(db, user)
        # Checked again: options may have changed since the card was shown.
        items = [(i['key'], i['value']) for i in card['proposal'] if memory.is_valid(options, i['key'], i['value'])]
        card['confirmed'] = True
        memories.save_proposal(db, user['id'], row['id'], items, payload)
    return {'data': listing(db, user)}
