-- Agent memory: one structured value per user and key (rag/memory.py validates both), kept apart from chat.
-- Times are UTC, and the application writes them explicitly.
CREATE TABLE user_memories (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  mem_key VARCHAR(32) NOT NULL,
  mem_value VARCHAR(64) NOT NULL,
  source ENUM('manual','chat') NOT NULL,
  source_message_id BIGINT UNSIGNED NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_user_memories_key (user_id, mem_key),
  CONSTRAINT fk_user_memories_user FOREIGN KEY (user_id) REFERENCES users(id),
  CONSTRAINT fk_user_memories_message FOREIGN KEY (source_message_id) REFERENCES messages(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
