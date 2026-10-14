"""Resource and navigation URLs must not depend on the upstream origin."""
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from tests.test_portal import portal, login, csrf


class PageURLs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls=[]

    def handle_starttag(self,tag,attrs):
        self.urls.extend(value for key,value in attrs if key in {'href','src','action'} and value)


@pytest.mark.parametrize('origin,headers',[
    ('http://127.0.0.1:8000',{}),
    ('http://127.0.0.1:8000',{'X-Forwarded-Host':'testnote.stream','X-Forwarded-Proto':'https'}),
    ('https://testnote.stream',{}),
])
def test_resources_and_redirects_are_origin_independent(portal,origin,headers):
    # Isolate URL generation from host validation; mirrors the demo's allowed hosts.
    from dataclasses import replace
    from app import create_app
    application=create_app(replace(portal[2],allowed_hosts=('*',)),portal[1])
    with TestClient(application,base_url=origin,headers=headers) as client:
        anonymous=client.get('/documents/2',follow_redirects=False)
        assert anonymous.status_code==303
        assert anonymous.headers['location']=='/login?next=%2Fdocuments%2F2'
        page=client.get('/login')
        assert 'href="/static/css/index.css"' in page.text
        result=login(client,next_path='/documents/2')
        assert result.status_code==303
        assert result.headers['location']=='/documents/2'
        for path in ['/','/documents','/documents/2','/announcements','/announcements/1','/assistant','/missing']:
            page=client.get(path)
            assert page.status_code==(404 if path=='/missing' else 200)
            assert 'href="/static/css/index.css"' in page.text
            assert 'src="/static/js/app.js"' in page.text
            urls=PageURLs()
            urls.feed(page.text)
            assert all(url.startswith(('/', '#')) and not url.startswith('//') for url in urls.urls),urls.urls
        css=client.get('/static/css/index.css')
        js=client.get('/static/js/app.js')
        assert css.status_code==js.status_code==200
        assert css.headers['content-type'].startswith('text/css')
        assert 'javascript' in js.headers['content-type']
        result=client.post('/logout',data={'csrf_token':csrf(client.get('/'))},follow_redirects=False)
        assert result.status_code==303
        assert result.headers['location']=='/login'
