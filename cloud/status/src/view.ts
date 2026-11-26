import { dayStart, scopeIds, utcDay } from "./cron";
import type { Database } from "./db";
import {
  STALE_SECONDS,
  STATES,
  dayState,
  emptyCounts,
  isState,
  overall,
  uptime,
  type Counts,
  type State,
} from "./states";
import type { Tenant } from "./tenant";

export const HISTORY_DAYS = 90;
export const RECENT_INCIDENT_SECONDS = 86400;

export interface DayView {
  day: string;
  state: State;
  uptime: number | null;
  counts: Counts;
}

export interface ComponentView {
  id: string;
  title: string;
  state: State;
  days: DayView[];
}

export interface IncidentView {
  id: number;
  title: string;
  body: string;
  state: string;
  impact: string;
  components: string[];
  startedAt: number;
  updatedAt: number;
  resolvedAt: number | null;
}

export interface MaintenanceView {
  id: number;
  title: string;
  body: string;
  components: string[];
  startsAt: number;
  endsAt: number;
  active: boolean;
}

export interface StatusView {
  tenant: { id: string; title: string; locale: string };
  overall: State;
  updatedAt: number | null;
  components: ComponentView[];
  incidents: IncidentView[];
  maintenance: MaintenanceView[];
}

type CountRow = { component_id: string; day: string } & Counts;

function counts(row: Partial<Counts>): Counts {
  const result = emptyCounts();
  for (const state of STATES) {
    result[state] = Number(row[state] ?? 0);
  }
  return result;
}

export async function loadStatus(db: Database, tenant: Tenant, now: number): Promise<StatusView> {
  const today = dayStart(now);
  const firstDay = today - (HISTORY_DAYS - 1) * 86400;
  const [components, latest, daily, incidents, maintenance] = await Promise.all([
    db
      .prepare("SELECT id, title FROM components WHERE tenant_id = ? ORDER BY position, id")
      .bind(tenant.id)
      .all<{ id: string; title: string }>(),
    db
      .prepare(
        "SELECT m.component_id, m.minute, m.state FROM components c JOIN minutes m " +
          "ON m.tenant_id = c.tenant_id AND m.component_id = c.id AND m.minute = " +
          "(SELECT MAX(minute) FROM minutes WHERE tenant_id = c.tenant_id AND component_id = c.id) " +
          "WHERE c.tenant_id = ?",
      )
      .bind(tenant.id)
      .all<{ component_id: string; minute: number; state: string }>(),
    db
      .prepare(
        `SELECT component_id, day, ${STATES.join(", ")} FROM daily ` +
          "WHERE tenant_id = ? AND day >= ?",
      )
      .bind(tenant.id, utcDay(firstDay))
      .all<CountRow>(),
    db
      .prepare(
        "SELECT id, title, body, state, impact, components, started_at, updated_at, resolved_at " +
          "FROM incidents WHERE tenant_id = ? AND (resolved_at IS NULL OR resolved_at > ?) " +
          "ORDER BY started_at DESC",
      )
      .bind(tenant.id, now - RECENT_INCIDENT_SECONDS)
      .all<{
        id: number;
        title: string;
        body: string;
        state: string;
        impact: string;
        components: string;
        started_at: number;
        updated_at: number;
        resolved_at: number | null;
      }>(),
    db
      .prepare(
        "SELECT id, title, body, components, starts_at, ends_at FROM maintenance " +
          "WHERE tenant_id = ? AND ends_at > ? ORDER BY starts_at LIMIT 20",
      )
      .bind(tenant.id, now)
      .all<{
        id: number;
        title: string;
        body: string;
        components: string;
        starts_at: number;
        ends_at: number;
      }>(),
  ]);
  const newest = latest.results.reduce<number | null>(
    (max, row) => (max === null || row.minute > max ? row.minute : max),
    null,
  );
  const current = new Map(
    latest.results
      .filter((row) => now - row.minute <= STALE_SECONDS)
      .map((row) => [row.component_id, row.state]),
  );
  const history = new Map<string, Counts>();
  for (const row of daily.results) {
    history.set(`${row.component_id}|${row.day}`, counts(row));
  }
  const titles = new Map(components.results.map((row) => [row.id, row.title]));
  const views: ComponentView[] = components.results.map((component) => {
    const state = current.get(component.id);
    const days: DayView[] = [];
    for (let index = 0; index < HISTORY_DAYS; index += 1) {
      const day = utcDay(firstDay + index * 86400);
      const dayCounts = history.get(`${component.id}|${day}`) ?? emptyCounts();
      days.push({ day, state: dayState(dayCounts), uptime: uptime(dayCounts), counts: dayCounts });
    }
    return {
      id: component.id,
      title: component.title,
      state: isState(state) ? state : "no_data",
      days,
    };
  });
  const named = (raw: string): string[] =>
    scopeIds(raw).flatMap((id) => {
      const title = titles.get(id);
      return title === undefined ? [] : [title];
    });
  return {
    tenant: { id: tenant.id, title: tenant.title, locale: tenant.locale },
    overall: overall(views.map((view) => view.state)),
    updatedAt: newest,
    components: views,
    incidents: incidents.results.map((row) => ({
      id: row.id,
      title: row.title,
      body: row.body,
      state: row.state,
      impact: row.impact,
      components: named(row.components),
      startedAt: row.started_at,
      updatedAt: row.updated_at,
      resolvedAt: row.resolved_at,
    })),
    maintenance: maintenance.results.map((row) => ({
      id: row.id,
      title: row.title,
      body: row.body,
      components: named(row.components),
      startsAt: row.starts_at,
      endsAt: row.ends_at,
      active: row.starts_at <= now,
    })),
  };
}

export function publicJson(view: StatusView): unknown {
  return {
    tenant: view.tenant.id,
    title: view.tenant.title,
    overall: view.overall,
    updated_at: view.updatedAt,
    components: view.components.map((component) => ({
      id: component.id,
      title: component.title,
      state: component.state,
      uptime_90d: uptime(
        component.days.reduce((total, day) => {
          for (const state of STATES) {
            total[state] += day.counts[state];
          }
          return total;
        }, emptyCounts()),
      ),
    })),
    incidents: view.incidents.map((incident) => ({
      id: incident.id,
      title: incident.title,
      body: incident.body,
      state: incident.state,
      impact: incident.impact,
      components: incident.components,
      started_at: incident.startedAt,
      updated_at: incident.updatedAt,
      resolved_at: incident.resolvedAt,
    })),
    maintenance: view.maintenance.map((window) => ({
      id: window.id,
      title: window.title,
      body: window.body,
      components: window.components,
      starts_at: window.startsAt,
      ends_at: window.endsAt,
    })),
  };
}
