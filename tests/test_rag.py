import pytest


@pytest.fixture(scope='module')
def seeded(tmp_path_factory):
    from core.database import Database
    from scripts.migrate import migrate
    from scripts.seed import seed
    db=Database.sqlite(tmp_path_factory.mktemp('rag')/'rag.db')
    migrate(db,'sqlite')
    seed(db)
    departments={r['code']:r['id'] for r in db.query('SELECT id,code FROM departments')}
    return db,departments


def test_tokenize_keeps_codes_whole_and_splits_chinese_into_bigrams():
    from rag.tokenize import tokenize
    tokens=tokenize('ＣＬ02-OV-03 與 RF-25、HC-230AG 出現 D09，批號 C2-260209D-04 見 MFG-SOP-003')
    for code in ['cl02-ov-03','rf-25','hc-230ag','d09','c2-260209d-04','mfg-sop-003']:
        assert code in tokens
    assert tokenize('離型力異常')==['離型','型力','力異','異常']
    assert tokenize('條')==['條']


def test_chunking_keeps_tables_drops_disclaimer_and_merges_short_sections():
    from rag.chunking import chunk_document
    table='| a | b |\n| - | - |\n'+''.join(f'| 列{i} | {"值"*40} |\n' for i in range(40))
    body=(f'## 1. 目的\n\n短。\n\n## 2. 參數\n\n{table}\n## 3. 說明\n\n{"內容很長。"*30}\n\n'
          '## 4. 備註\n\n太短\n\n本文件為虛構企業展示資料，內容與數值不得用於真實生產。')
    chunks=chunk_document('X-SOP-001','測試',body)
    assert [c.chunk_id for c in chunks]==['X-SOP-001#1','X-SOP-001#2']
    assert chunks[0].section_path=='2. 參數' and chunks[0].text.count('| 列')==40 and '短。' in chunks[0].text
    assert '太短' in chunks[1].text and not any('虛構企業展示資料' in c.text for c in chunks)
    assert chunks[1].indexed_text.startswith('【X-SOP-001 測試｜3. 說明】')


def test_chunking_splits_long_sections_at_subheadings_only_when_too_long():
    from rag.chunking import chunk_document
    long_body='## 2. 保養項目\n\n引言。\n\n### 2.1 CL-01\n\n'+'甲'*900+'\n\n### 2.2 CL-02\n\n'+'乙'*900
    chunks=chunk_document('X-PLAN-001','測試',long_body)
    assert [c.section_path for c in chunks]==['2. 保養項目 › 2.1 CL-01','2. 保養項目 › 2.2 CL-02']
    assert chunks[0].text.startswith('引言。') and '乙' not in chunks[0].text
    short_body='## 2. 保養項目\n\n### 2.1 CL-01\n\n'+'甲'*200+'\n\n### 2.2 CL-02\n\n'+'乙'*200
    assert [c.section_path for c in chunk_document('X-PLAN-002','測試',short_body)]==['2. 保養項目']


def test_search_never_returns_other_department_chunks_even_on_exact_match(seeded):
    from rag import index
    db,departments=seeded
    query=db.query("SELECT title FROM documents WHERE document_code='RD-RULE-001'",one=True)['title']+' FM-RF-025 白金 100 ppm'
    mfg={'department_id':departments['MFG']}
    rd={'department_id':departments['RD']}
    assert all(not hit.doc_code.startswith('RD-') for hit in index.search(db,mfg,query,k=50))
    assert index.search(db,rd,query,k=3)[0].doc_code=='RD-RULE-001'
    assert index.search(db,mfg,query,restrict_docs={'RD-RULE-001'})==[]


def test_visible_catalog_lists_only_company_and_own_department(seeded):
    from rag import index
    db,departments=seeded
    catalog=index.visible_catalog(db,{'department_id':departments['MFG']})
    codes={d['doc_code'] for d in catalog['documents']}
    assert len(codes)==18+18 and not any(c.startswith(('RD-','PE-')) for c in codes)
    assert 'ADM-RULE-005' in codes and len(catalog['announcements'])==20
    sop=next(d for d in catalog['documents'] if d['doc_code']=='MFG-SOP-010')
    assert sop['applies_to']=={'lines':['CL-02'],'products':['RF']}


def test_search_caps_chunks_per_document(seeded):
    from rag import index
    db,departments=seeded
    hits=index.search(db,{'department_id':departments['QA']},'D01 D02 D03 D04 D05 D06 判定 標準 等級 異常代碼',k=20)
    assert max(sum(h.doc_code==code for h in hits) for code in {h.doc_code for h in hits})<=3
