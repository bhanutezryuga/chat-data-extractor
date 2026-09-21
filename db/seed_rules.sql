-- Chat Data Extractor — default content-type rules (see docs/RULES.md)
-- Apply: wrangler d1 execute DB --local --file=db/seed_rules.sql
--
-- v1 = DESCRIPTION-ONLY. Video download/analysis is V2; in v1 a reel/short/video
-- with no usable description goes to NEEDS_REVIEW instead of being downloaded.
-- All rules share ONE generic task template (see docs/RULES.md).

-- Single seeded user for v1 (single-user scope)
INSERT OR IGNORE INTO users (id, telegram_id, display_name)
VALUES ('user_default', NULL, 'Default User');

INSERT OR REPLACE INTO rules
  (id, name, matcher, matcher_kind, content_type, purpose, extraction_strategy, analyzer, action_template, priority, enabled)
VALUES
('rule_ig_reel', 'Instagram Reel',
 '^https?://(www\.)?instagram\.com/(reel|reels)/', 'url_regex', 'reel',
 'Short-form how-to/recipe/tip/product; the demonstration is the value.',
 'description_first', 'gemini_text',
 'Summarize the content in one short paragraph, list up to 3 key points, and propose ONE concrete actionable task (title, why it matters, first step). Tag the topic. Return as JSON.',
 10, 1),

('rule_ig_post', 'Instagram Post',
 '^https?://(www\.)?instagram\.com/p/', 'url_regex', 'post',
 'Instagram photo/carousel/video post; the caption often holds the value (lists, tips).',
 'description_first', 'gemini_text',
 'Summarize the content in one short paragraph, list up to 3 key points, and propose ONE concrete actionable task (title, why it matters, first step). Tag the topic. Return as JSON.',
 13, 1),

('rule_yt_short', 'YouTube Short',
 '^https?://(www\.)?youtube\.com/shorts/', 'url_regex', 'short',
 'Bite-size tip/news/demo.',
 'description_first', 'gemini_text',
 'Summarize the content in one short paragraph, list up to 3 key points, and propose ONE concrete actionable task (title, why it matters, first step). Tag the topic. Return as JSON.',
 11, 1),

('rule_tiktok', 'TikTok',
 '^https?://(www\.)?(tiktok\.com|vm\.tiktok\.com)/', 'url_regex', 'short',
 'Short-form video.',
 'description_first', 'gemini_text',
 'Summarize the content in one short paragraph, list up to 3 key points, and propose ONE concrete actionable task (title, why it matters, first step). Tag the topic. Return as JSON.',
 12, 1),

('rule_yt_video', 'YouTube Video',
 '^https?://(www\.)?(youtube\.com/watch|youtu\.be/)', 'url_regex', 'video',
 'Long-form tutorial/talk/review; value is depth.',
 'description_first', 'gemini_text',
 'Summarize the content in one short paragraph, list up to 3 key points, and propose ONE concrete actionable task (title, why it matters, first step). Tag the topic. Return as JSON.',
 20, 1),

('rule_pdf', 'PDF / Technical Paper',
 '(\.pdf($|\?)|arxiv\.org/(pdf|abs)/)', 'url_regex', 'pdf',
 'Reference/research; a technical paper should become a buildable use-case.',
 'pdf', 'gemini_pdf',
 'Summarize the content in one short paragraph, list up to 3 key points, and propose ONE concrete actionable task (title, why it matters, first step). Tag the topic. Return as JSON.',
 30, 1),

('rule_document', 'Shared Document',
 '(docs\.google\.com|drive\.google\.com|notion\.so|sharepoint\.com)', 'host', 'document',
 'Shared working doc/spec/sheet.',
 'readability', 'gemini_text',
 'Summarize the content in one short paragraph, list up to 3 key points, and propose ONE concrete actionable task (title, why it matters, first step). Tag the topic. Return as JSON.',
 35, 1),

('rule_article', 'Article / Web (catch-all)',
 '^https?://', 'url_regex', 'article',
 'Reading material; value is the argument/insight.',
 'readability', 'gemini_text',
 'Summarize the content in one short paragraph, list up to 3 key points, and propose ONE concrete actionable task (title, why it matters, first step). Tag the topic. Return as JSON.',
 90, 1),

-- Not reached via classify() (enabled=0, matcher never matches) — exists only so items.rule_id's
-- foreign key is satisfied for manually-typed notes (pipeline.create_note / Telegram `/note`).
('rule_note', 'Manual note',
 '(?!)', 'url_regex', 'note',
 'A personal note the user chose to write down directly (not fetched from a link).',
 'text', 'gemini_text',
 'Give this note a short, specific title (not a full sentence) and classify it into the best-fitting category. Keep the summary and key points brief — they will not be shown; the note text itself is preserved verbatim on the saved page.',
 999, 0);
