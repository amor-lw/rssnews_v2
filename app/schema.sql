PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  observe_only INTEGER NOT NULL DEFAULT 0,
  trust_weight REAL NOT NULL DEFAULT 0.6,
  topic_hint TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
  guid TEXT PRIMARY KEY,
  canonical_url TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT '',
  source_name TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL,
  discovered_at TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  content_text TEXT NOT NULL DEFAULT '',
  lang TEXT,
  hn_id INTEGER,
  hn_points REAL NOT NULL DEFAULT 0,
  hn_comments INTEGER NOT NULL DEFAULT 0,
  source_rank INTEGER,
  source_list TEXT NOT NULL DEFAULT '',
  source_family TEXT NOT NULL DEFAULT '',
  base_score REAL NOT NULL DEFAULT 0,
  bonus_score REAL NOT NULL DEFAULT 0,
  hot_signal_type TEXT NOT NULL DEFAULT '',
  raw_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_sources (
  article_guid TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL DEFAULT '',
  discovered_at TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (article_guid, source_name, external_id),
  FOREIGN KEY (article_guid) REFERENCES articles(guid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS classifications (
  article_guid TEXT NOT NULL,
  run_id INTEGER NOT NULL DEFAULT 0,
  profile TEXT NOT NULL,
  bucket TEXT NOT NULL,
  importance_score REAL NOT NULL DEFAULT 0,
  relevance_score REAL NOT NULL DEFAULT 0,
  confidence_score REAL NOT NULL DEFAULT 0,
  reason_code TEXT NOT NULL DEFAULT '',
  reason_summary TEXT NOT NULL DEFAULT '',
  matched_terms_json TEXT NOT NULL DEFAULT '[]',
  score_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  PRIMARY KEY (article_guid, run_id, profile),
  FOREIGN KEY (article_guid) REFERENCES articles(guid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feed_items (
  feed_name TEXT NOT NULL,
  article_guid TEXT NOT NULL,
  rank INTEGER NOT NULL DEFAULT 0,
  added_at TEXT NOT NULL,
  persistent INTEGER NOT NULL DEFAULT 0,
  run_id INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (feed_name, article_guid),
  FOREIGN KEY (article_guid) REFERENCES articles(guid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_issues (
  issue_date TEXT NOT NULL,
  bucket TEXT NOT NULL,
  archive_path TEXT NOT NULL,
  link TEXT NOT NULL,
  guid TEXT NOT NULL,
  item_count INTEGER NOT NULL DEFAULT 0,
  top_titles_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  PRIMARY KEY (issue_date, bucket)
);

CREATE TABLE IF NOT EXISTS daily_issue_items (
  issue_date TEXT NOT NULL,
  bucket TEXT NOT NULL,
  article_guid TEXT NOT NULL,
  rank INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (issue_date, bucket, article_guid),
  FOREIGN KEY (issue_date, bucket) REFERENCES daily_issues(issue_date, bucket) ON DELETE CASCADE,
  FOREIGN KEY (article_guid) REFERENCES articles(guid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  article_guid TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT '',
  remote_addr TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_articles (
  article_guid TEXT PRIMARY KEY,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY (article_guid) REFERENCES articles(guid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fetch_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS event_clusters (
  cluster_id TEXT NOT NULL,
  run_id INTEGER NOT NULL,
  canonical_title TEXT NOT NULL DEFAULT '',
  representative_guid TEXT NOT NULL DEFAULT '',
  representative_url TEXT NOT NULL DEFAULT '',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  source_count INTEGER NOT NULL DEFAULT 0,
  independent_source_count INTEGER NOT NULL DEFAULT 0,
  hotness_score REAL NOT NULL DEFAULT 0,
  confidence_score REAL NOT NULL DEFAULT 0,
  bucket TEXT NOT NULL DEFAULT '',
  reason_code TEXT NOT NULL DEFAULT '',
  score_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  PRIMARY KEY (cluster_id, run_id)
);

CREATE TABLE IF NOT EXISTS event_cluster_items (
  cluster_id TEXT NOT NULL,
  run_id INTEGER NOT NULL,
  article_guid TEXT NOT NULL,
  source_name TEXT NOT NULL DEFAULT '',
  source_family TEXT NOT NULL DEFAULT '',
  list_name TEXT NOT NULL DEFAULT '',
  rank_in_source INTEGER,
  match_type TEXT NOT NULL DEFAULT '',
  similarity_score REAL NOT NULL DEFAULT 0,
  base_score REAL NOT NULL DEFAULT 0,
  bonus_score REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (cluster_id, run_id, article_guid, source_name, list_name),
  FOREIGN KEY (cluster_id, run_id) REFERENCES event_clusters(cluster_id, run_id) ON DELETE CASCADE,
  FOREIGN KEY (article_guid) REFERENCES articles(guid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hn_hot_queries (
  name TEXT PRIMARY KEY,
  query TEXT NOT NULL,
  min_points INTEGER NOT NULL DEFAULT 100,
  item_limit INTEGER NOT NULL DEFAULT 20,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hn_hot_items (
  query_name TEXT NOT NULL,
  article_guid TEXT NOT NULL,
  rank INTEGER NOT NULL DEFAULT 0,
  points REAL NOT NULL DEFAULT 0,
  comments INTEGER NOT NULL DEFAULT 0,
  matched_terms_json TEXT NOT NULL DEFAULT '[]',
  fetched_at TEXT NOT NULL,
  persistent INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (query_name, article_guid),
  FOREIGN KEY (query_name) REFERENCES hn_hot_queries(name) ON DELETE CASCADE,
  FOREIGN KEY (article_guid) REFERENCES articles(guid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rule_terms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile TEXT NOT NULL,
  term_type TEXT NOT NULL,
  term TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  notes TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'admin',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(profile, term_type, term)
);

CREATE TABLE IF NOT EXISTS rule_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_domain ON articles(domain);
CREATE INDEX IF NOT EXISTS idx_articles_source_rank ON articles(source_family, source_list, source_rank);
CREATE INDEX IF NOT EXISTS idx_classifications_bucket ON classifications(bucket, profile, importance_score);
CREATE INDEX IF NOT EXISTS idx_feed_items_feed_rank ON feed_items(feed_name, rank);
CREATE INDEX IF NOT EXISTS idx_feedback_events_guid ON feedback_events(article_guid);
CREATE INDEX IF NOT EXISTS idx_rule_terms_profile ON rule_terms(profile, term_type, enabled);
CREATE INDEX IF NOT EXISTS idx_event_clusters_run_bucket ON event_clusters(run_id, bucket, hotness_score);
