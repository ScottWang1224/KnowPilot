-- Local development and tests only. Keep in sync with migrations/mysql.
-- documents.status has no CHECK here, so 'archived' needs no table rebuild.
ALTER TABLE documents ADD COLUMN owner_department_id INTEGER REFERENCES departments(id);

UPDATE documents SET owner_department_id = (
  SELECT p.id FROM departments p WHERE p.code = substr(documents.document_code, 1, instr(documents.document_code, '-') - 1));

CREATE TABLE document_submissions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('create','revise')),
    document_id INTEGER REFERENCES documents(id),
    base_version TEXT,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('company','department')),
    change_note TEXT,
    version_bump TEXT CHECK (version_bump IN ('minor','major')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','withdrawn')),
    submitted_by INTEGER NOT NULL REFERENCES users(id),
    submitted_at TEXT NOT NULL,
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    review_note TEXT,
    flags TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (kind <> 'revise' OR (document_id IS NOT NULL AND base_version IS NOT NULL))
);
CREATE INDEX ix_submissions_user ON document_submissions(submitted_by, status, submitted_at);
CREATE INDEX ix_submissions_queue ON document_submissions(status, submitted_at);
CREATE INDEX ix_submissions_document ON document_submissions(document_id, status);

CREATE TABLE document_revisions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    submission_id INTEGER REFERENCES document_submissions(id),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('company','department')),
    version TEXT NOT NULL,
    document_updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_revisions_document ON document_revisions(document_id, id);
