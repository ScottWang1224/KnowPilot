-- Local development and tests only. Keep in sync with migrations/mysql.
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    department_id INTEGER NOT NULL REFERENCES departments(id),
    title TEXT NOT NULL,
    state TEXT,
    clarify_turns INTEGER NOT NULL DEFAULT 0,
    hidden_at TEXT,
    hidden_reason TEXT CHECK (hidden_reason IN ('user','department_change')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((hidden_at IS NULL) = (hidden_reason IS NULL))
);
CREATE INDEX ix_conversations_user ON conversations(user_id, hidden_at, updated_at);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL,
    history_text TEXT,
    kind TEXT CHECK (kind IN ('clarify','answer','out_of_scope','error')),
    payload TEXT,
    trace TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((role = 'assistant') = (kind IS NOT NULL))
);
CREATE INDEX ix_messages_conversation ON messages(conversation_id, id);

CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    message_id INTEGER REFERENCES messages(id),
    purpose TEXT NOT NULL CHECK (purpose IN ('router','answer')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    budget_cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_llm_calls_created ON llm_calls(created_at);
CREATE INDEX ix_llm_calls_user ON llm_calls(user_id, created_at);

CREATE TABLE feedback (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    rating TEXT NOT NULL CHECK (rating IN ('up','down')),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (message_id, user_id)
);
