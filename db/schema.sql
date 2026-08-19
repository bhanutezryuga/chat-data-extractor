-- Chat Data Extractor — D1 (SQLite) schema
-- Apply locally: wrangler d1 execute DB --local --file=db/schema.sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id           TEXT PRIMARY KEY,
  telegram_id  TEXT UNIQUE,
  display_name TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rules (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  matcher             TEXT NOT NULL,
  matcher_kind        TEXT NOT NULL,            -- url_regex|mime|host
  content_type        TEXT NOT NULL,
  purpose             TEXT,
  extraction_strategy TEXT NOT NULL,            -- description_first|pdf|readability|video
  analyzer            TEXT,                     -- gemini_text|gemini_pdf|gemini_video
  action_template     TEXT,
  priority            INTEGER NOT NULL DEFAULT 100,
  enabled             INTEGER NOT NULL DEFAULT 1,
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rules_enabled ON rules(enabled, priority);

CREATE TABLE IF NOT EXISTS items (
  id             TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL REFERENCES users(id),
  source_chat_id TEXT,
  source_msg_id  TEXT,
  raw_text       TEXT,
  raw_url        TEXT,
  content_type   TEXT,                          -- reel|short|video|pdf|article|document|unknown
  rule_id        TEXT REFERENCES rules(id),
  action         TEXT,                           -- chosen intent: note|list|translate (smart default, overridable)
  status         TEXT NOT NULL DEFAULT 'NEW',    -- NEW|PROCESSING|EXTRACTED|ACTIONABLE|NEEDS_REVIEW|FAILED
  confidence     REAL,
  -- knowledge / PKM record
  title          TEXT,
  source         TEXT,                           -- domain the link came from
  category       TEXT,                           -- Reading|Watching|Learning|Shopping|... (see categories)
  priority       TEXT,                           -- LOW|MEDIUM|HIGH
  deadline       TEXT,                           -- next revisit datetime (spaced repetition)
  revisit_stage  INTEGER NOT NULL DEFAULT 0,     -- index into the spaced-repetition schedule
  revisit_count  INTEGER NOT NULL DEFAULT 0,
  learn_status   TEXT NOT NULL DEFAULT 'active', -- active|learned|archived
  progress       INTEGER NOT NULL DEFAULT 0,     -- 0..100
  reminded_at    TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (source_chat_id, source_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_type   ON items(content_type);
CREATE INDEX IF NOT EXISTS idx_items_user   ON items(user_id);
-- Indexes on the PKM columns (deadline/category/learn_status) are created in db.init()
-- AFTER the column migrations run, so an older items table doesn't fail this script.

CREATE TABLE IF NOT EXISTS extractions (
  id           TEXT PRIMARY KEY,
  item_id      TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  summary      TEXT,
  transcript   TEXT,
  key_points   TEXT,                            -- JSON array
  list_items   TEXT,                            -- JSON array of {name, note, link} (books/products/steps)
  translation  TEXT,                            -- Translate action: content rendered in TRANSLATE_TO
  detected_language TEXT,                        -- language Gemini detected in the content
  raw_metadata TEXT,                            -- JSON
  source       TEXT,                            -- description|oembed|readability|gemini_video|gemini_pdf
  model        TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_extractions_item ON extractions(item_id);

CREATE TABLE IF NOT EXISTS tasks (
  id                 TEXT PRIMARY KEY,
  item_id            TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  user_id            TEXT NOT NULL REFERENCES users(id),
  title              TEXT NOT NULL,
  description        TEXT,
  suggested_use_case TEXT,
  priority           TEXT NOT NULL DEFAULT 'MEDIUM',  -- LOW|MEDIUM|HIGH
  status             TEXT NOT NULL DEFAULT 'TODO',    -- TODO|IN_PROGRESS|DONE|DISMISSED
  tags               TEXT,                            -- JSON array
  due_at             TEXT,
  created_at         TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_user   ON tasks(user_id);

CREATE TABLE IF NOT EXISTS assets (
  id         TEXT PRIMARY KEY,
  item_id    TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  r2_key     TEXT NOT NULL,
  kind       TEXT NOT NULL,                     -- video|pdf|thumb|audio
  bytes      INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_assets_item ON assets(item_id);

CREATE TABLE IF NOT EXISTS processing_logs (
  id         TEXT PRIMARY KEY,
  item_id    TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  step       TEXT NOT NULL,                     -- ingest|classify|fetch|extract|generate_task
  status     TEXT NOT NULL,                     -- ok|warn|error
  detail     TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_logs_item ON processing_logs(item_id);

-- V2: Gemini token-usage metering (there is no "balance" API, so we meter actual usage)
CREATE TABLE IF NOT EXISTS gemini_usage (
  id            TEXT PRIMARY KEY,
  item_id       TEXT REFERENCES items(id) ON DELETE SET NULL,
  model         TEXT,
  kind          TEXT,                              -- text|pdf|video
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens  INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON gemini_usage(created_at);

-- PKM: extensible category taxonomy (each category maps to a typed collection)
CREATE TABLE IF NOT EXISTS categories (
  id         TEXT PRIMARY KEY,
  name       TEXT UNIQUE NOT NULL,
  collection TEXT,                               -- e.g. To-Read, To-Watch, Wishlist (nullable)
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- PKM: individual entities pulled from list_items (books, products, videos) as trackable rows
CREATE TABLE IF NOT EXISTS collection_items (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id),
  item_id    TEXT REFERENCES items(id) ON DELETE CASCADE,
  collection TEXT NOT NULL,                      -- To-Read, To-Watch, Wishlist, ...
  name       TEXT NOT NULL,
  note       TEXT,
  link       TEXT,
  done       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_collitems_collection ON collection_items(collection);
CREATE INDEX IF NOT EXISTS idx_collitems_item       ON collection_items(item_id);

-- PKM: revisit audit trail (metrics)
CREATE TABLE IF NOT EXISTS revisits (
  id         TEXT PRIMARY KEY,
  item_id    TEXT REFERENCES items(id) ON DELETE CASCADE,
  action     TEXT NOT NULL,                      -- revisited|snoozed|learned
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_revisits_item ON revisits(item_id);

-- Logseq two-way sync: remember each exported page's mtime so read-back only re-parses changed files
CREATE TABLE IF NOT EXISTS logseq_state (
  path       TEXT PRIMARY KEY,
  mtime      REAL,
  synced_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- small key/value store for app state (e.g. when the weekly review digest was last sent)
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
