import os
import uuid
import boto3

import mysql.connector

from dotenv import load_dotenv

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()
S3_BUCKET = os.getenv("S3_BUCKET")
AWS_REGION = os.getenv("AWS_REGION")
CLOUDFRONT_DOMAIN = os.getenv("CLOUDFRONT_DOMAIN")

s3 = boto3.client("s3", region_name=AWS_REGION)

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
    )


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/api/posts")
def create_post(content: str = Form(...), image: UploadFile = File(...)):
    extension = os.path.splitext(image.filename)[1]

    filename = f"{uuid.uuid4()}{extension}"

    s3.upload_fileobj(
        image.file, S3_BUCKET, filename, ExtraArgs={"ContentType": image.content_type}
    )

    image_url = f"https://{CLOUDFRONT_DOMAIN}/{filename}"

    connection = get_db_connection()
    cursor = connection.cursor()

    sql = """
        INSERT INTO posts (content, image_url)
        VALUES (%s, %s)
    """

    cursor.execute(sql, (content, image_url))

    connection.commit()

    post_id = cursor.lastrowid

    cursor.close()
    connection.close()

    return {
        "ok": True,
        "data": {"id": post_id, "content": content, "image_url": image_url},
    }


@app.get("/api/posts")
def get_posts():
    connection = get_db_connection()

    cursor = connection.cursor(dictionary=True)

    sql = """
        SELECT id, content, image_url, created_at
        FROM posts
        ORDER BY id DESC
    """

    cursor.execute(sql)

    posts = cursor.fetchall()

    cursor.close()
    connection.close()

    return {"ok": True, "data": posts}
