"""Opt-in legacy image APIs. Existing posts/objects are preserved.

No S3 client is created unless an authenticated administrator uploads an image.
"""
import io
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from core.security import require_user, verify_csrf

router=APIRouter(dependencies=[Depends(require_user)])


def require_admin(request):
    if request.state.user['role']!='admin': raise HTTPException(403)


@router.get('/api/posts')
def get_posts(request:Request):
    require_admin(request)
    posts=request.app.state.db.query('SELECT id,content,image_url,created_at FROM posts ORDER BY id DESC')
    return {'ok':True,'data':posts}


@router.post('/api/posts')
def create_post(request:Request,content:str=Form(...,max_length=2000),image:UploadFile=File(...),csrf_token:str=Form('')):
    require_admin(request)
    verify_csrf(request,csrf_token)
    data=image.file.read(5*1024*1024+1)
    if len(data)>5*1024*1024: raise HTTPException(413)
    if image.content_type=='image/png' and data.startswith(b'\x89PNG\r\n\x1a\n'):
        extension='.png'
    elif image.content_type=='image/jpeg' and data.startswith(b'\xff\xd8\xff'):
        extension='.jpg'
    else: raise HTTPException(415)
    bucket=os.getenv('S3_BUCKET')
    domain=os.getenv('CLOUDFRONT_DOMAIN')
    if not bucket or not domain: raise HTTPException(503)
    import boto3
    s3=boto3.client('s3',region_name=os.getenv('AWS_REGION'))
    filename=f'{uuid.uuid4()}{extension}'
    s3.upload_fileobj(io.BytesIO(data),bucket,filename,ExtraArgs={'ContentType':image.content_type})
    image_url=f'https://{domain}/{filename}'
    with request.app.state.db.transaction() as cursor:
        cursor.execute('INSERT INTO posts(content,image_url) VALUES(%s,%s)',(content,image_url))
        post_id=cursor.lastrowid
    return {'ok':True,'data':{'id':post_id,'content':content,'image_url':image_url}}
