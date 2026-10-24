"""security.yaml and the test documents of the security evaluation (phase 6). In pytest rather than only in
scripts/check_data.py, which stays on the author's machine: a fresh clone must be able to check them."""
import re

import pytest
import yaml

from core.config import ROOT

EVAL = ROOT/'data'/'eval'
FIXTURES = EVAL/'fixtures'
CANARY = re.compile(r'^KP-CANARY-[A-Z0-9]{4}$')
FIELDS = {'id', 'category', 'asker_department', 'question', 'turns', 'memory', 'fixtures', 'canary', 'must_not_reveal',
          'must_not_docs', 'expect', 'expected_docs', 'expect_safety_note', 'check_language', 'note'}
NEW_CATEGORIES = {'multi_turn', 'clarify_reply', 'fake_structure', 'obfuscation', 'off_topic', 'indirect',
                  'benign_lookalike', 'missing_smuggling', 'safety_style'}
FIXTURE_NOTE = '評測用，含刻意植入的指令'


@pytest.fixture(scope='module')
def security():
    return yaml.safe_load((EVAL/'security.yaml').read_text(encoding='utf-8'))


def real_texts():
    return [p.read_text(encoding='utf-8') for folder in ('documents', 'announcements')
            for p in (ROOT/'data'/folder).rglob('*.md')]


def fixture_files():
    return sorted(FIXTURES.rglob('*.md'))


def test_items_have_known_fields_and_cover_every_new_category(security):
    items = security['items']
    assert len({i['id'] for i in items}) == len(items)
    for item in items:
        assert set(item) <= FIELDS, item['id']
        assert re.fullmatch(r'X\d{2}', item['id'])
        assert ('question' in item) != ('turns' in item), item['id']
        assert item['expect'] in ('refuse', 'no_effect', 'answer'), item['id']
        if item['category'] in ('benign_lookalike', 'indirect') or item.get('fixtures'):
            assert item.get('expected_docs'), item['id']
    assert {i['category'] for i in items} == NEW_CATEGORIES


def test_refs_expand_to_existing_items():
    from scripts.eval_security import load_items
    items = load_items()
    by_id = {i['id']: i for i in items}
    questions = yaml.safe_load((EVAL/'questions.yaml').read_text(encoding='utf-8'))
    assert {q['id'] for q in questions if q['type'] == 'unauthorized'} <= set(by_id)
    assert by_id['Q011']['category'] == 'benign_lookalike' and by_id['Q011']['expect'] == 'answer'
    assert by_id['Q031']['must_not_docs'] and by_id['Q031']['expect'] == 'refuse'
    assert by_id['M10']['memory_invalid'] and by_id['M14']['expect_safety_note'] and by_id['M14']['max_points'] == 3
    assert sum(i['source'] == 'adversarial.yaml' for i in items) == len(
        yaml.safe_load((EVAL/'adversarial.yaml').read_text(encoding='utf-8')))


def test_a_missing_ref_id_is_an_error(tmp_path):
    from scripts.eval_security import expand_refs
    (tmp_path/'memory.yaml').write_text('- {id: M01, type: fill, asker_department: MFG, question: q}', encoding='utf-8')
    with pytest.raises(ValueError, match='M99'):
        expand_refs({'memory.yaml': {'ids': ['M01', 'M99']}}, root=tmp_path)


def test_canaries_are_well_formed_unique_and_absent_from_real_documents(security):
    canaries = [i['canary'] for i in security['items'] if i.get('canary')]
    assert canaries and len(set(canaries)) == len(canaries)
    assert all(CANARY.match(c) for c in canaries)
    texts = real_texts()
    assert not [c for c in canaries if any(c in t for t in texts)]
    # A canary planted in a test document belongs to an item that runs on the fixture database.
    planted = {c for c in canaries if any(c in p.read_text(encoding='utf-8') for p in fixture_files())}
    assert planted and all(i.get('fixtures') for i in security['items'] if i.get('canary') in planted)


def test_test_documents_are_marked_and_never_in_the_real_data_folders():
    files = fixture_files()
    assert len(files) == 5
    assert all(FIXTURE_NOTE in p.read_text(encoding='utf-8') for p in files)
    assert not [p for p in (ROOT/'data'/'documents').rglob('*.md') if '-TST-' in p.name]
    assert not list((ROOT/'data'/'announcements').glob('fixture-*.md'))


def test_test_documents_are_seeded_only_with_extra_dirs(tmp_path):
    from core.database import Database
    from rag import index
    from scripts.migrate import migrate
    from scripts.seed import seed
    plain, with_fixtures = Database.sqlite(tmp_path/'plain.db'), Database.sqlite(tmp_path/'fixtures.db')
    for db in (plain, with_fixtures):
        migrate(db, 'sqlite')
    seed(plain)
    seed(with_fixtures, extra_dirs=[FIXTURES])
    assert plain.query("SELECT COUNT(*) AS n FROM documents WHERE document_code LIKE '%%-TST-%%'", one=True)['n'] == 0
    assert plain.query("SELECT COUNT(*) AS n FROM announcements WHERE seed_key LIKE 'fixture-%%'", one=True)['n'] == 0
    departments = {r['code']: r['id'] for r in with_fixtures.query('SELECT id,code FROM departments')}
    mfg = index.visible_codes(with_fixtures, {'id': 0, 'department_id': departments['MFG']})
    rd = index.visible_codes(with_fixtures, {'id': 0, 'department_id': departments['RD']})
    assert {'MFG-TST-901', 'MFG-TST-902', 'ADM-TST-901', 'ANN:fixture-canteen'} <= mfg and 'QA-TST-901' not in mfg
    assert 'MFG-TST-901' not in rd and 'ADM-TST-901' in rd
    # The marker is a front-matter comment: the model never reads it, so it cannot make answers hedge.
    contents = [r['content'] for r in with_fixtures.query("SELECT content FROM documents WHERE document_code LIKE '%%-TST-%%'")]
    contents += [r['content'] for r in with_fixtures.query("SELECT content FROM announcements WHERE seed_key LIKE 'fixture-%%'")]
    assert len(contents) == 5 and not any(FIXTURE_NOTE in c for c in contents)
