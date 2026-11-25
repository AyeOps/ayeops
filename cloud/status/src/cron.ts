import type { Database, Statement } from "./db";
import { STEP_SECONDS, notify, type NoticeRun, type NotifyTenant, type Watched } from "./notify";
import { probe, probeState, type Fetcher, type ProbeResult } from "./probe";
import { STATES, componentState, isState, type Pushed, type State } from "./states";

export const MINUTE_RETENTION_SECONDS = 2 * 86400;
export const DAILY_RETENTION_DAYS = 90;
export const RETENTION_EVERY_SECONDS = 3600;

interface ComponentRow {
  tenant_id: string;
  id: string;
  title: string;
  push: number;
  previous: string | null;
  notified: string | null;
  since: number | null;
}

interface ProbeRow {
  tenant_id: string;
  component_id: string;
  url: string;
}

interface PushRow {
  tenant_id: string;
  component_id: string;
  ts: number;
  state: string;
}

interface ScopedRow {
  tenant_id: string;
  components: string;
}

interface IncidentRow extends ScopedRow {
  impact: string;
}

export interface MinuteRun {
  skipped: boolean;
  components: number;
  probes: number;
  notices: NoticeRun;
}

const COMPONENTS =
  "SELECT c.tenant_id, c.id, c.title, c.push, p.state AS previous, n.state AS notified, n.since " +
  "FROM components c " +
  "LEFT JOIN minutes p ON p.tenant_id = c.tenant_id AND p.component_id = c.id AND p.minute = ? " +
  "LEFT JOIN notices n ON n.tenant_id = c.tenant_id AND n.component_id = c.id " +
  "ORDER BY c.tenant_id, c.position";

const NOTIFY_TENANTS =
  "SELECT id, hostname, title, locale, notify_chat, notify_min FROM tenants WHERE notify_chat IS NOT NULL";

export function utcDay(seconds: number): string {
  return new Date(seconds * 1000).toISOString().slice(0, 10);
}

export function dayStart(seconds: number): number {
  return Math.floor(seconds / 86400) * 86400;
}

export function scopeIds(raw: string): string[] {
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function covers(row: ScopedRow, tenantId: string, componentId: string): boolean {
  if (row.tenant_id !== tenantId) {
    return false;
  }
  const ids = scopeIds(row.components);
  return ids.length === 0 || ids.includes(componentId);
}

function key(tenantId: string, componentId: string): string {
  return `${tenantId}\u0000${componentId}`;
}

export function retention(db: Database, minute: number): Statement[] {
  const oldestDay = utcDay(dayStart(minute) - (DAILY_RETENTION_DAYS - 1) * 86400);
  const oldestMinute = minute - MINUTE_RETENTION_SECONDS;
  return [
    db.prepare("DELETE FROM minutes WHERE minute < ?").bind(oldestMinute),
    db.prepare("DELETE FROM runs WHERE minute < ?").bind(oldestMinute),
    db.prepare("DELETE FROM daily WHERE day < ?").bind(oldestDay),
  ];
}

function recordMinute(
  db: Database,
  tenantId: string,
  componentId: string,
  minute: number,
  state: State,
  latencyMs: number | null,
): Statement[] {
  if (!(STATES as readonly string[]).includes(state)) {
    throw new Error(`unknown state ${state}`);
  }
  return [
    db
      .prepare(
        "INSERT INTO minutes (tenant_id, component_id, minute, state, latency_ms) VALUES (?, ?, ?, ?, ?)",
      )
      .bind(tenantId, componentId, minute, state, latencyMs),
    db
      .prepare(
        `INSERT INTO daily (tenant_id, component_id, day, ${state}) VALUES (?, ?, ?, 1) ` +
          `ON CONFLICT (tenant_id, component_id, day) DO UPDATE SET ${state} = ${state} + 1`,
      )
      .bind(tenantId, componentId, utcDay(minute)),
  ];
}

async function alreadyRan(db: Database, minute: number): Promise<boolean> {
  return (await db.prepare("SELECT minute FROM runs WHERE minute = ?").bind(minute).first()) !== null;
}

function stateOrNull(value: string | null): State | null {
  return isState(value) ? value : null;
}

export async function runMinute(
  db: Database,
  now: number,
  fetcher: Fetcher,
  clock: () => number = Date.now,
  telegramToken: string | null = null,
): Promise<MinuteRun> {
  const minute = Math.floor(now / 60) * 60;
  const [components, probes, pushes, windows, incidents, tenants] = await Promise.all([
    db.prepare(COMPONENTS).bind(minute - STEP_SECONDS).all<ComponentRow>(),
    db.prepare("SELECT tenant_id, component_id, url FROM probes").all<ProbeRow>(),
    db.prepare("SELECT tenant_id, component_id, ts, state FROM push_latest").all<PushRow>(),
    db
      .prepare("SELECT tenant_id, components FROM maintenance WHERE starts_at <= ? AND ends_at > ?")
      .bind(now, now)
      .all<ScopedRow>(),
    db
      .prepare("SELECT tenant_id, components, impact FROM incidents WHERE resolved_at IS NULL")
      .all<IncidentRow>(),
    telegramToken
      ? db.prepare(NOTIFY_TENANTS).all<NotifyTenant>()
      : Promise.resolve({ results: [] as NotifyTenant[] }),
  ]);
  const urls = [...new Set(probes.results.map((row) => row.url))];
  const results = new Map<string, ProbeResult>(
    await Promise.all(urls.map(async (url) => [url, await probe(url, fetcher, clock)] as const)),
  );
  const pushed = new Map<string, Pushed>();
  for (const row of pushes.results) {
    if (isState(row.state)) {
      pushed.set(key(row.tenant_id, row.component_id), { ts: row.ts, state: row.state });
    }
  }
  const statements: Statement[] = [
    db.prepare("INSERT INTO runs (minute) VALUES (?)").bind(minute),
  ];
  const watched: Watched[] = [];
  for (const component of components.results) {
    const outcomes = probes.results
      .filter((row) => row.tenant_id === component.tenant_id && row.component_id === component.id)
      .map((row) => results.get(row.url) ?? { ok: false, latencyMs: null, status: null });
    const impacts = incidents.results
      .filter((row) => covers(row, component.tenant_id, component.id))
      .map((row) => row.impact);
    const state = componentState({
      now,
      probes: outcomes.map(probeState),
      pushed: pushed.get(key(component.tenant_id, component.id)) ?? null,
      pushExpected: component.push === 1,
      incident: impacts.includes("outage") ? "outage" : impacts.includes("degraded") ? "degraded" : null,
      maintenance: windows.results.some((row) => covers(row, component.tenant_id, component.id)),
    });
    const latencies = outcomes.flatMap((outcome) =>
      outcome.latencyMs === null ? [] : [outcome.latencyMs],
    );
    const latency = latencies.length > 0 ? Math.round(Math.max(...latencies)) : null;
    statements.push(...recordMinute(db, component.tenant_id, component.id, minute, state, latency));
    watched.push({
      tenantId: component.tenant_id,
      componentId: component.id,
      title: component.title,
      state,
      previous: stateOrNull(component.previous),
      notified: stateOrNull(component.notified),
      started: component.since,
    });
  }
  if (minute % RETENTION_EVERY_SECONDS === 0) {
    statements.push(...retention(db, minute));
  }
  const counts = { components: components.results.length, probes: urls.length };
  try {
    await db.batch(statements);
  } catch (error) {
    if (await alreadyRan(db, minute)) {
      return { skipped: true, ...counts, notices: { sent: 0, failed: 0 } };
    }
    throw error;
  }
  const notices =
    telegramToken && tenants.results.length > 0
      ? await notify(db, fetcher, telegramToken, tenants.results, watched, minute)
      : { sent: 0, failed: 0 };
  return { skipped: false, ...counts, notices };
}
