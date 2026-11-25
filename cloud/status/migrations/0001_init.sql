CREATE TABLE tenants (
  id TEXT PRIMARY KEY,
  hostname TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  locale TEXT NOT NULL DEFAULT 'en',
  secret TEXT
);

CREATE TABLE components (
  tenant_id TEXT NOT NULL,
  id TEXT NOT NULL,
  title TEXT NOT NULL,
  position INTEGER NOT NULL,
  push INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE probes (
  tenant_id TEXT NOT NULL,
  component_id TEXT NOT NULL,
  url TEXT NOT NULL,
  PRIMARY KEY (tenant_id, component_id, url)
);

CREATE TABLE push_latest (
  tenant_id TEXT NOT NULL,
  component_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('operational', 'degraded', 'outage', 'maintenance', 'no_data')),
  PRIMARY KEY (tenant_id, component_id)
);

CREATE TABLE minutes (
  tenant_id TEXT NOT NULL,
  component_id TEXT NOT NULL,
  minute INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('operational', 'degraded', 'outage', 'maintenance', 'no_data')),
  latency_ms INTEGER,
  PRIMARY KEY (tenant_id, component_id, minute)
) WITHOUT ROWID;

CREATE TABLE runs (
  minute INTEGER PRIMARY KEY
);

CREATE TABLE daily (
  tenant_id TEXT NOT NULL,
  component_id TEXT NOT NULL,
  day TEXT NOT NULL,
  operational INTEGER NOT NULL DEFAULT 0,
  degraded INTEGER NOT NULL DEFAULT 0,
  outage INTEGER NOT NULL DEFAULT 0,
  maintenance INTEGER NOT NULL DEFAULT 0,
  no_data INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, component_id, day)
) WITHOUT ROWID;

CREATE TABLE incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK (state IN ('investigating', 'identified', 'monitoring', 'resolved')),
  impact TEXT NOT NULL CHECK (impact IN ('none', 'degraded', 'outage')),
  components TEXT NOT NULL DEFAULT '[]',
  started_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  resolved_at INTEGER
);

CREATE INDEX incidents_open ON incidents (tenant_id, resolved_at);

CREATE TABLE maintenance (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  components TEXT NOT NULL DEFAULT '[]',
  starts_at INTEGER NOT NULL,
  ends_at INTEGER NOT NULL CHECK (ends_at > starts_at)
);

CREATE INDEX maintenance_window ON maintenance (tenant_id, ends_at);
