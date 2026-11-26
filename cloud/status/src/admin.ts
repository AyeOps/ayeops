import { ACCESS_HEADER, ACCESS_KEYS, type AccessKeys, accessConfig, verifyAccess } from "./access";
import type { Database } from "./db";
import { BodyError, isRecord, json, problem, readJson } from "./http";
import { componentIds, type Tenant } from "./tenant";

export { ACCESS_HEADER };
export const MAX_ADMIN_BYTES = 16384;
export const INCIDENT_STATES = ["investigating", "identified", "monitoring", "resolved"] as const;
export const IMPACTS = ["none", "degraded", "outage"] as const;

const MAX_TITLE = 200;
const MAX_BODY = 5000;

type Fields = Record<string, unknown>;

interface Checked {
  errors: string[];
  values: Fields;
}

function text(field: string, value: unknown, max: number, errors: string[]): string | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== "string" || value.length > max) {
    errors.push(`${field} must be a string of at most ${max} characters`);
    return undefined;
  }
  return value.trim();
}

function oneOf<T extends string>(
  field: string,
  value: unknown,
  allowed: readonly T[],
  errors: string[],
): T | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    errors.push(`${field} must be one of ${allowed.join(", ")}`);
    return undefined;
  }
  return value as T;
}

function moment(field: string, value: unknown, errors: string[]): number | undefined {
  if (value === undefined) {
    return undefined;
  }
  const parsed = typeof value === "string" ? Date.parse(value) : Number.NaN;
  if (Number.isNaN(parsed)) {
    errors.push(`${field} must be an ISO 8601 date-time`);
    return undefined;
  }
  return Math.floor(parsed / 1000);
}

function scope(value: unknown, known: Set<string>, errors: string[]): string | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    errors.push("components must be a list of component ids");
    return undefined;
  }
  const unknown = value.filter((id) => !known.has(id));
  if (unknown.length > 0) {
    errors.push(`unknown components: ${unknown.join(", ")}`);
  }
  return JSON.stringify([...new Set(value)]);
}

function unexpected(body: Fields, allowed: readonly string[]): string[] {
  return Object.keys(body)
    .filter((key) => !allowed.includes(key))
    .map((key) => `unexpected field ${key}`);
}

function checkIncident(body: Fields, known: Set<string>, creating: boolean): Checked {
  const errors = unexpected(body, ["title", "body", "state", "impact", "components"]);
  const values: Fields = {
    title: text("title", body.title, MAX_TITLE, errors),
    body: text("body", body.body, MAX_BODY, errors),
    state: oneOf("state", body.state, INCIDENT_STATES, errors),
    impact: oneOf("impact", body.impact, IMPACTS, errors),
    components: scope(body.components, known, errors),
  };
  if (creating) {
    if (!values.title) {
      errors.push("title is required");
    }
    if (values.impact === undefined && body.impact === undefined) {
      errors.push("impact is required");
    }
  } else if (values.title === "") {
    errors.push("title cannot be empty");
  }
  return { errors, values };
}

function checkMaintenance(body: Fields, known: Set<string>): Checked {
  const errors = unexpected(body, ["title", "body", "components", "starts_at", "ends_at"]);
  const values: Fields = {
    title: text("title", body.title, MAX_TITLE, errors),
    body: text("body", body.body, MAX_BODY, errors),
    components: scope(body.components, known, errors),
    starts_at: moment("starts_at", body.starts_at, errors),
    ends_at: moment("ends_at", body.ends_at, errors),
  };
  if (!values.title) {
    errors.push("title is required");
  }
  if (typeof values.starts_at !== "number" || typeof values.ends_at !== "number") {
    errors.push("starts_at and ends_at are required");
  } else if (values.ends_at <= values.starts_at) {
    errors.push("ends_at must be after starts_at");
  }
  return { errors, values };
}

async function createIncident(db: Database, tenant: Tenant, body: Fields, now: number) {
  const checked = checkIncident(body, await componentIds(db, tenant.id), true);
  if (checked.errors.length > 0) {
    return problem(400, "incident is not valid", checked.errors);
  }
  const values = checked.values;
  const state = values.state ?? "investigating";
  const row = await db
    .prepare(
      "INSERT INTO incidents (tenant_id, title, body, state, impact, components, started_at, " +
        "updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
    )
    .bind(
      tenant.id,
      values.title,
      values.body ?? "",
      state,
      values.impact,
      values.components ?? "[]",
      now,
      now,
      state === "resolved" ? now : null,
    )
    .first<{ id: number }>();
  return json(201, { id: row?.id ?? null });
}

async function updateIncident(db: Database, tenant: Tenant, id: number, body: Fields, now: number) {
  const checked = checkIncident(body, await componentIds(db, tenant.id), false);
  if (checked.errors.length > 0) {
    return problem(400, "incident update is not valid", checked.errors);
  }
  const values = checked.values;
  const sets = ["updated_at = ?"];
  const binds: unknown[] = [now];
  for (const field of ["title", "body", "state", "impact", "components"]) {
    if (values[field] !== undefined) {
      sets.push(`${field} = ?`);
      binds.push(values[field]);
    }
  }
  if (values.state !== undefined) {
    sets.push("resolved_at = ?");
    binds.push(values.state === "resolved" ? now : null);
  }
  const row = await db
    .prepare(`UPDATE incidents SET ${sets.join(", ")} WHERE id = ? AND tenant_id = ? RETURNING id`)
    .bind(...binds, id, tenant.id)
    .first<{ id: number }>();
  return row === null ? problem(404, "no such incident") : json(200, { id: row.id });
}

async function createMaintenance(db: Database, tenant: Tenant, body: Fields) {
  const checked = checkMaintenance(body, await componentIds(db, tenant.id));
  if (checked.errors.length > 0) {
    return problem(400, "maintenance is not valid", checked.errors);
  }
  const values = checked.values;
  const row = await db
    .prepare(
      "INSERT INTO maintenance (tenant_id, title, body, components, starts_at, ends_at) " +
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
    )
    .bind(
      tenant.id,
      values.title,
      values.body ?? "",
      values.components ?? "[]",
      values.starts_at,
      values.ends_at,
    )
    .first<{ id: number }>();
  return json(201, { id: row?.id ?? null });
}

async function deleteMaintenance(db: Database, tenant: Tenant, id: number) {
  const row = await db
    .prepare("DELETE FROM maintenance WHERE id = ? AND tenant_id = ? RETURNING id")
    .bind(id, tenant.id)
    .first<{ id: number }>();
  return row === null ? problem(404, "no such maintenance") : new Response(null, { status: 204 });
}

async function body(request: Request): Promise<Fields | Response> {
  try {
    const parsed = await readJson(request, MAX_ADMIN_BYTES);
    return isRecord(parsed) ? parsed : problem(400, "body must be an object");
  } catch (error) {
    if (error instanceof BodyError) {
      return problem(error.status, error.message);
    }
    throw error;
  }
}

export async function handleAdmin(
  request: Request,
  db: Database,
  tenant: Tenant,
  path: string,
  now: number,
  keys: AccessKeys = ACCESS_KEYS,
): Promise<Response> {
  const verdict = await verifyAccess(
    request.headers.get(ACCESS_HEADER),
    accessConfig(tenant.access_team, tenant.access_aud),
    now,
    keys,
  );
  if (!verdict.ok) {
    return problem(verdict.status, verdict.reason);
  }
  const match = /^\/api\/admin\/(incidents|maintenance)(?:\/([1-9][0-9]{0,15}))?$/.exec(path);
  if (match === null) {
    return problem(404, "not found");
  }
  const [, kind, rawId] = match;
  const id = rawId === undefined ? null : Number(rawId);
  const method = request.method;
  const allowed =
    id === null ? "POST" : kind === "incidents" ? "PATCH" : "DELETE";
  if (method !== allowed) {
    return new Response(null, { status: 405, headers: { allow: allowed } });
  }
  if (method === "DELETE" && id !== null) {
    return deleteMaintenance(db, tenant, id);
  }
  const fields = await body(request);
  if (fields instanceof Response) {
    return fields;
  }
  if (kind === "incidents") {
    return id === null
      ? createIncident(db, tenant, fields, now)
      : updateIncident(db, tenant, id, fields, now);
  }
  return createMaintenance(db, tenant, fields);
}
