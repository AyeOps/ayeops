import type { Database } from "./db";
import { DEFAULT_LOCALE, LOCALES, translate, type Messages } from "./i18n";
import { PROBE_TIMEOUT_MS, USER_AGENT, type Fetcher } from "./probe";
import { SEVERITY, type State } from "./states";

export const TELEGRAM_API = "https://api.telegram.org";
export const MIN_STATES = ["outage", "degraded", "no_data"] as const;
export type MinState = (typeof MIN_STATES)[number];
export const MAX_NOTICE_LINES = 30;
export const STEP_SECONDS = 60;

export interface NotifyTenant {
  id: string;
  hostname: string;
  title: string;
  locale: string;
  notify_chat: string;
  notify_min: string;
}

export interface Watched {
  tenantId: string;
  componentId: string;
  title: string;
  state: State;
  previous: State | null;
  notified: State | null;
  started: number | null;
}

export interface Change {
  tenantId: string;
  componentId: string;
  title: string;
  from: State;
  to: State;
  at: number;
  started: number;
}

export interface NoticeRun {
  sent: number;
  failed: number;
}

const UPSERT =
  "INSERT INTO notices (tenant_id, component_id, state, since) " +
  "SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), json_extract(value, '$[2]'), " +
  "json_extract(value, '$[3]') FROM json_each(?) WHERE true " +
  "ON CONFLICT (tenant_id, component_id) DO UPDATE SET state = excluded.state, since = excluded.since";

const STREAK_STARTS =
  "SELECT c.tenant_id, c.component_id, COALESCE(" +
  "(SELECT m.minute + 60 FROM minutes m WHERE m.tenant_id = c.tenant_id AND m.component_id = c.component_id " +
  "AND m.minute < ? AND m.state <> c.state ORDER BY m.minute DESC LIMIT 1), " +
  "(SELECT m.minute FROM minutes m WHERE m.tenant_id = c.tenant_id AND m.component_id = c.component_id " +
  "ORDER BY m.minute LIMIT 1)) AS start " +
  "FROM (SELECT json_extract(value, '$[0]') AS tenant_id, json_extract(value, '$[1]') AS component_id, " +
  "json_extract(value, '$[2]') AS state FROM json_each(?)) c";

interface StreakRow {
  tenant_id: string;
  component_id: string;
  start: number | null;
}

function key(tenantId: string, componentId: string): string {
  return `${tenantId}\u0000${componentId}`;
}

export function placed(change: Change, at: number, stored: number | null): Change {
  const fresh = change.from === "operational" || change.to === "operational";
  return { ...change, at, started: fresh ? at : (stored ?? at) };
}

async function streakStarts(db: Database, changes: readonly Change[], minute: number): Promise<Map<string, number>> {
  const { results } = await db
    .prepare(STREAK_STARTS)
    .bind(minute, JSON.stringify(changes.map((change) => [change.tenantId, change.componentId, change.to])))
    .all<StreakRow>();
  const starts = new Map<string, number>();
  for (const row of results) {
    if (row.start !== null) {
      starts.set(key(row.tenant_id, row.component_id), Math.min(row.start, minute - STEP_SECONDS));
    }
  }
  return starts;
}

export function minState(value: string): MinState {
  return (MIN_STATES as readonly string[]).includes(value) ? (value as MinState) : "outage";
}

export function noticeLevel(state: State, min: MinState): State | null {
  if (state === "operational") {
    return "operational";
  }
  if (state === "maintenance" || SEVERITY[state] < SEVERITY[min]) {
    return null;
  }
  return state;
}

export function changeOf(component: Watched, min: MinState, minute: number): Change | null {
  if (component.previous === null) {
    return null;
  }
  const level = noticeLevel(component.state, min);
  if (level === null || level !== noticeLevel(component.previous, min)) {
    return null;
  }
  const from = component.notified ?? "operational";
  if (level === from) {
    return null;
  }
  const change = {
    tenantId: component.tenantId,
    componentId: component.componentId,
    title: component.title,
    from,
    to: level,
    at: 0,
    started: 0,
  };
  return placed(change, minute - STEP_SECONDS, component.started);
}

function utcTime(seconds: number): string {
  return `${new Date(seconds * 1000).toISOString().slice(11, 16)} UTC`;
}

function line(messages: Messages, change: Change, started: number | null): string {
  if (change.to === "operational") {
    const minutes = Math.round((change.at - (started ?? change.at)) / 60);
    return translate(messages, "notice.recovered", { component: change.title, minutes });
  }
  return translate(messages, "notice.changed", {
    component: change.title,
    state: translate(messages, `state.${change.to}`),
    time: utcTime(change.at),
  });
}

export function noticeText(
  tenant: NotifyTenant,
  changes: readonly { change: Change; started: number | null }[],
): string {
  const messages = LOCALES[tenant.locale] ?? (LOCALES[DEFAULT_LOCALE] as Messages);
  const lines = changes.slice(0, MAX_NOTICE_LINES).map((item) => line(messages, item.change, item.started));
  const extra = changes.length - MAX_NOTICE_LINES;
  if (extra > 0) {
    lines.push(translate(messages, "notice.more", { count: extra }));
  }
  return [translate(messages, "page_title", { title: tenant.title }), ...lines, `https://${tenant.hostname}`].join(
    "\n",
  );
}

export async function sendTelegram(
  fetcher: Fetcher,
  token: string,
  chatId: string,
  text: string,
): Promise<number | null> {
  try {
    const response = await fetcher(`${TELEGRAM_API}/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json", "user-agent": USER_AGENT },
      body: JSON.stringify({ chat_id: chatId, text, link_preview_options: { is_disabled: true } }),
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    await response.body?.cancel();
    return response.status;
  } catch {
    return null;
  }
}

export async function notify(
  db: Database,
  fetcher: Fetcher,
  token: string,
  tenants: readonly NotifyTenant[],
  watched: readonly Watched[],
  minute: number,
): Promise<NoticeRun> {
  const pending = tenants.map((tenant) => {
    const min = minState(tenant.notify_min);
    const changes = watched
      .filter((component) => component.tenantId === tenant.id)
      .flatMap((component) => {
        const change = changeOf(component, min, minute);
        return change === null ? [] : [{ change, started: component.started }];
      });
    return { tenant, changes };
  });
  const all = pending.flatMap((item) => item.changes.map((entry) => entry.change));
  if (all.length === 0) {
    return { sent: 0, failed: 0 };
  }
  const starts = await streakStarts(db, all, minute);
  const outcomes = await Promise.all(
    pending.map(async ({ tenant, changes: found }) => {
      if (found.length === 0) {
        return null;
      }
      const changes = found.map(({ change, started }) => ({
        change: placed(change, starts.get(key(change.tenantId, change.componentId)) ?? change.at, started),
        started,
      }));
      const status = await sendTelegram(fetcher, token, tenant.notify_chat, noticeText(tenant, changes));
      const ok = status !== null && status >= 200 && status < 300;
      if (!ok) {
        console.warn(`status notice for ${tenant.id} not sent: ${status ?? "network error"}`);
      }
      return { ok, changes: changes.map((item) => item.change) };
    }),
  );
  const done = outcomes.flatMap((outcome) => (outcome?.ok ? outcome.changes : []));
  if (done.length > 0) {
    await db
      .prepare(UPSERT)
      .bind(JSON.stringify(done.map((change) => [change.tenantId, change.componentId, change.to, change.started])))
      .run();
  }
  return {
    sent: outcomes.filter((outcome) => outcome?.ok === true).length,
    failed: outcomes.filter((outcome) => outcome?.ok === false).length,
  };
}
