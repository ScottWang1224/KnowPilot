"""Run explicitly: python -m scripts.seed. Insert-only fictional departments and content.

Documents and announcements are read from data/ (Markdown with YAML front matter).
Existing rows, matched by document_code or seed_key, stay unchanged.
Accounts come from registration or scripts.seed_admin.
"""
import json
from datetime import date, datetime, timedelta, timezone

import yaml
from dotenv import load_dotenv
from core.config import ROOT
from core.database import Database

DATA = ROOT/'data'
TAIPEI = timezone(timedelta(hours=8))
DEPARTMENTS = [('MFG','製造部'),('PE','製程工程部'),('QA','品質保證部'),('RD','研發部'),('ADM','行政管理部')]


def read_front_matter(path):
    """Return (metadata, body) for a Markdown file that starts with a --- YAML block."""
    text=path.read_text(encoding='utf-8')
    if not text.startswith('---\n') or (end:=text.find('\n---\n',4))<0:
        raise ValueError(f'{path}: missing front matter')
    return yaml.safe_load(text[4:end]) or {}, text[end+5:].strip()


def taipei_to_utc(value):
    """Front matter dates are Taiwan local time; the database stores naive UTC."""
    if isinstance(value,date) and not isinstance(value,datetime):
        value=datetime.combine(value,datetime.min.time())
    elif not isinstance(value,datetime):
        value=datetime.strptime(str(value),'%Y-%m-%d %H:%M')
    return value.replace(tzinfo=TAIPEI).astimezone(timezone.utc).replace(tzinfo=None)


def load_documents():
    for path in sorted((DATA/'documents').glob('*/*.md')):
        meta,body=read_front_matter(path)
        if meta['visibility']=='department' and not meta.get('department'):
            raise ValueError(f'{path}: department document requires department')
        yield meta,body


def load_announcements():
    for path in sorted((DATA/'announcements').glob('*.md')):
        yield read_front_matter(path)


def seed(db):
    with db.transaction() as cursor:
        for code,name in DEPARTMENTS:
            cursor.execute('SELECT id FROM departments WHERE code=%s',(code,))
            if not cursor.fetchone():
                cursor.execute('INSERT INTO departments(code,name) VALUES(%s,%s)',(code,name))
        cursor.execute('SELECT id,code FROM departments')
        departments={r['code']:r['id'] for r in cursor.fetchall()}
        for meta,body in load_announcements():
            cursor.execute('SELECT id FROM announcements WHERE seed_key=%s',(meta['seed_key'],))
            if not cursor.fetchone():
                cursor.execute('''INSERT INTO announcements(seed_key,title,content,category,is_pinned,status,published_at)
                                  VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                               (meta['seed_key'],meta['title'],body,meta['category'],int(bool(meta['is_pinned'])),
                                'published',taipei_to_utc(meta['published_at'])))
        for meta,body in load_documents():
            cursor.execute('SELECT id FROM documents WHERE document_code=%s',(meta['code'],))
            if not cursor.fetchone():
                # Company documents keep department_id NULL so the UI labels them 全公司;
                # the effective date doubles as the document's last-updated time.
                department_id=departments[meta['department']] if meta['visibility']=='department' else None
                effective=taipei_to_utc(meta['effective_date'])
                applies_to=json.dumps(meta['applies_to'],ensure_ascii=False) if meta.get('applies_to') else None
                cursor.execute('''INSERT INTO documents(document_code,title,summary,content,category,department_id,
                                  visibility,version,status,applies_to,created_at,updated_at)
                                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                               (meta['code'],meta['title'],meta['summary'],body,meta['category'],department_id,
                                meta['visibility'],str(meta['version']),'published',applies_to,effective,effective))


if __name__=='__main__':
    load_dotenv(ROOT/'.env')
    seed(Database.from_env())
    print('Seed complete. Existing records were preserved.')
