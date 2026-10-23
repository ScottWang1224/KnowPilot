"""Company glossary (QA-TRN-001) as colloquial → official term pairs for router query rewriting.

The glossary is read from the database through document_scope(), never from data/,
so a user who cannot see the document gets no glossary.
"""
import logging
import re

from repositories.content import document_scope

log = logging.getLogger('knowpilot.rag')
GLOSSARY_DOC_CODE = 'QA-TRN-001'
TERM_COLUMN, COLLOQUIAL_COLUMN, MEANING_COLUMN = '術語', '常見口語說法', '意思'
SEPARATORS = re.compile(r'[、，,/／]')
TABLE_ROW = re.compile(r'^\s*\|(.*)\|\s*$')
SEPARATOR_ROW = re.compile(r'^[\s|:-]+$')


def _cells(line):
    match = TABLE_ROW.match(line)
    return [cell.strip() for cell in match.group(1).split('|')] if match else None


def parse(content):
    """[{term, colloquial, meaning}] from every table whose header has 術語 and 常見口語說法."""
    entries, columns = [], None
    for line in content.splitlines():
        cells = _cells(line)
        if cells is None:
            columns = None
            continue
        if columns is None:
            columns = {name: i for i, name in enumerate(cells)} if TERM_COLUMN in cells and COLLOQUIAL_COLUMN in cells else {}
            continue
        if not columns or SEPARATOR_ROW.match(line):
            continue
        cell = lambda name: cells[columns[name]] if name in columns and columns[name] < len(cells) else ''
        term = cell(TERM_COLUMN)
        if not term:
            continue
        colloquial = [w.strip() for w in SEPARATORS.split(cell(COLLOQUIAL_COLUMN))]
        entries.append({'term': term, 'meaning': cell(MEANING_COLUMN),
                        'colloquial': [w for w in dict.fromkeys(colloquial) if w and w != term]})
    return entries


def load(db, user):
    """Glossary entries this user may see; [] when the document is hidden, missing or unparsable."""
    scope, params = document_scope(user)
    row = db.query('SELECT d.content FROM documents d WHERE ' + scope + ' AND d.document_code=%s',
                   params + (GLOSSARY_DOC_CODE,), one=True)
    if not row:
        return []
    entries = parse(row['content'])
    if not entries:
        log.warning('glossary %s: no terms parsed, router runs without it', GLOSSARY_DOC_CODE)
    return entries


def format_for_prompt(entries):
    """One line per term that has colloquial phrases: 術語：口語1、口語2. Meanings are left out to save tokens."""
    return '\n'.join(f'{e["term"]}：{"、".join(e["colloquial"])}' for e in entries if e['colloquial'])
