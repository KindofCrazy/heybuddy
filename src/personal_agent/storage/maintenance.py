"""Append-only schema upgrades for bounded organization maintenance; Journal unchanged."""

CONSISTENCY_SCHEMA = (
    "ALTER TABLE topic_threads ADD COLUMN source_revision INTEGER NOT NULL DEFAULT 0",
    """CREATE TABLE thread_repairs (
        thread_id TEXT PRIMARY KEY REFERENCES topic_threads(id),
        source_revision INTEGER NOT NULL, thread_revision INTEGER NOT NULL,
        revision INTEGER NOT NULL, after_time TEXT NOT NULL DEFAULT '',
        after_id TEXT NOT NULL DEFAULT '', working_state TEXT NOT NULL DEFAULT ''
        CHECK(length(CAST(working_state AS BLOB))<=8192),
        source_count INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
        error_code TEXT NOT NULL DEFAULT '', metrics TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE thread_repair_sources (
        thread_id TEXT NOT NULL REFERENCES thread_repairs(thread_id),
        episode_id TEXT NOT NULL REFERENCES episodes(id), episode_revision INTEGER NOT NULL,
        ordinal INTEGER NOT NULL, PRIMARY KEY(thread_id,episode_id))""",
    """CREATE TABLE thread_state_sources (
        thread_id TEXT NOT NULL, thread_revision INTEGER NOT NULL,
        episode_id TEXT NOT NULL REFERENCES episodes(id), episode_revision INTEGER NOT NULL,
        ordinal INTEGER NOT NULL, PRIMARY KEY(thread_id,thread_revision,episode_id),
        FOREIGN KEY(thread_id,thread_revision) REFERENCES thread_versions(thread_id,revision))""",
    """CREATE TRIGGER thread_episode_insert AFTER INSERT ON episodes
        WHEN NEW.thread_id IS NOT NULL BEGIN
        UPDATE topic_threads SET dirty=1,source_revision=source_revision+1 WHERE id=NEW.thread_id;
        END""",
    """CREATE TRIGGER thread_episode_move AFTER UPDATE OF thread_id ON episodes
        WHEN OLD.thread_id IS NOT NEW.thread_id BEGIN
        UPDATE topic_threads SET dirty=1,source_revision=source_revision+1
        WHERE id=OLD.thread_id OR id=NEW.thread_id; END""",
    """CREATE TRIGGER thread_episode_content AFTER UPDATE OF payload ON episodes
        WHEN NEW.thread_id IS NOT NULL AND (
          json_extract(OLD.payload,'$.title') IS NOT json_extract(NEW.payload,'$.title') OR
          json_extract(OLD.payload,'$.summary') IS NOT json_extract(NEW.payload,'$.summary') OR
          json_extract(OLD.payload,'$.highlights') IS NOT
          json_extract(NEW.payload,'$.highlights') OR
          json_extract(OLD.payload,'$.run_ids') IS NOT json_extract(NEW.payload,'$.run_ids')) BEGIN
        UPDATE topic_threads SET dirty=1,source_revision=source_revision+1 WHERE id=NEW.thread_id;
        END""",
    """CREATE TRIGGER thread_episode_delete AFTER DELETE ON episodes
        WHEN OLD.thread_id IS NOT NULL BEGIN
        UPDATE topic_threads SET dirty=1,source_revision=source_revision+1 WHERE id=OLD.thread_id;
        END""",
    "CREATE INDEX thread_dirty_order ON topic_threads(agent_id,dirty,last_active_at,id)",
)

SCHEDULE_SCHEMA = (
    """CREATE TABLE organization_schedule (
        agent_id TEXT PRIMARY KEY REFERENCES agents(id), stream_id TEXT NOT NULL,
        last_activity_at TEXT NOT NULL, thread_due_at TEXT NOT NULL,
        last_thread_check_at TEXT, cooldown_until TEXT, last_cycle_at TEXT,
        last_episode_success_at TEXT, last_thread_success_at TEXT, last_repair_success_at TEXT,
        last_batches INTEGER NOT NULL DEFAULT 0, last_requests INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(stream_id,agent_id) REFERENCES conversations(id,agent_id))""",
)
