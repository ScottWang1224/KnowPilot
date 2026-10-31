-- Document submissions and review. documents keeps only published and archived content, so document_scope(),
-- the document center and the RAG index never see drafts. Times are UTC, and the application writes them explicitly.
ALTER TABLE documents
  ADD COLUMN owner_department_id BIGINT UNSIGNED NULL AFTER department_id,
  ADD CONSTRAINT fk_documents_owner FOREIGN KEY (owner_department_id) REFERENCES departments(id);

UPDATE documents d JOIN departments p ON p.code = SUBSTRING_INDEX(d.document_code, '-', 1)
  SET d.owner_department_id = p.id;

ALTER TABLE documents MODIFY status ENUM('draft','published','archived') NOT NULL DEFAULT 'draft';

CREATE TABLE document_submissions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  kind ENUM('create','revise') NOT NULL,
  document_id BIGINT UNSIGNED NULL,
  base_version VARCHAR(16) NULL,
  department_id BIGINT UNSIGNED NOT NULL,
  title VARCHAR(200) NOT NULL,
  summary VARCHAR(500) NOT NULL,
  content MEDIUMTEXT NOT NULL,
  category VARCHAR(32) NOT NULL,
  visibility ENUM('company','department') NOT NULL,
  change_note VARCHAR(500) NULL,
  version_bump ENUM('minor','major') NULL,
  status ENUM('pending','approved','rejected','withdrawn') NOT NULL DEFAULT 'pending',
  submitted_by BIGINT UNSIGNED NOT NULL,
  submitted_at DATETIME NOT NULL,
  reviewed_by BIGINT UNSIGNED NULL,
  reviewed_at DATETIME NULL,
  review_note VARCHAR(1000) NULL,
  flags TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_submissions_user (submitted_by, status, submitted_at),
  KEY ix_submissions_queue (status, submitted_at),
  KEY ix_submissions_document (document_id, status),
  CONSTRAINT fk_submissions_document FOREIGN KEY (document_id) REFERENCES documents(id),
  CONSTRAINT fk_submissions_department FOREIGN KEY (department_id) REFERENCES departments(id),
  CONSTRAINT fk_submissions_user FOREIGN KEY (submitted_by) REFERENCES users(id),
  CONSTRAINT fk_submissions_reviewer FOREIGN KEY (reviewed_by) REFERENCES users(id),
  CONSTRAINT ck_submissions_revise CHECK (kind <> 'revise' OR (document_id IS NOT NULL AND base_version IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE document_revisions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  document_id BIGINT UNSIGNED NOT NULL,
  submission_id BIGINT UNSIGNED NULL,
  title VARCHAR(200) NOT NULL,
  summary VARCHAR(500) NOT NULL,
  content LONGTEXT NOT NULL,
  category VARCHAR(32) NOT NULL,
  visibility ENUM('company','department') NOT NULL,
  version VARCHAR(16) NOT NULL,
  document_updated_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_revisions_document (document_id, id),
  CONSTRAINT fk_revisions_document FOREIGN KEY (document_id) REFERENCES documents(id),
  CONSTRAINT fk_revisions_submission FOREIGN KEY (submission_id) REFERENCES document_submissions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
