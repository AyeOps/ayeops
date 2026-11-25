import { describe, expect, it } from "vitest";
import { handle } from "../src/app";
import { MAX_PUSH_BYTES, sign, validatePush } from "../src/push";
import { NOW, PUSH, acmeDatabase, addGlobex, pushRequest, request } from "./helpers";

interface Row {
  component_id: string;
  ts: number;
  state: string;
}

function latest(db: ReturnType<typeof acmeDatabase>): Row[] {
  return db.rows<Row>(
    "SELECT component_id, ts, state FROM push_latest WHERE tenant_id = 'acme' ORDER BY component_id",
  );
}

async function send(body: string, signature: string, now = NOW, host?: string) {
  const db = acmeDatabase();
  const response = await handle(pushRequest(body, signature, host), db, now);
  return { db, response };
}

describe("push contract fixtures", () => {
  it.each(PUSH.valid)("accepts a signed push: $name", async (fixture) => {
    const { db, response } = await send(fixture.body, fixture.signature);
    expect(response.status).toBe(202);
    const body = JSON.parse(fixture.body) as { components: { id: string; state: string }[] };
    expect(await response.json()).toEqual({ accepted: body.components.length });
    for (const component of body.components) {
      expect(latest(db)).toContainEqual(expect.objectContaining({ component_id: component.id, state: component.state }));
    }
  });

  it.each(PUSH.invalid)("rejects an off-contract body with 400: $name", async (fixture) => {
    expect(validatePush(JSON.parse(fixture.body)).ok).toBe(false);
    const { db, response } = await send(fixture.body, fixture.signature);
    expect(response.status).toBe(400);
    expect(latest(db)).toEqual([]);
  });

  it.each(PUSH.rejected)("refuses $name with $status", async (fixture) => {
    const { db, response } = await send(fixture.body, fixture.signature);
    expect(response.status).toBe(fixture.status);
    expect(latest(db)).toEqual([]);
  });

  it("signs exactly like the Python reporter does", async () => {
    for (const fixture of [...PUSH.valid, ...PUSH.invalid, ...PUSH.rejected]) {
      expect(await sign(fixture.body, fixture.signed_with ?? PUSH.secret)).toBe(fixture.signature);
    }
  });
});

describe("push handling", () => {
  const [first] = PUSH.valid;
  if (first === undefined) {
    throw new Error("push.json has no valid case");
  }

  it("treats a replayed push as a no-op", async () => {
    const db = acmeDatabase();
    await handle(pushRequest(first.body, first.signature), db, NOW);
    const before = latest(db);
    const again = await handle(pushRequest(first.body, first.signature), db, NOW + 30);
    expect(again.status).toBe(202);
    expect(latest(db)).toEqual(before);
  });

  it("never lets an older push overwrite a newer one", async () => {
    const db = acmeDatabase();
    const newer = JSON.stringify({ tenant: "acme", ts: NOW, components: [{ id: "api", state: "outage" }] });
    const older = JSON.stringify({ tenant: "acme", ts: NOW - 60, components: [{ id: "api", state: "operational" }] });
    await handle(pushRequest(newer, await sign(newer, PUSH.secret)), db, NOW);
    const response = await handle(pushRequest(older, await sign(older, PUSH.secret)), db, NOW);
    expect(response.status).toBe(202);
    expect(latest(db)).toEqual([{ component_id: "api", ts: NOW, state: "outage" }]);
  });

  it("refuses a push for one tenant sent to another tenant's hostname", async () => {
    const db = acmeDatabase();
    addGlobex(db);
    const response = await handle(
      pushRequest(first.body, first.signature, "https://status.globex.example"),
      db,
      NOW,
    );
    expect(response.status).toBe(400);
  });

  it("accepts nothing for a tenant whose secret is not set", async () => {
    const db = acmeDatabase();
    db.exec("UPDATE tenants SET secret = NULL");
    const response = await handle(pushRequest(first.body, first.signature), db, NOW);
    expect(response.status).toBe(401);
  });

  it("refuses a body over the size limit with 413", async () => {
    const body = " ".repeat(MAX_PUSH_BYTES + 1);
    const { response } = await send(body, await sign(body, PUSH.secret));
    expect(response.status).toBe(413);
  });

  it("refuses a body whose declared length is over the limit before reading it", async () => {
    let pulled = false;
    const stream = new ReadableStream<Uint8Array>(
      {
        pull(controller) {
          pulled = true;
          controller.enqueue(new Uint8Array(MAX_PUSH_BYTES + 1));
        },
      },
      { highWaterMark: 0 },
    );
    const response = await handle(
      request("/api/push", {
        method: "POST",
        body: stream,
        headers: { "content-length": String(MAX_PUSH_BYTES + 1), [PUSH.header]: first.signature },
        duplex: "half",
      } as RequestInit),
      acmeDatabase(),
      NOW,
    );
    expect(response.status).toBe(413);
    expect(pulled).toBe(false);
  });

  it("stops reading a streamed body without a length once it passes the limit", async () => {
    const chunk = new Uint8Array(4096).fill(32);
    let sent = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        sent += chunk.byteLength;
        controller.enqueue(chunk);
      },
    });
    const response = await handle(
      request("/api/push", {
        method: "POST",
        body: stream,
        headers: { [PUSH.header]: first.signature },
        duplex: "half",
      } as RequestInit),
      acmeDatabase(),
      NOW,
    );
    expect(response.status).toBe(413);
    expect(sent).toBeLessThanOrEqual(MAX_PUSH_BYTES + 2 * chunk.byteLength);
  });

  it("refuses a push without a signature header", async () => {
    const db = acmeDatabase();
    const response = await handle(
      request("/api/push", { method: "POST", body: first.body }),
      db,
      NOW,
    );
    expect(response.status).toBe(401);
  });

  it("answers anything but POST with 405", async () => {
    const response = await handle(request("/api/push"), acmeDatabase(), NOW);
    expect(response.status).toBe(405);
  });
});
