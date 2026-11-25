ALTER TABLE tenants ADD COLUMN notify_chat TEXT;

ALTER TABLE tenants ADD COLUMN notify_min TEXT NOT NULL DEFAULT 'outage'
  CHECK (notify_min IN ('outage', 'degraded', 'no_data'));

CREATE TABLE notices (
  tenant_id TEXT NOT NULL,
  component_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('operational', 'degraded', 'outage', 'no_data')),
  since INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, component_id)
) WITHOUT ROWID;
