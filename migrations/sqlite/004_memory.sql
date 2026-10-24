-- Local development and tests only. Keep in sync with migrations/mysql.
CREATE TABLE user_memories (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    mem_key TEXT NOT NULL,
    mem_value TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual','chat')),
    source_message_id INTEGER REFERENCES messages(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, mem_key)
);
