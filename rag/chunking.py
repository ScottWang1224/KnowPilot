"""Split documents into retrievable sections.

Documents split at `##` headings (deeper headings stay inside their `##` section).
Tables are never cut. Short sections merge into the previous one; long sections
without tables split at blank lines.
"""
import re
from dataclasses import dataclass

MIN_CHARS = 80
MAX_CHARS = 1500
DISCLAIMER = re.compile(r'^本文件為虛構企業展示資料.*$', re.M)
TABLE_ROW = re.compile(r'^\s*\|.*\|\s*$', re.M)


@dataclass
class Chunk:
    chunk_id: str
    doc_code: str
    doc_title: str
    section_path: str
    text: str

    @property
    def indexed_text(self):
        """Text sent to BM25 and the LLM: a header makes every chunk understandable on its own."""
        return f'【{self.doc_code} {self.doc_title}｜{self.section_path}】\n{self.text}'


def _sections(body):
    """Yield (section_path, text) pairs split at `##` headings; content before the first is 概要."""
    path, lines = '概要', []
    for line in body.splitlines():
        heading = re.match(r'^##\s+(.+?)\s*$', line)
        if heading:
            yield path, '\n'.join(lines).strip()
            path, lines = heading.group(1), []
        else:
            lines.append(line)
    yield path, '\n'.join(lines).strip()


def _split_long(text):
    if len(text) <= MAX_CHARS or TABLE_ROW.search(text):
        return [text]
    parts, current = [], ''
    for paragraph in re.split(r'\n\s*\n', text):
        if current and len(current) + len(paragraph) > MAX_CHARS:
            parts.append(current)
            current = paragraph
        else:
            current = f'{current}\n\n{paragraph}' if current else paragraph
    return parts + ([current] if current else [])


def chunk_document(doc_code, title, body):
    body = DISCLAIMER.sub('', body).strip()
    merged = []  # [section_path, text]
    for path, text in _sections(body):
        if not text:
            continue
        if merged and len(text) < MIN_CHARS:
            merged[-1][1] += f'\n\n{path}\n{text}'
        else:
            merged.append([path, text])
    # A short first section has nothing before it; fold it into the next one instead.
    if len(merged) > 1 and len(merged[0][1]) < MIN_CHARS:
        first_path, first_text = merged.pop(0)
        merged[0][1] = f'{first_text}\n\n{merged[0][1]}' if first_path == '概要' else f'{first_path}\n{first_text}\n\n{merged[0][1]}'
    chunks = []
    for path, text in merged:
        for part in _split_long(text):
            chunks.append(Chunk(f'{doc_code}#{len(chunks) + 1}', doc_code, title, path, part))
    return chunks


def chunk_announcement(seed_key, title, body):
    code = f'ANN:{seed_key}'
    return [Chunk(f'{code}#1', code, title, '公告', body.strip())]
