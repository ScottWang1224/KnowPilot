-- Chat: conversations, messages, LLM call records and answer feedback. JSON is stored as TEXT, like 002.
-- Times are UTC (get_db_connection sets time_zone='+00:00'); the application writes them explicitly.
CREATE TABLE conversations (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  department_id BIGINT UNSIGNED NOT NULL,
  title VARCHAR(100) NOT NULL,
  state TEXT NULL,
  clarify_turns INT UNSIGNED NOT NULL DEFAULT 0,
  hidden_at DATETIME NULL,
  hidden_reason ENUM('user','department_change') NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_conversations_user (user_id, hidden_at, updated_at),
  CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users(id),
  CONSTRAINT fk_conversations_department FOREIGN KEY (department_id) REFERENCES departments(id),
  CONSTRAINT ck_conversations_hidden CHECK ((hidden_at IS NULL) = (hidden_reason IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE messages (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  conversation_id BIGINT UNSIGNED NOT NULL,
  role ENUM('user','assistant') NOT NULL,
  content MEDIUMTEXT NOT NULL,
  history_text MEDIUMTEXT NULL,
  kind ENUM('clarify','answer','out_of_scope','error') NULL,
  payload MEDIUMTEXT NULL,
  trace MEDIUMTEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_messages_conversation (conversation_id, id),
  CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id),
  CONSTRAINT ck_messages_kind CHECK ((role = 'assistant') = (kind IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE llm_calls (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  message_id BIGINT UNSIGNED NULL,
  purpose ENUM('router','answer') NOT NULL,
  provider VARCHAR(32) NOT NULL,
  model VARCHAR(100) NOT NULL,
  prompt_hash VARCHAR(16) NOT NULL DEFAULT '',
  input_tokens INT UNSIGNED NOT NULL DEFAULT 0,
  output_tokens INT UNSIGNED NOT NULL DEFAULT 0,
  latency_ms INT UNSIGNED NOT NULL DEFAULT 0,
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  cost_usd DOUBLE NULL,
  budget_cost_usd DOUBLE NOT NULL DEFAULT 0,
  error VARCHAR(32) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY ix_llm_calls_created (created_at),
  KEY ix_llm_calls_user (user_id, created_at),
  CONSTRAINT fk_llm_calls_user FOREIGN KEY (user_id) REFERENCES users(id),
  CONSTRAINT fk_llm_calls_message FOREIGN KEY (message_id) REFERENCES messages(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE feedback (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  message_id BIGINT UNSIGNED NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  rating ENUM('up','down') NOT NULL,
  comment VARCHAR(500) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_feedback_message_user (message_id, user_id),
  CONSTRAINT fk_feedback_message FOREIGN KEY (message_id) REFERENCES messages(id),
  CONSTRAINT fk_feedback_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
