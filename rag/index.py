"""In-memory BM25 index over published documents and announcements.

The index holds every published chunk; access is decided per query by running
document_scope()/announcement_scope() in SQL. No department logic lives here.
"""
import json
import math
import weakref
from collections import Counter
from dataclasses import dataclass

from rag.chunking import chunk_announcement, chunk_document
from rag.tokenize import tokenize
from repositories.content import announcement_scope, document_scope

K1, B = 1.5, 0.75
MAX_PER_DOC = 3


@dataclass
class ChunkHit:
    chunk_id: str
    doc_code: str
    doc_title: str
    section_path: str
    text: str
    score: float


class Bm25Index:
    def __init__(self, chunks):
        self.chunks = chunks
        self.term_freqs = [Counter(tokenize(c.indexed_text)) for c in chunks]
        self.lengths = [sum(tf.values()) for tf in self.term_freqs]
        self.avg_length = sum(self.lengths) / len(chunks) if chunks else 0
        doc_freq = Counter(term for tf in self.term_freqs for term in tf)
        n = len(chunks)
        self.idf = {term: math.log(1 + (n - df + 0.5) / (df + 0.5)) for term, df in doc_freq.items()}

    def score(self, query_terms, allowed):
        """Yield (score, position) for chunks whose doc_code is in allowed."""
        for i, chunk in enumerate(self.chunks):
            if chunk.doc_code not in allowed:
                continue
            tf, norm = self.term_freqs[i], K1 * (1 - B + B * self.lengths[i] / self.avg_length)
            total = sum(self.idf[t] * tf[t] * (K1 + 1) / (tf[t] + norm) for t in query_terms if t in tf)
            if total > 0:
                yield total, i


# One index per Database, in this process's memory. Publishing or archiving a document rebuilds it
# (routers/admin_submissions.py), which only this process sees: with more than one uvicorn worker the
# others would keep serving the old index, so the app runs a single worker until the index is shared.
_indexes = weakref.WeakKeyDictionary()


def rebuild(db):
    """Re-read published content from the database. Call after seeding, publishing or archiving documents."""
    chunks = []
    for row in db.query("SELECT document_code,title,content FROM documents WHERE status='published' ORDER BY document_code"):
        chunks.extend(chunk_document(row['document_code'], row['title'], row['content']))
    scope, params = announcement_scope()
    for row in db.query('SELECT a.id,a.seed_key,a.title,a.content FROM announcements a WHERE ' + scope + ' ORDER BY a.id', params):
        chunks.extend(chunk_announcement(row['seed_key'] or f'id-{row["id"]}', row['title'], row['content']))
    _indexes[db] = Bm25Index(chunks)
    return _indexes[db]


def get_index(db):
    return _indexes.get(db) or rebuild(db)


def visible_codes(db, user):
    """Document codes (and ANN: codes) this user may read, straight from the SQL scopes."""
    scope, params = document_scope(user)
    codes = {r['document_code'] for r in db.query('SELECT d.document_code FROM documents d WHERE ' + scope, params)}
    scope, params = announcement_scope()
    codes |= {f'ANN:{r["seed_key"] or "id-" + str(r["id"])}' for r in db.query('SELECT a.id,a.seed_key FROM announcements a WHERE ' + scope, params)}
    return codes


def search(db, user, query, *, k=8, restrict_docs=None):
    allowed = visible_codes(db, user)
    if restrict_docs is not None:
        allowed &= set(restrict_docs)
    index = get_index(db)
    ranked = sorted(index.score(set(tokenize(query)), allowed), reverse=True)
    hits, per_doc = [], Counter()
    for score, i in ranked:
        chunk = index.chunks[i]
        if per_doc[chunk.doc_code] >= MAX_PER_DOC:
            continue
        per_doc[chunk.doc_code] += 1
        hits.append(ChunkHit(chunk.chunk_id, chunk.doc_code, chunk.doc_title, chunk.section_path, chunk.indexed_text, round(score, 4)))
        if len(hits) == k:
            break
    return hits


def visible_catalog(db, user):
    """Catalog of what this user may see, for the router prompt. Never includes other departments."""
    scope, params = document_scope(user)
    documents = [{'doc_code': r['document_code'], 'title': r['title'], 'category': r['category'], 'summary': r['summary'],
                  'applies_to': json.loads(r['applies_to']) if r['applies_to'] else None}
                 for r in db.query('SELECT d.document_code,d.title,d.category,d.summary,d.applies_to FROM documents d WHERE '
                                   + scope + ' ORDER BY d.document_code', params)]
    scope, params = announcement_scope()
    announcements = [{'doc_code': f'ANN:{r["seed_key"] or "id-" + str(r["id"])}', 'title': r['title']}
                     for r in db.query('SELECT a.id,a.seed_key,a.title FROM announcements a WHERE ' + scope
                                       + ' ORDER BY a.published_at DESC', params)]
    return {'documents': documents, 'announcements': announcements}
