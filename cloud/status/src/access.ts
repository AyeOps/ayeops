import { isRecord } from "./http";

export const ACCESS_HEADER = "Cf-Access-Jwt-Assertion";
export const CLOCK_SKEW_SECONDS = 60;
export const KEYS_TTL_SECONDS = 3600;
export const REFETCH_SECONDS = 30;
export const CERTS_TIMEOUT_MS = 5000;
export const ACCESS_TEAM = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
export const ACCESS_AUD = /^[0-9a-f]{64}$/;

const SEGMENT = /^[A-Za-z0-9_-]+$/;
const MAX_TOKEN = 16384;
const MAX_KID = 256;
const RSA = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" } as const;

export interface AccessConfig {
  team: string;
  aud: string;
}

export type CertsFetcher = (url: string, init: RequestInit) => Promise<Response>;

export type Verdict =
  | { ok: true }
  | { ok: false; status: 403 | 503; reason: string };

export class CertsUnavailable extends Error {}

interface KeySet {
  keys: Map<string, CryptoKey>;
  fetchedAt: number;
}

export function teamOrigin(team: string): string {
  return `https://${team}.cloudflareaccess.com`;
}

export function certsUrl(team: string): string {
  return `${teamOrigin(team)}/cdn-cgi/access/certs`;
}

export function accessConfig(team: string | null, aud: string | null): AccessConfig | null {
  if (team === null || aud === null || !ACCESS_TEAM.test(team) || !ACCESS_AUD.test(aud)) {
    return null;
  }
  return { team, aud };
}

async function importKeys(document: unknown): Promise<Map<string, CryptoKey>> {
  const keys = new Map<string, CryptoKey>();
  if (!isRecord(document) || !Array.isArray(document.keys)) {
    throw new CertsUnavailable("the Access certs hold no keys");
  }
  for (const jwk of document.keys as unknown[]) {
    if (
      !isRecord(jwk) ||
      jwk.kty !== "RSA" ||
      typeof jwk.kid !== "string" ||
      typeof jwk.n !== "string" ||
      typeof jwk.e !== "string" ||
      !SEGMENT.test(jwk.n) ||
      !SEGMENT.test(jwk.e) ||
      (jwk.alg !== undefined && jwk.alg !== "RS256") ||
      (jwk.use !== undefined && jwk.use !== "sig")
    ) {
      continue;
    }
    try {
      const key = await crypto.subtle.importKey(
        "jwk",
        { kty: "RSA", n: jwk.n, e: jwk.e },
        RSA,
        false,
        ["verify"],
      );
      keys.set(jwk.kid, key);
    } catch {
      continue;
    }
  }
  return keys;
}

export class AccessKeys {
  private readonly sets = new Map<string, KeySet>();
  private readonly pending = new Map<string, Promise<KeySet>>();

  constructor(private readonly fetcher: CertsFetcher) {}

  async key(team: string, kid: string, now: number): Promise<CryptoKey | null> {
    let set = this.sets.get(team);
    if (set === undefined || now - set.fetchedAt >= KEYS_TTL_SECONDS || now < set.fetchedAt) {
      set = await this.refresh(team, now);
    }
    const found = set.keys.get(kid);
    if (found !== undefined) {
      return found;
    }
    if (now - set.fetchedAt < REFETCH_SECONDS) {
      return null;
    }
    set = await this.refresh(team, now);
    return set.keys.get(kid) ?? null;
  }

  private refresh(team: string, now: number): Promise<KeySet> {
    const running = this.pending.get(team);
    if (running !== undefined) {
      return running;
    }
    const loading = this.load(team, now).finally(() => this.pending.delete(team));
    this.pending.set(team, loading);
    return loading;
  }

  private async load(team: string, now: number): Promise<KeySet> {
    let document: unknown;
    try {
      const response = await this.fetcher(certsUrl(team), {
        headers: { accept: "application/json" },
        signal: AbortSignal.timeout(CERTS_TIMEOUT_MS),
      });
      if (!response.ok) {
        throw new CertsUnavailable(`the Access certs answered ${response.status}`);
      }
      document = await response.json();
    } catch (error) {
      if (error instanceof CertsUnavailable) {
        throw error;
      }
      throw new CertsUnavailable("the Access certs cannot be fetched");
    }
    const set = { keys: await importKeys(document), fetchedAt: now };
    this.sets.set(team, set);
    return set;
  }
}

function base64urlBytes(segment: string): Uint8Array<ArrayBuffer> {
  const padded = segment.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function segmentBytes(segment: string): Uint8Array<ArrayBuffer> | null {
  try {
    return base64urlBytes(segment);
  } catch {
    return null;
  }
}

function segmentJson(segment: string): Record<string, unknown> | null {
  const bytes = segmentBytes(segment);
  if (bytes === null) {
    return null;
  }
  try {
    const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false });
    const value: unknown = JSON.parse(decoder.decode(bytes));
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}

function denied(reason: string): Verdict {
  return { ok: false, status: 403, reason };
}

function audiences(value: unknown): string[] {
  if (typeof value === "string") {
    return [value];
  }
  return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
}

function optionalTime(value: unknown): number | null | undefined {
  if (value === undefined) {
    return undefined;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function claimsProblem(claims: Record<string, unknown>, config: AccessConfig, now: number) {
  if (claims.iss !== teamOrigin(config.team)) {
    return "the token was issued by another Access team";
  }
  if (!audiences(claims.aud).includes(config.aud)) {
    return "the token is for another Access application";
  }
  const exp = optionalTime(claims.exp);
  const nbf = optionalTime(claims.nbf);
  const iat = optionalTime(claims.iat);
  if (exp === undefined || exp === null || nbf === null || iat === null) {
    return "the token has malformed times";
  }
  if (now >= exp + CLOCK_SKEW_SECONDS) {
    return "the token has expired";
  }
  if (nbf !== undefined && now + CLOCK_SKEW_SECONDS < nbf) {
    return "the token is not valid yet";
  }
  if (iat !== undefined && now + CLOCK_SKEW_SECONDS < iat) {
    return "the token was issued in the future";
  }
  return null;
}

export async function verifyAccess(
  token: string | null,
  config: AccessConfig | null,
  now: number,
  keys: AccessKeys,
): Promise<Verdict> {
  if (config === null) {
    return denied("admin is closed: the tenant has no Cloudflare Access application configured");
  }
  if (!token) {
    return denied("admin requests come through Cloudflare Access");
  }
  const parts = token.split(".");
  const wellFormed = parts.length === 3 && parts.every((part) => SEGMENT.test(part));
  if (token.length > MAX_TOKEN || !wellFormed) {
    return denied("the Access token is malformed");
  }
  const [rawHeader, rawClaims, rawSignature] = parts as [string, string, string];
  const header = segmentJson(rawHeader);
  const claims = segmentJson(rawClaims);
  const signature = segmentBytes(rawSignature);
  if (header === null || claims === null || signature === null) {
    return denied("the Access token is malformed");
  }
  if (header.alg !== "RS256") {
    return denied("the Access token must be signed with RS256");
  }
  if (header.crit !== undefined) {
    return denied("the Access token asks for extensions this Worker does not know");
  }
  const kid = header.kid;
  if (typeof kid !== "string" || kid.length === 0 || kid.length > MAX_KID) {
    return denied("the Access token names no key");
  }
  let key: CryptoKey | null;
  try {
    key = await keys.key(config.team, kid, now);
  } catch (error) {
    if (error instanceof CertsUnavailable) {
      return { ok: false, status: 503, reason: error.message };
    }
    throw error;
  }
  if (key === null) {
    return denied("the Access token is signed with an unknown key");
  }
  const signed = new TextEncoder().encode(`${rawHeader}.${rawClaims}`);
  const valid = await crypto.subtle.verify(RSA, key, signature, signed);
  if (!valid) {
    return denied("the Access token signature is not valid");
  }
  const reason = claimsProblem(claims, config, now);
  return reason === null ? { ok: true } : denied(reason);
}

export const ACCESS_KEYS = new AccessKeys((url, init) => fetch(url, init));
