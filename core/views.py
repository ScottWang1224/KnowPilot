from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

from core.security import csrf_token

TAIPEI = timezone(timedelta(hours=8))


def taipei_date(value):
    if not value: return '—'
    if isinstance(value,str): value=datetime.fromisoformat(value)
    if value.tzinfo is None: value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(TAIPEI).strftime('%Y.%m.%d')


def render(request,name,context=None,status_code=200):
    now=datetime.now(TAIPEI)
    common={'user':getattr(request.state,'user',None),'csrf_token':csrf_token(request),
            'demo_login':request.app.state.settings.demo_login and request.app.state.settings.environment=='development',
            'current_year':now.year,'today':now.strftime('%Y 年 %m 月 %d 日'),
            'role_labels':{'employee':'員工','manager':'部門主管','admin':'管理員'},
            'visibility_labels':{'company':'全公司','department':'部門限定','restricted':'限制存取'},
            'active':'','page_label':'工作平台'}
    common.update(context or {})
    return request.app.state.templates.TemplateResponse(request=request,name=name,context=common,status_code=status_code)


def pagination(path,page,total,page_size,**filters):
    pages=max(1,(total+page_size-1)//page_size)
    filters={k:v for k,v in filters.items() if v}
    def link(number): return path+'?'+urlencode({**filters,'page':number})
    return {'page':page,'total':total,'total_pages':pages,
            'previous_url':link(page-1) if page>1 else None,
            'next_url':link(page+1) if page<pages else None}
