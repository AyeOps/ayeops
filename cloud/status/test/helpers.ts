import type { webcrypto } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import type { Database, Statement } from "../src/db";
import type { Fetcher } from "../src/probe";

const ROOT = join(import.meta.dirname, "..");
export const CONTRACT = join(ROOT, "contract");
export const FIXTURES = join(CONTRACT, "fixtures");

export interface PushCase {
  name: string;
  body: string;
  signature: string;
  status?: number;
  signed_with?: string;
}

export interface PushFixtures {
  secret: string;
  now: number;
  header: string;
  valid: PushCase[];
  invalid: PushCase[];
  rejected: PushCase[];
}

export interface ComponentsDocument {
  tenant: string;
  hostname: string;
  title: string;
  locale: string;
  notify: { telegram_chat_id: string; min_state: string } | null;
  access: { team: string; aud: string } | null;
  components: { id: string; title: string; probes: string[]; push: boolean }[];
}

export function fixture<T>(name: string): T {
  return JSON.parse(readFileSync(join(FIXTURES, name), "utf-8")) as T;
}

export const PUSH = fixture<PushFixtures>("push.json");
export const COMPONENTS = fixture<ComponentsDocument>("acme.components.json");
export const NOW = PUSH.now;
export const HOST = `https://${COMPONENTS.hostname}`;

class SqliteStatement implements Statement {
  constructor(
    private readonly raw: DatabaseSync,
    private readonly sql: string,
    private readonly values: SQLInputValue[] = [],
  ) {}

  bind(...values: unknown[]): Statement {
    return new SqliteStatement(this.raw, this.sql, values as SQLInputValue[]);
  }

  async all<T>(): Promise<{ results: T[] }> {
    return { results: this.rows<T>() };
  }

  async first<T>(): Promise<T | null> {
    return this.rows<T>()[0] ?? null;
  }

  async run(): Promise<unknown> {
    this.execute();
    return { success: true };
  }

  execute(): void {
    this.raw.prepare(this.sql).all(...this.values);
  }

  private rows<T>(): T[] {
    return this.raw
      .prepare(this.sql)
      .all(...this.values)
      .map((row) => ({ ...row }) as T);
  }
}

export class SqliteD1 implements Database {
  constructor(readonly raw: DatabaseSync) {}

  prepare(query: string): Statement {
    return new SqliteStatement(this.raw, query);
  }

  async batch(statements: Statement[]): Promise<unknown[]> {
    this.raw.exec("BEGIN");
    try {
      for (const statement of statements) {
        (statement as SqliteStatement).execute();
      }
      this.raw.exec("COMMIT");
    } catch (error) {
      this.raw.exec("ROLLBACK");
      throw error;
    }
    return statements.map(() => ({ success: true }));
  }

  rows<T>(sql: string, ...values: SQLInputValue[]): T[] {
    return this.raw
      .prepare(sql)
      .all(...values)
      .map((row) => ({ ...row }) as T);
  }

  exec(sql: string, ...values: SQLInputValue[]): void {
    this.raw.prepare(sql).run(...values);
  }
}

export function migratedDatabase(): SqliteD1 {
  const raw = new DatabaseSync(":memory:");
  const migrations = join(ROOT, "migrations");
  for (const file of readdirSync(migrations).filter((name) => name.endsWith(".sql")).sort()) {
    raw.exec(readFileSync(join(migrations, file), "utf-8"));
  }
  return new SqliteD1(raw);
}

export function acmeDatabase(): SqliteD1 {
  const db = migratedDatabase();
  db.raw.exec(readFileSync(join(FIXTURES, "acme.seed.sql"), "utf-8"));
  db.exec("UPDATE tenants SET secret = ? WHERE id = ?", PUSH.secret, COMPONENTS.tenant);
  return db;
}

export function addGlobex(db: SqliteD1): void {
  db.raw.exec(
    "INSERT INTO tenants (id, hostname, title, locale, secret) VALUES " +
      "('globex', 'status.globex.example', 'Globex', 'en', 'globex-secret');" +
      "INSERT INTO components (tenant_id, id, title, position, push) VALUES " +
      "('globex', 'shop', 'Shop', 0, 0);" +
      "INSERT INTO probes (tenant_id, component_id, url) VALUES " +
      "('globex', 'shop', 'https://shop.globex.example');",
  );
}

export interface FakeSite {
  status: number[];
  latencyMs?: number;
}

export class FakeNet {
  time = 0;
  calls: string[] = [];
  bodies: { url: string; body: string | null }[] = [];

  constructor(private readonly sites: Record<string, FakeSite> = {}) {}

  clock = (): number => this.time;

  fetcher: Fetcher = async (url, init) => {
    this.calls.push(url);
    this.bodies.push({ url, body: typeof init.body === "string" ? init.body : null });
    const started = this.time;
    const site = this.sites[url] ?? { status: [200] };
    const status = site.status.length > 1 ? site.status.shift() : site.status[0];
    await new Promise((resolve) => setTimeout(resolve, 0));
    this.time = started + (site.latencyMs ?? 120);
    if (status === undefined || status === 0) {
      throw new TypeError("network error");
    }
    return new Response("ok", { status });
  };
}

export function request(path: string, init: RequestInit = {}, host = HOST): Request {
  return new Request(`${host}${path}`, init);
}

export function pushRequest(body: string, signature: string, host = HOST): Request {
  return request(
    "/api/push",
    { method: "POST", body, headers: { [PUSH.header]: signature, "content-type": "application/json" } },
    host,
  );
}

export const ACCESS = {
  team: COMPONENTS.access?.team ?? "",
  aud: COMPONENTS.access?.aud ?? "",
  issuer: `https://${COMPONENTS.access?.team ?? ""}.cloudflareaccess.com`,
};

export interface TestKey {
  kid: string;
  privateKey: CryptoKey;
  jwk: webcrypto.JsonWebKey & { kid: string };
}

export async function rsaKey(kid: string): Promise<TestKey> {
  const pair = await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );
  const jwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
  return { kid, privateKey: pair.privateKey, jwk: { ...jwk, kid, alg: "RS256", use: "sig" } };
}

export function base64url(data: string | Uint8Array): string {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function accessClaims(now: number, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    aud: [ACCESS.aud],
    email: "ops@acme.example",
    exp: now + 3600,
    iat: now - 5,
    nbf: now - 5,
    iss: ACCESS.issuer,
    type: "app",
    sub: "6f1d2c3b-0000-4000-8000-000000000001",
    ...extra,
  };
}

export async function signJwt(
  key: TestKey,
  claims: Record<string, unknown>,
  header: Record<string, unknown> = { alg: "RS256", kid: key.kid, typ: "JWT" },
): Promise<string> {
  const signed = `${base64url(JSON.stringify(header))}.${base64url(JSON.stringify(claims))}`;
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key.privateKey,
    new TextEncoder().encode(signed),
  );
  return `${signed}.${base64url(new Uint8Array(signature))}`;
}

export class FakeCerts {
  urls: string[] = [];
  status = 200;

  constructor(public keys: TestKey[]) {}

  fetcher = async (url: string): Promise<Response> => {
    this.urls.push(url);
    if (this.status === 0) {
      throw new TypeError("network error");
    }
    return Response.json(
      { keys: this.keys.map((key) => key.jwk), public_cert: {}, public_certs: [] },
      { status: this.status },
    );
  };
}
