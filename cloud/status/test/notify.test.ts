import { describe, expect, it } from "vitest";
import { runMinute } from "../src/cron";
import type { Database, Statement } from "../src/db";
import { MAX_NOTICE_LINES, TELEGRAM_API, changeOf, noticeText, type Watched } from "../src/notify";
import { FakeNet, acmeDatabase, addGlobex, type FakeSite } from "./helpers";

const TOKEN = "123456:test-token";
const TELEGRAM = `${TELEGRAM_API}/bot${TOKEN}/sendMessage`;
const START = 1_792_742_400 + 600;
const WEBSITE = "https://acme.example";
const DOCS = "https://docs.acme.example";
const UP = [200];
const DOWN = [0];

interface Tick {
  website?: number[];
  slow?: boolean;
  docs?: number[];
  telegram?: number;
  reporter?: boolean;
  token?: string | null;
}

class Watch {
  readonly db = acmeDatabase();
  readonly sites: Record<string, FakeSite> = {};
  readonly net = new FakeNet(this.sites);
  minute = START;

  constructor(minState = "outage") {
    this.db.exec("UPDATE tenants SET notify_min = ?", minState);
  }

  async run(at: number, tick: Tick = {}) {
    this.sites[WEBSITE] = { status: [...(tick.website ?? UP)], latencyMs: tick.slow ? 3500 : 120 };
    this.sites[DOCS] = { status: [...(tick.docs ?? UP)] };
    this.sites[TELEGRAM] = { status: [tick.telegram ?? 200] };
    if (tick.reporter ?? true) {
      for (const id of ["api", "payments-checkout"]) {
        this.db.exec(
          "INSERT INTO push_latest (tenant_id, component_id, ts, state) VALUES ('acme', ?, ?, 'operational') " +
            "ON CONFLICT (tenant_id, component_id) DO UPDATE SET ts = excluded.ts, state = excluded.state",
          id,
          at,
        );
      }
    }
    return runMinute(this.db, at + 5, this.net.fetcher, this.net.clock, tick.token === undefined ? TOKEN : tick.token);
  }

  async tick(tick: Tick = {}) {
    const result = await this.run(this.minute, tick);
    this.minute += 60;
    return result;
  }

  async ticks(count: number, tick: Tick = {}) {
    for (let index = 0; index < count; index += 1) {
      await this.tick(tick);
    }
  }

  sends(): { chat_id: string; text: string; link_preview_options: unknown }[] {
    return this.net.bodies
      .filter((call) => call.url.startsWith(TELEGRAM_API))
      .map((call) => JSON.parse(call.body ?? "{}") as { chat_id: string; text: string; link_preview_options: unknown });
  }

  texts(): string[] {
    return this.sends().map((send) => send.text);
  }

  notices(): { component_id: string; state: string; since: number }[] {
    return this.db.rows("SELECT component_id, state, since FROM notices ORDER BY component_id");
  }
}

describe("debounce", () => {
  it("says nothing after one bad minute and announces the second", async () => {
    const watch = new Watch();
    await watch.tick({ website: DOWN });
    expect(watch.texts()).toEqual([]);
    const second = await watch.tick({ website: DOWN });
    expect(second.notices).toEqual({ sent: 1, failed: 0 });
    expect(watch.texts()).toEqual(["Acme: стан сервісів\nWebsite: Збій з 08:10 UTC\nhttps://status.acme.example"]);
  });

  it("ignores a one-minute blip", async () => {
    const watch = new Watch();
    await watch.tick({ website: DOWN });
    await watch.ticks(3);
    await watch.tick({ website: DOWN });
    await watch.tick();
    expect(watch.texts()).toEqual([]);
  });

  it("does not repeat while the state holds", async () => {
    const watch = new Watch();
    await watch.ticks(10, { website: DOWN });
    expect(watch.texts()).toHaveLength(1);
  });

  it("needs two consecutive cron minutes, not two bad minutes with a gap", async () => {
    const watch = new Watch();
    await watch.run(START, { website: DOWN });
    await watch.run(START + 120, { website: DOWN });
    expect(watch.texts()).toEqual([]);
  });
});

describe("recovery", () => {
  it("sends one recovery message with the length of the outage", async () => {
    const watch = new Watch();
    await watch.ticks(3, { website: DOWN });
    await watch.tick();
    expect(watch.texts()).toHaveLength(1);
    await watch.ticks(5);
    const texts = watch.texts();
    expect(texts).toHaveLength(2);
    expect(texts[1]).toBe("Acme: стан сервісів\nWebsite: знову працює, перерва 3 хв\nhttps://status.acme.example");
    expect(watch.notices()).toEqual([{ component_id: "website", state: "operational", since: START + 3 * 60 }]);
  });

  it("announces a change between two bad states and counts the outage from its start", async () => {
    const watch = new Watch("degraded");
    await watch.ticks(2, { website: DOWN });
    await watch.ticks(2, { slow: true });
    await watch.ticks(2);
    expect(watch.texts().map((text) => text.split("\n")[1])).toEqual([
      "Website: Збій з 08:10 UTC",
      "Website: Перебої з 08:12 UTC",
      "Website: знову працює, перерва 4 хв",
    ]);
  });

  it("keeps an open outage through maintenance and recovers after it", async () => {
    const watch = new Watch();
    await watch.ticks(2, { website: DOWN });
    watch.db.exec(
      "INSERT INTO maintenance (tenant_id, title, components, starts_at, ends_at) VALUES ('acme', 'Fix', '[\"website\"]', ?, ?)",
      watch.minute,
      watch.minute + 180,
    );
    await watch.ticks(3, { website: DOWN });
    expect(watch.texts()).toHaveLength(1);
    await watch.ticks(2);
    expect(watch.texts()).toHaveLength(2);
    expect(watch.texts()[1]).toContain("Website: знову працює");
  });
});

describe("exactly once", () => {
  it("sends nothing on a duplicate run of the same minute", async () => {
    const watch = new Watch();
    await watch.tick({ website: DOWN });
    const at = watch.minute;
    await watch.tick({ website: DOWN });
    const again = await watch.run(at, { website: DOWN });
    expect(again.skipped).toBe(true);
    expect(again.notices).toEqual({ sent: 0, failed: 0 });
    expect(watch.texts()).toHaveLength(1);
  });

  it("remembers what it announced in D1, so a fresh Worker does not announce it again", async () => {
    const watch = new Watch();
    await watch.ticks(2, { website: DOWN });
    expect(watch.notices()).toEqual([{ component_id: "website", state: "outage", since: START }]);
    const restarted = new FakeNet({ [WEBSITE]: { status: DOWN }, [TELEGRAM]: { status: [200] } });
    await runMinute(watch.db, watch.minute + 5, restarted.fetcher, restarted.clock, TOKEN);
    expect(restarted.calls.filter((url) => url.startsWith(TELEGRAM_API))).toEqual([]);
  });
});

describe("a failed send", () => {
  it("leaves the minute recorded, marks nothing and retries on the next minute", async () => {
    const watch = new Watch();
    await watch.tick({ website: DOWN });
    const failed = await watch.tick({ website: DOWN, telegram: 500 });
    expect(failed).toMatchObject({ skipped: false, notices: { sent: 0, failed: 1 } });
    expect(watch.db.rows("SELECT state FROM minutes WHERE component_id = 'website'")).toHaveLength(2);
    expect(watch.notices()).toEqual([]);
    const network = await watch.tick({ website: DOWN, telegram: 0 });
    expect(network.notices).toEqual({ sent: 0, failed: 1 });
    expect(watch.notices()).toEqual([]);
    const retried = await watch.tick({ website: DOWN });
    expect(retried.notices).toEqual({ sent: 1, failed: 0 });
    const texts = watch.texts();
    expect(texts).toHaveLength(3);
    expect(new Set(texts)).toEqual(
      new Set(["Acme: стан сервісів\nWebsite: Збій з 08:10 UTC\nhttps://status.acme.example"]),
    );
    expect(watch.notices()).toEqual([{ component_id: "website", state: "outage", since: START }]);
    await watch.ticks(3, { website: DOWN });
    expect(watch.texts()).toHaveLength(3);
    await watch.ticks(2);
    expect(watch.texts().at(-1)).toContain("Website: знову працює, перерва 7 хв");
  });

  it("drops a pending announcement when the component recovers before it got through", async () => {
    const watch = new Watch();
    await watch.tick({ website: DOWN });
    await watch.tick({ website: DOWN, telegram: 503 });
    await watch.ticks(3);
    expect(watch.texts()).toHaveLength(1);
    expect(watch.notices()).toEqual([]);
  });
});

describe("batching", () => {
  it("puts every component of a tenant that changed in one minute into one message", async () => {
    const watch = new Watch();
    await watch.ticks(2, { website: DOWN, docs: DOWN });
    const sends = watch.sends();
    expect(sends).toHaveLength(1);
    expect(sends[0]?.text.split("\n")).toEqual([
      "Acme: стан сервісів",
      "Website: Збій з 08:10 UTC",
      "Документація: Збій з 08:10 UTC",
      "https://status.acme.example",
    ]);
    expect(sends[0]?.chat_id).toBe("-1001234567890");
    expect(sends[0]?.link_preview_options).toEqual({ is_disabled: true });
    expect(JSON.stringify(sends)).not.toContain(TOKEN);
  });

  it("sends one message per tenant to the tenant's own chat", async () => {
    const watch = new Watch();
    addGlobex(watch.db);
    watch.db.exec("UPDATE tenants SET notify_chat = '200300400', notify_min = 'outage' WHERE id = 'globex'");
    watch.sites["https://shop.globex.example"] = { status: DOWN };
    await watch.ticks(2, { website: DOWN });
    const sends = watch.sends();
    expect(sends.map((send) => send.chat_id).sort()).toEqual(["-1001234567890", "200300400"]);
    const globex = sends.find((send) => send.chat_id === "200300400");
    expect(globex?.text).toBe("Globex status\nShop: Outage since 08:10 UTC\nhttps://status.globex.example");
  });

  it("caps a long message and says how many changes it left out", () => {
    const watched: Watched[] = Array.from({ length: MAX_NOTICE_LINES + 4 }, (_, index) => ({
      tenantId: "acme",
      componentId: `c${index}`,
      title: `C${index}`,
      state: "outage",
      previous: "outage",
      notified: null,
      started: null,
    }));
    const changes = watched.flatMap((component) => {
      const change = changeOf(component, "outage", START);
      return change === null ? [] : [{ change, started: null }];
    });
    const tenant = { id: "acme", hostname: "status.acme.example", title: "Acme", locale: "en", notify_chat: "1", notify_min: "outage" };
    const lines = noticeText(tenant, changes).split("\n");
    expect(lines).toHaveLength(MAX_NOTICE_LINES + 3);
    expect(lines.at(-2)).toBe("and 4 more");
  });
});

describe("severity filter", () => {
  it("with outage only, stays quiet about degraded and no_data", async () => {
    const watch = new Watch("outage");
    await watch.ticks(4, { slow: true, reporter: false });
    expect(watch.db.rows("SELECT DISTINCT state FROM minutes WHERE component_id = 'website'")).toEqual([
      { state: "degraded" },
    ]);
    expect(watch.texts()).toEqual([]);
  });

  it("with degraded, announces a slow probe but not a silent reporter", async () => {
    const watch = new Watch("degraded");
    await watch.ticks(2, { slow: true, reporter: false });
    expect(watch.texts()).toEqual(["Acme: стан сервісів\nWebsite: Перебої з 08:10 UTC\nhttps://status.acme.example"]);
  });

  it("with no_data, announces a silent reporter", async () => {
    const watch = new Watch("no_data");
    await watch.ticks(2, { reporter: false });
    expect(watch.texts()).toEqual([
      "Acme: стан сервісів\nAPI: Немає даних з 08:10 UTC\nPayments & checkout: Немає даних з 08:10 UTC\nhttps://status.acme.example",
    ]);
  });

  it("with outage only, still announces an outage", async () => {
    const watch = new Watch("outage");
    await watch.ticks(2, { reporter: false, website: DOWN });
    expect(watch.texts()).toEqual(["Acme: стан сервісів\nWebsite: Збій з 08:10 UTC\nhttps://status.acme.example"]);
  });
});

class CountedStatement implements Statement {
  constructor(
    readonly inner: Statement,
    private readonly counts: { queries: number; batched: number },
  ) {}

  bind(...values: unknown[]): Statement {
    return new CountedStatement(this.inner.bind(...values), this.counts);
  }

  all<T>(): Promise<{ results: T[] }> {
    this.counts.queries += 1;
    return this.inner.all<T>();
  }

  first<T>(): Promise<T | null> {
    this.counts.queries += 1;
    return this.inner.first<T>();
  }

  run(): Promise<unknown> {
    this.counts.queries += 1;
    return this.inner.run();
  }
}

class CountedD1 implements Database {
  readonly counts = { queries: 0, batched: 0 };

  constructor(private readonly inner: Database) {}

  prepare(query: string): Statement {
    return new CountedStatement(this.inner.prepare(query), this.counts);
  }

  batch(statements: Statement[]): Promise<unknown[]> {
    this.counts.queries += 1;
    this.counts.batched += statements.length;
    return this.inner.batch(statements.map((statement) => (statement as CountedStatement).inner));
  }
}

describe("budget", () => {
  it("adds one read to a quiet minute and two queries and one subrequest per tenant to a minute that announces", async () => {
    const watch = new Watch();
    const components = watch.db.rows("SELECT id FROM components").length;
    const counted = new CountedD1(watch.db);
    const run = async () => {
      watch.sites[WEBSITE] = { status: [...DOWN] };
      watch.sites[TELEGRAM] = { status: [200] };
      counted.counts.queries = 0;
      counted.counts.batched = 0;
      const before = watch.net.calls.length;
      await runMinute(counted, watch.minute + 5, watch.net.fetcher, watch.net.clock, TOKEN);
      watch.minute += 60;
      return { ...counted.counts, fetches: watch.net.calls.slice(before) };
    };
    const quiet = await run();
    expect(quiet).toMatchObject({ queries: 6 + 1, batched: 2 * components + 1 });
    const announcing = await run();
    expect(announcing).toMatchObject({ queries: 6 + 1 + 2, batched: 2 * components + 1 });
    expect(announcing.fetches.filter((url) => url.startsWith(TELEGRAM_API))).toHaveLength(1);
  });
});

describe("without a notify config", () => {
  it("sends nothing when the tenant has no chat", async () => {
    const watch = new Watch();
    watch.db.exec("UPDATE tenants SET notify_chat = NULL");
    const results = [];
    for (let index = 0; index < 4; index += 1) {
      results.push(await watch.tick({ website: DOWN }));
    }
    expect(watch.net.calls.filter((url) => url.startsWith(TELEGRAM_API))).toEqual([]);
    expect(results.every((result) => result.notices.sent === 0 && result.notices.failed === 0)).toBe(true);
    expect(watch.notices()).toEqual([]);
  });

  it("sends nothing when the Worker has no bot token", async () => {
    const watch = new Watch();
    await watch.ticks(4, { website: DOWN, token: null });
    expect(watch.net.calls.filter((url) => url.startsWith(TELEGRAM_API))).toEqual([]);
    expect(watch.notices()).toEqual([]);
  });
});
