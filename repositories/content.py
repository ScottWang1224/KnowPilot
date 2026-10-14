"""SQL applies publication and access filters before pagination/count/detail."""
from datetime import datetime, timezone

DOCUMENT_SELECT = '''SELECT d.id,d.document_code,d.title,d.summary,d.content,d.category,
                           d.department_id,d.visibility,d.version,d.updated_at,
                           dep.name AS department_name
                    FROM documents d LEFT JOIN departments dep ON dep.id=d.department_id'''


def document_scope(user):
    """The only document access rule: department alone decides; role never widens it.

    Every document listing, detail and future RAG retrieval must go through here,
    always with the database user from get_current_user, never a request parameter.
    """
    return ("d.status='published' AND (d.visibility='company' OR (d.visibility='department' AND d.department_id=%s))",
            (user['department_id'],))


def search_scope(user,q='',category=''):
    scope,params=document_scope(user)
    if q:
        pattern='%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'
        scope+=" AND (d.title LIKE %s ESCAPE '!' OR d.document_code LIKE %s ESCAPE '!' OR d.summary LIKE %s ESCAPE '!')"
        params+= (pattern,pattern,pattern)
    if category:
        scope+=' AND d.category=%s'
        params+=(category,)
    return scope,params


def document_count(db,user,q='',category=''):
    scope,params=search_scope(user,q,category)
    return db.query('SELECT COUNT(*) AS total FROM documents d WHERE '+scope,params,one=True)['total']


def documents(db,user,*,q='',category='',limit=12,offset=0):
    scope,params=search_scope(user,q,category)
    return db.query(DOCUMENT_SELECT+' WHERE '+scope+' ORDER BY d.updated_at DESC,d.id DESC LIMIT %s OFFSET %s',params+(limit,offset))


def document(db,user,doc_id):
    scope,params=document_scope(user)
    return db.query(DOCUMENT_SELECT+' WHERE '+scope+' AND d.id=%s',params+(doc_id,),one=True)


def document_categories(db,user):
    scope,params=document_scope(user)
    return [r['category'] for r in db.query('SELECT DISTINCT d.category FROM documents d WHERE '+scope+' ORDER BY d.category',params)]


def announcement_scope():
    return "a.status='published' AND a.published_at IS NOT NULL AND a.published_at<=%s",(datetime.now(timezone.utc).replace(tzinfo=None),)


def announcement_count(db):
    scope,params=announcement_scope()
    return db.query('SELECT COUNT(*) AS total FROM announcements a WHERE '+scope,params,one=True)['total']


def announcements(db,*,limit=12,offset=0):
    scope,params=announcement_scope()
    return db.query('SELECT a.* FROM announcements a WHERE '+scope+' ORDER BY a.is_pinned DESC,a.published_at DESC,a.id DESC LIMIT %s OFFSET %s',params+(limit,offset))


def announcement(db,item_id):
    scope,params=announcement_scope()
    return db.query('''SELECT a.*,u.name AS author_name FROM announcements a
                       LEFT JOIN users u ON u.id=a.created_by WHERE '''+scope+' AND a.id=%s',params+(item_id,),one=True)
