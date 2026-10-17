-- documents.applies_to: JSON text from data/ front matter (lines, products, shifts), NULL when not set.
ALTER TABLE documents ADD COLUMN applies_to TEXT NULL;
