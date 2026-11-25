import { describe, expect, it } from "vitest";
import {
  ACCESS_HEADER,
  AccessKeys,
  CLOCK_SKEW_SECONDS,
  KEYS_TTL_SECONDS,
  REFETCH_SECONDS,
  accessConfig,
  certsUrl,
  verifyAccess,
} from "../src/access";
import { handle } from "../src/app";
import {
  ACCESS,
  FakeCerts,
  NOW,
  accessClaims,
  acmeDatabase,
  addGlobex,
  base64url,
  request,
  rsaKey,
  signJwt,
} from "./helpers";

const KEY = await rsaKey("acme-2026-10");
const NEXT = await rsaKey("acme-2026-11");
const STRANGER = await rsaKey("acme-2026-10");
const CONFIG = { team: ACCESS.team, aud: ACCESS.aud };
const INCIDENT = { title: "Checkout fails", impact: "outage" };

function setup(keys: typeof KEY[] = [KEY]) {
  const certs = new FakeCerts(keys);
  return { certs, keys: new AccessKeys(certs.fetcher), db: acmeDatabase() };
}

function post(token: string | null, host?: string): Request {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (token !== null) {
    headers[ACCESS_HEADER] = token;
  }
  return request(
    "/api/admin/incidents",
    { method: "POST", headers, body: JSON.stringify(INCIDENT) },
    host,
  );
}

async function refusal(response: Response): Promise<string> {
  expect(response.status).toBe(403);
  return ((await response.json()) as { error: string }).error;
}

function rawToken(header: Record<string, unknown>, claims: Record<string, unknown>, signature: string) {
  return `${base64url(JSON.stringify(header))}.${base64url(JSON.stringify(claims))}${signature}`;
}

describe("admin access through Cloudflare Access", () => {
  it("accepts a token signed by a key of the team, for the application, in time", async () => {
    const { certs, keys, db } = setup();
    const response = await handle(post(await signJwt(KEY, accessClaims(NOW))), db, NOW, keys);
    expect(response.status).toBe(201);
    expect(db.rows("SELECT title FROM incidents")).toEqual([{ title: "Checkout fails" }]);
    expect(certs.urls).toEqual(["https://acme.cloudflareaccess.com/cdn-cgi/access/certs"]);
  });

  it("accepts an aud given as one string", async () => {
    const { keys, db } = setup();
    const token = await signJwt(KEY, accessClaims(NOW, { aud: ACCESS.aud }));
    expect((await handle(post(token), db, NOW, keys)).status).toBe(201);
  });

  it("refuses a request without the header", async () => {
    const { certs, keys, db } = setup();
    expect(await refusal(await handle(post(null), db, NOW, keys))).toContain("Cloudflare Access");
    expect(certs.urls).toEqual([]);
    expect(db.rows("SELECT id FROM incidents")).toEqual([]);
  });

  it.each([
    ["three random parts", "eyJ.test.jwt"],
    ["two parts", "eyJhbGciOiJSUzI1NiJ9.e30"],
    ["four parts", "a.b.c.d"],
    ["over 16 KiB", `${"a".repeat(16384)}.e30.c2ln`],
    ["characters outside base64url", "eyJ+.e30=.c2ln"],
    ["a header that is not an object", `${base64url("[1]")}.${base64url("{}")}.c2ln`],
    ["a payload that is not JSON", `${base64url('{"alg":"RS256","kid":"acme-2026-10"}')}.bm9wZQ.c2ln`],
    ["a signature of impossible length", `${base64url('{"alg":"RS256","kid":"k"}')}.${base64url("{}")}.A`],
  ])("refuses a malformed token: %s", async (_, token) => {
    const { certs, keys, db } = setup();
    expect(await refusal(await handle(post(token), db, NOW, keys))).toBe(
      "the Access token is malformed",
    );
    expect(certs.urls).toEqual([]);
  });

  it("refuses a token signed with a key that is not in the team's JWKS, even with its kid", async () => {
    const { keys, db } = setup();
    const forged = await signJwt(STRANGER, accessClaims(NOW));
    expect(await refusal(await handle(post(forged), db, NOW, keys))).toBe(
      "the Access token signature is not valid",
    );
    expect(db.rows("SELECT id FROM incidents")).toEqual([]);
  });

  it("refuses a valid signature over altered claims", async () => {
    const { keys, db } = setup();
    const [header, , signature] = (await signJwt(KEY, accessClaims(NOW))).split(".");
    const altered = base64url(JSON.stringify(accessClaims(NOW, { email: "intruder@evil.example" })));
    expect(await refusal(await handle(post(`${header}.${altered}.${signature}`), db, NOW, keys))).toBe(
      "the Access token signature is not valid",
    );
  });

  it("refuses a token for another Access application", async () => {
    const { keys, db } = setup();
    const token = await signJwt(KEY, accessClaims(NOW, { aud: ["0".repeat(64)] }));
    expect(await refusal(await handle(post(token), db, NOW, keys))).toBe(
      "the token is for another Access application",
    );
  });

  it("refuses a token without an aud", async () => {
    const { keys, db } = setup();
    const token = await signJwt(KEY, accessClaims(NOW, { aud: undefined }));
    expect(await refusal(await handle(post(token), db, NOW, keys))).toBe(
      "the token is for another Access application",
    );
  });

  it.each([
    ["another team", "https://evil.cloudflareaccess.com"],
    ["http instead of https", "http://acme.cloudflareaccess.com"],
    ["a trailing slash", "https://acme.cloudflareaccess.com/"],
  ])("refuses a token issued by %s", async (_, iss) => {
    const { keys, db } = setup();
    const token = await signJwt(KEY, accessClaims(NOW, { iss }));
    expect(await refusal(await handle(post(token), db, NOW, keys))).toBe(
      "the token was issued by another Access team",
    );
  });

  it("refuses an expired token and tolerates a small clock skew", async () => {
    const { keys, db } = setup();
    const expired = await signJwt(KEY, accessClaims(NOW, { exp: NOW - CLOCK_SKEW_SECONDS }));
    expect(await refusal(await handle(post(expired), db, NOW, keys))).toBe("the token has expired");
    const skewed = await signJwt(KEY, accessClaims(NOW, { exp: NOW - CLOCK_SKEW_SECONDS + 1 }));
    expect((await handle(post(skewed), db, NOW, keys)).status).toBe(201);
  });

  it("refuses a token without an exp or with times that are not numbers", async () => {
    const { keys, db } = setup();
    for (const extra of [{ exp: undefined }, { exp: String(NOW + 60) }, { nbf: "soon" }, { iat: null }]) {
      const token = await signJwt(KEY, accessClaims(NOW, extra));
      expect(await refusal(await handle(post(token), db, NOW, keys))).toBe(
        "the token has malformed times",
      );
    }
  });

  it("refuses a token that is not valid yet and tolerates a small clock skew", async () => {
    const { keys, db } = setup();
    const early = await signJwt(KEY, accessClaims(NOW, { nbf: NOW + CLOCK_SKEW_SECONDS + 1 }));
    expect(await refusal(await handle(post(early), db, NOW, keys))).toBe("the token is not valid yet");
    const skewed = await signJwt(KEY, accessClaims(NOW, { nbf: NOW + CLOCK_SKEW_SECONDS }));
    expect((await handle(post(skewed), db, NOW, keys)).status).toBe(201);
  });

  it("refuses a token issued in the future", async () => {
    const { keys, db } = setup();
    const token = await signJwt(KEY, accessClaims(NOW, { iat: NOW + 600 }));
    expect(await refusal(await handle(post(token), db, NOW, keys))).toBe(
      "the token was issued in the future",
    );
  });

  it.each([
    ["none with an empty signature", { alg: "none", kid: "acme-2026-10" }, "."],
    ["none with a signature", { alg: "none", kid: "acme-2026-10" }, ".c2lnbmF0dXJl"],
    ["NONE", { alg: "NONE", kid: "acme-2026-10" }, ".c2lnbmF0dXJl"],
    ["HS256", { alg: "HS256", kid: "acme-2026-10" }, ".c2lnbmF0dXJl"],
    ["RS512", { alg: "RS512", kid: "acme-2026-10" }, ".c2lnbmF0dXJl"],
    ["no alg", { kid: "acme-2026-10" }, ".c2lnbmF0dXJl"],
  ])("refuses alg %s", async (_, header, signature) => {
    const { certs, keys, db } = setup();
    const token = rawToken(header, accessClaims(NOW), signature);
    const error = await refusal(await handle(post(token), db, NOW, keys));
    expect(["the Access token is malformed", "the Access token must be signed with RS256"]).toContain(
      error,
    );
    expect(certs.urls).toEqual([]);
  });

  it("refuses a token without a kid or with crit extensions", async () => {
    const { keys, db } = setup();
    const noKid = await signJwt(KEY, accessClaims(NOW), { alg: "RS256" });
    expect(await refusal(await handle(post(noKid), db, NOW, keys))).toBe(
      "the Access token names no key",
    );
    const crit = await signJwt(KEY, accessClaims(NOW), { alg: "RS256", kid: KEY.kid, crit: ["exp"] });
    expect(await refusal(await handle(post(crit), db, NOW, keys))).toContain("extensions");
  });

  it("refetches the keys once for an unknown kid, so a rotated key is accepted", async () => {
    const { certs, keys, db } = setup([KEY]);
    expect((await handle(post(await signJwt(KEY, accessClaims(NOW))), db, NOW, keys)).status).toBe(
      201,
    );
    certs.keys = [NEXT, KEY];
    const later = NOW + REFETCH_SECONDS;
    const rotated = await signJwt(NEXT, accessClaims(later));
    expect((await handle(post(rotated), db, later, keys)).status).toBe(201);
    expect(certs.urls).toHaveLength(2);
    const unknown = await signJwt(await rsaKey("acme-2027-01"), accessClaims(later + 1));
    expect(await refusal(await handle(post(unknown), db, later + 1, keys))).toBe(
      "the Access token is signed with an unknown key",
    );
    expect(certs.urls).toHaveLength(2);
  });

  it("refetches an unknown kid at most once per request", async () => {
    const { certs, keys, db } = setup([KEY]);
    await handle(post(await signJwt(KEY, accessClaims(NOW))), db, NOW, keys);
    const later = NOW + REFETCH_SECONDS + 5;
    const unknown = await signJwt(await rsaKey("acme-2027-01"), accessClaims(later));
    expect(await refusal(await handle(post(unknown), db, later, keys))).toBe(
      "the Access token is signed with an unknown key",
    );
    expect(certs.urls).toHaveLength(2);
  });

  it("caches the keys and fetches them again after the TTL", async () => {
    const { certs, keys, db } = setup();
    const token = await signJwt(KEY, accessClaims(NOW, { exp: NOW + 2 * KEYS_TTL_SECONDS }));
    await handle(post(token), db, NOW, keys);
    await handle(post(token), db, NOW + 1, keys);
    expect(certs.urls).toHaveLength(1);
    await handle(post(token), db, NOW + KEYS_TTL_SECONDS, keys);
    expect(certs.urls).toHaveLength(2);
  });

  it("shares one fetch between concurrent requests", async () => {
    const { certs, keys, db } = setup();
    const token = await signJwt(KEY, accessClaims(NOW));
    const answers = await Promise.all([1, 2, 3].map(() => handle(post(token), db, NOW, keys)));
    expect(answers.map((answer) => answer.status)).toEqual([201, 201, 201]);
    expect(certs.urls).toHaveLength(1);
  });

  it.each([
    ["a network error", 0],
    ["a 500", 500],
  ])("answers 503 and writes nothing when the certs give %s", async (_, status) => {
    const { certs, keys, db } = setup();
    certs.status = status;
    const response = await handle(post(await signJwt(KEY, accessClaims(NOW))), db, NOW, keys);
    expect(response.status).toBe(503);
    expect(db.rows("SELECT id FROM incidents")).toEqual([]);
    certs.status = 200;
    expect((await handle(post(await signJwt(KEY, accessClaims(NOW))), db, NOW, keys)).status).toBe(
      201,
    );
  });

  it("ignores keys of the JWKS that are not RS256 signing keys", async () => {
    const certs = new FakeCerts([]);
    const keys = new AccessKeys(async (url) => {
      certs.urls.push(url);
      return Response.json({
        keys: [
          { ...KEY.jwk, alg: "RS512" },
          { ...KEY.jwk, use: "enc" },
          { ...KEY.jwk, kty: "EC" },
          { kid: KEY.kid, kty: "RSA", n: 7, e: "AQAB" },
          { kid: KEY.kid, kty: "RSA", n: "!!", e: "AQAB" },
        ],
      });
    });
    const token = await signJwt(KEY, accessClaims(NOW));
    expect(await refusal(await handle(post(token), acmeDatabase(), NOW, keys))).toBe(
      "the Access token is signed with an unknown key",
    );
  });

  it("refuses everything for a tenant without Access settings, without fetching keys", async () => {
    const { certs, keys, db } = setup();
    db.exec("UPDATE tenants SET access_aud = NULL WHERE id = 'acme'");
    const token = await signJwt(KEY, accessClaims(NOW));
    expect(await refusal(await handle(post(token), db, NOW, keys))).toContain("admin is closed");
    expect(certs.urls).toEqual([]);
    expect(db.rows("SELECT id FROM incidents")).toEqual([]);
  });

  it("refuses a token of one tenant's application on another tenant's hostname", async () => {
    const { keys, db } = setup();
    addGlobex(db);
    db.exec(
      "UPDATE tenants SET access_team = ?, access_aud = ? WHERE id = 'globex'",
      ACCESS.team,
      "1".repeat(64),
    );
    const token = await signJwt(KEY, accessClaims(NOW));
    const response = await handle(post(token, "https://status.globex.example"), db, NOW, keys);
    expect(await refusal(response)).toBe("the token is for another Access application");
  });

  it("checks the access before it routes, so unknown admin paths say nothing", async () => {
    const { keys, db } = setup();
    const response = await handle(request("/api/admin/secrets", { method: "GET" }), db, NOW, keys);
    expect(response.status).toBe(403);
  });
});

describe("access settings", () => {
  it("need both a team and a 64 hex AUD tag", () => {
    expect(accessConfig("acme", ACCESS.aud)).toEqual(CONFIG);
    expect(accessConfig(null, ACCESS.aud)).toBeNull();
    expect(accessConfig("acme", null)).toBeNull();
    expect(accessConfig("acme.cloudflareaccess.com", ACCESS.aud)).toBeNull();
    expect(accessConfig("acme", ACCESS.aud.toUpperCase())).toBeNull();
    expect(accessConfig("acme", ACCESS.aud.slice(1))).toBeNull();
  });

  it("point at the team's certs endpoint", () => {
    expect(certsUrl("acme")).toBe("https://acme.cloudflareaccess.com/cdn-cgi/access/certs");
  });

  it("are checked before the token", async () => {
    const certs = new FakeCerts([KEY]);
    const verdict = await verifyAccess(null, null, NOW, new AccessKeys(certs.fetcher));
    expect(verdict).toEqual({
      ok: false,
      status: 403,
      reason: "admin is closed: the tenant has no Cloudflare Access application configured",
    });
  });
});
