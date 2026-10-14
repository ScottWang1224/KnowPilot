from datetime import datetime, timezone, timedelta

TAIPEI = timezone(timedelta(hours=8))


def utc_iso(value):
    """MySQL returns naive UTC datetimes and SQLite returns strings; send explicit UTC ISO 8601."""
    if not value: return None
    if isinstance(value,str): value=datetime.fromisoformat(value)
    if value.tzinfo is None: value=value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def render(request,name,context=None,status_code=200):
    now=datetime.now(TAIPEI)
    common={'page_name':name.removesuffix('.html'),'current_year':now.year,'today':now.strftime('%Y 年 %m 月 %d 日'),'active':'','page_label':'工作平台'}
    common.update(context or {})
    return request.app.state.templates.TemplateResponse(request=request,name=name,context=common,status_code=status_code)
