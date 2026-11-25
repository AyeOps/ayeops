import type { Database } from "./db";

export interface Tenant {
  id: string;
  hostname: string;
  title: string;
  locale: string;
  secret: string | null;
  access_team: string | null;
  access_aud: string | null;
}

const COLUMNS = "id, hostname, title, locale, secret, access_team, access_aud";

export async function tenantByHost(db: Database, hostname: string): Promise<Tenant | null> {
  return db
    .prepare(`SELECT ${COLUMNS} FROM tenants WHERE hostname = ?`)
    .bind(hostname.toLowerCase())
    .first<Tenant>();
}

export async function tenantById(db: Database, id: string): Promise<Tenant | null> {
  return db.prepare(`SELECT ${COLUMNS} FROM tenants WHERE id = ?`).bind(id).first<Tenant>();
}

export async function componentIds(db: Database, tenantId: string): Promise<Set<string>> {
  const { results } = await db
    .prepare("SELECT id FROM components WHERE tenant_id = ?")
    .bind(tenantId)
    .all<{ id: string }>();
  return new Set(results.map((row) => row.id));
}
