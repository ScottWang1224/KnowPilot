-- Local development and tests only. Keep in sync with migrations/mysql.
ALTER TABLE documents ADD COLUMN applies_to TEXT;
