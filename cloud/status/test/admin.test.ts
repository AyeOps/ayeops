import { describe, expect, it } from "vitest";
import { AccessKeys } from "../src/access";
import { ACCESS_HEADER } from "../src/admin";
import { handle } from "../src/app";
import { runMinute } from "../src/cron";
import {
  ACCESS,
  FakeCerts,
  FakeNet,
  NOW,
  accessClaims,
  acmeDatabase,
  addGlobex,
  request,
  rsaKey,
  signJwt,
} from "./helpers";

type Db = ReturnType<typeof acmeDatabase>;

const KEY = await rsaKey("acme-2026-10");
const KEYS = new AccessKeys(new FakeCerts([KEY]).fetcher);
const TOKEN = await signJwt(KEY, accessClaims(NOW));

function admin(db: Db, path: string, method: string, body?: unknown, host?: string) {
  return handle(
    request(
      path,
      {
        method,
        headers: { [ACCESS_HEADER]: TOKEN, "content-type": "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      },
      host,
    ),
    db,
    NOW,
    KEYS,
  );
}

const INCIDENT = { title: "Checkout fails", impact: "outage", components: ["payments-checkout"] };

describe("admin access", () => {
  it("refuses requests that did not pass Cloudflare Access", async () => {
    const response = await handle(
      request("/api/admin/incidents", { method: "POST", body: JSON.stringify(INCIDENT) }),
      acmeDatabase(),
      NOW,
    );
    expect(response.status).toBe(403);
  });

  it("answers a wrong method with 405", async () => {
    const response = await admin(acmeDatabase(), "/api/admin/incidents", "GET");
    expect(response.status).toBe(405);
  });
});

describe("incidents", () => {
  it("appear on the page and raise the components they name on the next minute", async () => {
    const db = acmeDatabase();
    const created = await admin(db, "/api/admin/incidents", "POST", INCIDENT);
    expect(created.status).toBe(201);
    const net = new FakeNet();
    await runMinute(db, NOW, net.fetcher, net.clock);
    const state = db.rows<{ state: string }>(
      "SELECT state FROM minutes WHERE component_id = 'payments-checkout'",
    );
    expect(state).toEqual([{ state: "outage" }]);
    const html = await (await handle(request("/?lang=en"), db, NOW)).text();
    expect(html).toContain("Checkout fails");
    expect(html).toContain("Investigating");
  });

  it("stop raising components once resolved", async () => {
    const db = acmeDatabase();
    const { id } = (await (await admin(db, "/api/admin/incidents", "POST", INCIDENT)).json()) as { id: number };
    const resolved = await admin(db, `/api/admin/incidents/${id}`, "PATCH", {
      state: "resolved",
      body: "Fixed the payment provider key.",
    });
    expect(resolved.status).toBe(200);
    expect(db.rows("SELECT resolved_at FROM incidents")).toEqual([{ resolved_at: NOW }]);
    db.exec(
      "INSERT INTO push_latest (tenant_id, component_id, ts, state) VALUES ('acme', 'payments-checkout', ?, 'operational')",
      NOW,
    );
    const net = new FakeNet();
    await runMinute(db, NOW, net.fetcher, net.clock);
    expect(
      db.rows("SELECT state FROM minutes WHERE component_id = 'payments-checkout'"),
    ).toEqual([{ state: "operational" }]);
  });

  it("refuse components the tenant does not have", async () => {
    const response = await admin(acmeDatabase(), "/api/admin/incidents", "POST", {
      ...INCIDENT,
      components: ["billing-cron"],
    });
    expect(response.status).toBe(400);
  });

  it("refuse an incident without an impact", async () => {
    const response = await admin(acmeDatabase(), "/api/admin/incidents", "POST", { title: "Hm" });
    expect(response.status).toBe(400);
  });

  it("cannot be changed from another tenant's hostname", async () => {
    const db = acmeDatabase();
    addGlobex(db);
    db.exec("UPDATE tenants SET access_team = ?, access_aud = ? WHERE id = 'globex'", ACCESS.team, ACCESS.aud);
    const { id } = (await (await admin(db, "/api/admin/incidents", "POST", INCIDENT)).json()) as { id: number };
    const response = await admin(
      db,
      `/api/admin/incidents/${id}`,
      "PATCH",
      { state: "resolved" },
      "https://status.globex.example",
    );
    expect(response.status).toBe(404);
  });
});

describe("maintenance", () => {
  const WINDOW = {
    title: "Database upgrade",
    components: ["api"],
    starts_at: new Date((NOW + 3600) * 1000).toISOString(),
    ends_at: new Date((NOW + 7200) * 1000).toISOString(),
  };

  it("is planned through the API and shown on the page", async () => {
    const db = acmeDatabase();
    expect((await admin(db, "/api/admin/maintenance", "POST", WINDOW)).status).toBe(201);
    const html = await (await handle(request("/?lang=en"), db, NOW)).text();
    expect(html).toContain("Database upgrade");
    expect(html).toContain("Planned maintenance");
  });

  it("needs ends_at after starts_at", async () => {
    const response = await admin(acmeDatabase(), "/api/admin/maintenance", "POST", {
      ...WINDOW,
      ends_at: WINDOW.starts_at,
    });
    expect(response.status).toBe(400);
  });

  it("can be cancelled", async () => {
    const db = acmeDatabase();
    const { id } = (await (await admin(db, "/api/admin/maintenance", "POST", WINDOW)).json()) as { id: number };
    expect((await admin(db, `/api/admin/maintenance/${id}`, "DELETE")).status).toBe(204);
    expect((await admin(db, `/api/admin/maintenance/${id}`, "DELETE")).status).toBe(404);
  });
});
