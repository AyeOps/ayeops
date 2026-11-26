import type { Database } from "./db";
import { BodyError, isRecord, json, parseJson, problem, readBody } from "./http";
import { isState, type State } from "./states";
import { componentIds, tenantById, type Tenant } from "./tenant";

export const SIGNATURE_HEADER = "X-AyeOps-Signature";
export const MAX_SKEW_SECONDS = 300;
export const MAX_PUSH_BYTES = 65536;
export const MAX_COMPONENTS = 100;

const NAME = /^[a-z][a-z0-9-]{0,39}$/;
const SIGNATURE = /^sha256=([0-9a-f]{64})$/;

export interface Push {
  tenant: string;
  ts: number;
  components: { id: string; state: State }[];
}

export type Validation = { ok: true; push: Push } | { ok: false; errors: string[] };

function unknownKeys(value: Record<string, unknown>, allowed: readonly string[]): string[] {
  return Object.keys(value).filter((key) => !allowed.includes(key));
}

export function validatePush(value: unknown): Validation {
  if (!isRecord(value)) {
    return { ok: false, errors: ["body must be an object"] };
  }
  const errors = unknownKeys(value, ["tenant", "ts", "components"]).map(
    (key) => `unexpected field ${key}`,
  );
  if (typeof value.tenant !== "string" || !NAME.test(value.tenant)) {
    errors.push("tenant must be a lower-case name");
  }
  if (typeof value.ts !== "number" || !Number.isInteger(value.ts) || value.ts < 0) {
    errors.push("ts must be unix seconds");
  }
  const components = value.components;
  if (!Array.isArray(components) || components.length < 1 || components.length > MAX_COMPONENTS) {
    errors.push(`components must hold 1 to ${MAX_COMPONENTS} entries`);
  } else {
    components.forEach((entry: unknown, index) => {
      if (!isRecord(entry)) {
        errors.push(`components.${index} must be an object`);
        return;
      }
      errors.push(
        ...unknownKeys(entry, ["id", "state"]).map((key) => `components.${index}: unexpected ${key}`),
      );
      if (typeof entry.id !== "string" || !NAME.test(entry.id)) {
        errors.push(`components.${index}.id must be a component id`);
      }
      if (!isState(entry.state)) {
        errors.push(`components.${index}.state is not a known state`);
      }
    });
  }
  return errors.length > 0 ? { ok: false, errors } : { ok: true, push: value as unknown as Push };
}

function hexBytes(hex: string): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(hex.length / 2);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(hex.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}

export async function verifySignature(
  body: Uint8Array<ArrayBuffer>,
  header: string | null,
  secret: string,
): Promise<boolean> {
  const match = SIGNATURE.exec((header ?? "").trim().toLowerCase());
  if (match === null || match[1] === undefined) {
    return false;
  }
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  return crypto.subtle.verify("HMAC", key, hexBytes(match[1]), body);
}

export async function sign(body: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body)),
  );
  return `sha256=${[...mac].map((byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

export async function handlePush(
  request: Request,
  db: Database,
  now: number,
  hostTenant: Tenant | null,
): Promise<Response> {
  let body: Uint8Array<ArrayBuffer>;
  let parsed: unknown;
  try {
    body = await readBody(request, MAX_PUSH_BYTES);
    parsed = parseJson(body);
  } catch (error) {
    if (error instanceof BodyError) {
      return problem(error.status, error.message);
    }
    throw error;
  }
  const validation = validatePush(parsed);
  if (!validation.ok) {
    return problem(400, "push does not match the contract", validation.errors);
  }
  const push = validation.push;
  const tenant = await tenantById(db, push.tenant);
  if (tenant === null || !tenant.secret) {
    return problem(401, "signature does not verify");
  }
  if (!(await verifySignature(body, request.headers.get(SIGNATURE_HEADER), tenant.secret))) {
    return problem(401, "signature does not verify");
  }
  if (hostTenant !== null && hostTenant.id !== push.tenant) {
    return problem(400, "tenant does not match the hostname");
  }
  if (Math.abs(now - push.ts) > MAX_SKEW_SECONDS) {
    return problem(401, `ts is more than ${MAX_SKEW_SECONDS} s away from the server clock`);
  }
  const ids = push.components.map((component) => component.id);
  const repeated = ids.filter((id, index) => ids.indexOf(id) !== index);
  if (repeated.length > 0) {
    return problem(400, "a component appears twice", [...new Set(repeated)]);
  }
  const known = await componentIds(db, tenant.id);
  const unknown = ids.filter((id) => !known.has(id));
  if (unknown.length > 0) {
    return problem(422, "unknown components", unknown);
  }
  await db.batch(
    push.components.map((component) =>
      db
        .prepare(
          "INSERT INTO push_latest (tenant_id, component_id, ts, state) VALUES (?, ?, ?, ?) " +
            "ON CONFLICT (tenant_id, component_id) DO UPDATE SET ts = excluded.ts, " +
            "state = excluded.state WHERE excluded.ts >= push_latest.ts",
        )
        .bind(tenant.id, component.id, push.ts, component.state),
    ),
  );
  return json(202, { accepted: push.components.length });
}
