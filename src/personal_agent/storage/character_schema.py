"""Schema 12: character receipts and transient state; no runtime imports."""

SCHEMA = (
    """CREATE TABLE character_runs (
       run_id TEXT PRIMARY KEY REFERENCES runs(id), agent_id TEXT NOT NULL REFERENCES agents(id),
       character_id TEXT NOT NULL, package_hash TEXT NOT NULL)""",
    "CREATE INDEX character_run_scope ON character_runs(agent_id,character_id,package_hash)",
    """CREATE TRIGGER character_run_agent BEFORE INSERT ON character_runs WHEN NOT EXISTS (
       SELECT 1 FROM runs r JOIN conversations c ON c.id=r.conversation_id
       WHERE r.id=NEW.run_id AND c.agent_id=NEW.agent_id)
       BEGIN SELECT RAISE(ABORT,'Character Run scope mismatch'); END""",
    """CREATE TABLE character_states (
       agent_id TEXT NOT NULL REFERENCES agents(id), character_id TEXT NOT NULL,
       package_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
       source_position INTEGER NOT NULL DEFAULT 0, source_at TEXT,
       last_attempt_at TEXT, payload TEXT CHECK(length(CAST(payload AS BLOB))<=4096),
       PRIMARY KEY(agent_id,character_id))""",
    """CREATE TABLE character_state_sources (
       agent_id TEXT NOT NULL REFERENCES agents(id), character_id TEXT NOT NULL,
       package_hash TEXT NOT NULL, episode_id TEXT NOT NULL REFERENCES episodes(id),
       episode_revision INTEGER NOT NULL, source_hash TEXT NOT NULL,
       state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', metrics TEXT NOT NULL DEFAULT '{}',
       PRIMARY KEY(agent_id,character_id,package_hash,episode_id,episode_revision))""",
    "CREATE INDEX character_source_scope ON character_state_sources(agent_id,character_id,state)",
)
