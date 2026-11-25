import { describe, expect, it } from "vitest";
import { dayStart, runMinute, utcDay } from "../src/cron";
import { FakeNet, acmeDatabase, type FakeSite } from "./helpers";

const HOUR = 1_792_742_400;
const MINUTE = HOUR + 60;

interface MinuteRow {
  component_id: string;
  minute: number;
  state: string;
  latency_ms: number | null;
}

function minutes(db: ReturnType<typeof acmeDatabase>, at = MINUTE): Record<string, MinuteRow> {
  const rows = db.rows<MinuteRow>(
    "SELECT component_id, minute, state, latency_ms FROM minutes WHERE minute = ?",
    at,
  );
  return Object.fromEntries(rows.map((row) => [row.component_id, row]));
}

function pushed(db: ReturnType<typeof acmeDatabase>, id: string, state: string, ts: number): void {
  db.exec(
    "INSERT INTO push_latest (tenant_id, component_id, ts, state) VALUES ('acme', ?, ?, ?)",
    id,
    ts,
    state,
  );
}

async function run(sites: Record<string, FakeSite> = {}, prepare?: (db: ReturnType<typeof acmeDatabase>) => void, at = MINUTE) {
  const db = acmeDatabase();
  prepare?.(db);
  const net = new FakeNet(sites);
  await runMinute(db, at + 7, net.fetcher, net.clock);
  return { db, net, rows: minutes(db, at) };
}

describe("external probes", () => {
  it("record a fast 2xx answer as operational with its latency", async () => {
    const { rows } = await run({ "https://acme.example": { status: [200], latencyMs: 85 } });
    expect(rows.website).toMatchObject({ state: "operational", latency_ms: 85 });
  });

  it("retry a failed probe once before it counts", async () => {
    const { rows, net } = await run({ "https://acme.example": { status: [503, 200] } });
    expect(rows.website?.state).toBe("operational");
    expect(net.calls.filter((url) => url === "https://acme.example")).toHaveLength(2);
  });

  it("record outage when the probe fails twice", async () => {
    const { rows } = await run({ "https://acme.example": { status: [0, 500] } });
    expect(rows.website?.state).toBe("outage");
  });

  it("record no latency when nothing answered at all", async () => {
    const { rows } = await run({ "https://acme.example": { status: [0] } });
    expect(rows.website).toMatchObject({ state: "outage", latency_ms: null });
  });

  it("record degraded when the answer takes longer than three seconds", async () => {
    const { rows } = await run({ "https://acme.example": { status: [200], latencyMs: 3500 } });
    expect(rows.website?.state).toBe("degraded");
  });

  it("fetch only the URLs a tenant marked public", async () => {
    const { net } = await run();
    expect(new Set(net.calls)).toEqual(
      new Set(["https://acme.example", "https://api.acme.example/health", "https://docs.acme.example"]),
    );
  });
});

describe("internal pushes", () => {
  it("leave a push-only component no_data while the reporter is silent", async () => {
    const { rows } = await run();
    expect(rows["payments-checkout"]?.state).toBe("no_data");
    expect(rows.api?.state).toBe("no_data");
  });

  it("record the latest fresh push", async () => {
    const { rows } = await run({}, (db) => {
      pushed(db, "payments-checkout", "degraded", MINUTE - 30);
      pushed(db, "api", "operational", MINUTE - 30);
    });
    expect(rows["payments-checkout"]?.state).toBe("degraded");
    expect(rows.api?.state).toBe("operational");
  });

  it("turn a push older than five minutes into no_data", async () => {
    const { rows } = await run({}, (db) => pushed(db, "payments-checkout", "operational", MINUTE - 400));
    expect(rows["payments-checkout"]?.state).toBe("no_data");
  });
});

describe("incidents and maintenance", () => {
  it("record maintenance for the components a window names, only while it is open", async () => {
    const { rows } = await run({}, (db) =>
      db.exec(
        "INSERT INTO maintenance (tenant_id, title, components, starts_at, ends_at) VALUES ('acme', 'DB upgrade', '[\"docs\"]', ?, ?)",
        MINUTE - 600,
        MINUTE + 600,
      ),
    );
    expect(rows.docs?.state).toBe("maintenance");
    expect(rows.website?.state).toBe("operational");
  });

  it("raise every component when an open incident names none", async () => {
    const { rows } = await run({}, (db) =>
      db.exec(
        "INSERT INTO incidents (tenant_id, title, state, impact, components, started_at, updated_at) VALUES ('acme', 'Slow', 'investigating', 'degraded', '[]', ?, ?)",
        MINUTE,
        MINUTE,
      ),
    );
    expect(rows.website?.state).toBe("degraded");
    expect(rows.docs?.state).toBe("degraded");
  });
});

describe("storage", () => {
  it("skips a second run of the same minute and counts nothing twice", async () => {
    const db = acmeDatabase();
    const net = new FakeNet();
    expect((await runMinute(db, MINUTE + 1, net.fetcher, net.clock)).skipped).toBe(false);
    expect((await runMinute(db, MINUTE + 50, net.fetcher, net.clock)).skipped).toBe(true);
    expect(db.rows("SELECT * FROM minutes")).toHaveLength(4);
    expect(db.rows("SELECT operational FROM daily WHERE component_id = 'website'")).toEqual([
      { operational: 1 },
    ]);
  });

  it("adds every minute to the day's counts of its component", async () => {
    const db = acmeDatabase();
    const net = new FakeNet({ "https://acme.example": { status: [0] } });
    for (const at of [HOUR - 120, HOUR - 60, HOUR]) {
      await runMinute(db, at, net.fetcher, net.clock);
    }
    const rows = db.rows<{ component_id: string; day: string; outage: number; no_data: number }>(
      "SELECT component_id, day, outage, no_data FROM daily ORDER BY component_id",
    );
    expect(rows.find((row) => row.component_id === "website")).toMatchObject({
      day: utcDay(HOUR),
      outage: 3,
    });
    expect(rows.find((row) => row.component_id === "api")).toMatchObject({ no_data: 3 });
  });

  it("starts a new day at midnight UTC", async () => {
    const db = acmeDatabase();
    const net = new FakeNet();
    const midnight = dayStart(HOUR) + 86400;
    await runMinute(db, midnight - 60, net.fetcher, net.clock);
    await runMinute(db, midnight, net.fetcher, net.clock);
    const days = db.rows<{ day: string }>(
      "SELECT day FROM daily WHERE component_id = 'website' ORDER BY day",
    );
    expect(days.map((row) => row.day)).toEqual([utcDay(midnight - 60), utcDay(midnight)]);
  });

  it("deletes minutes older than two days and days older than ninety", async () => {
    const db = acmeDatabase();
    const oldMinute = HOUR - 2 * 86400 - 60;
    const keptMinute = HOUR - 2 * 86400 + 60;
    for (const minute of [oldMinute, keptMinute]) {
      db.exec(
        "INSERT INTO minutes (tenant_id, component_id, minute, state) VALUES ('acme', 'website', ?, 'operational')",
        minute,
      );
    }
    const oldDay = utcDay(dayStart(HOUR) - 90 * 86400);
    const keptDay = utcDay(dayStart(HOUR) - 89 * 86400);
    for (const day of [oldDay, keptDay]) {
      db.exec(
        "INSERT INTO daily (tenant_id, component_id, day, operational) VALUES ('acme', 'website', ?, 1440)",
        day,
      );
    }
    const net = new FakeNet();
    await runMinute(db, HOUR, net.fetcher, net.clock);
    const kept = db.rows<{ minute: number }>("SELECT minute FROM minutes WHERE minute < ?", HOUR);
    expect(kept.map((row) => row.minute)).toEqual([keptMinute]);
    const days = db.rows<{ day: string }>("SELECT day FROM daily WHERE day < ? ORDER BY day", utcDay(HOUR));
    expect(days.map((row) => row.day)).toEqual([keptDay]);
  });
});
