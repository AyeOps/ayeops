import { describe, expect, it } from "vitest";
import { handle } from "../src/app";
import { runMinute } from "../src/cron";
import { PROJECT_URL } from "../src/page";
import { HISTORY_DAYS } from "../src/view";
import { COMPONENTS, FakeNet, NOW, acmeDatabase, addGlobex, request } from "./helpers";

const INTERNAL_SERVICE_NAMES = ["api-worker", "billing-cron"];

async function page(db = acmeDatabase(), path = "/", init: RequestInit = {}, now = NOW) {
  const response = await handle(request(path, init), db, now);
  return { response, html: await response.text() };
}

async function ticked(sites = {}) {
  const db = acmeDatabase();
  const net = new FakeNet(sites);
  await runMinute(db, NOW, net.fetcher, net.clock);
  return db;
}

describe("tenant resolution", () => {
  it("answers 404 on a hostname no tenant claims", async () => {
    const response = await handle(request("/", {}, "https://status.unknown.example"), acmeDatabase(), NOW);
    expect(response.status).toBe(404);
  });

  it("serves two tenants from one Worker without mixing their components", async () => {
    const db = acmeDatabase();
    addGlobex(db);
    const globex = await (await handle(request("/", {}, "https://status.globex.example"), db, NOW)).text();
    expect(globex).toContain("Shop");
    expect(globex).not.toContain("Website");
    const acme = await (await handle(request("/"), db, NOW)).text();
    expect(acme).not.toContain("Shop");
  });
});

describe("public page", () => {
  it("links the footer to the AyeOps project in the page's language", async () => {
    const db = acmeDatabase();
    for (const [lang, text] of [
      ["en", "Powered by AyeOps"],
      ["uk", "Працює на AyeOps"],
      ["ru", "Работает на AyeOps"],
    ]) {
      const html = await (await handle(request(`/?lang=${lang}`), db, NOW)).text();
      expect(html).toContain(`<a href="${PROJECT_URL}" rel="noopener noreferrer">${text}</a></footer>`);
    }
  });

  it("lists the components in manifest order", async () => {
    const { html } = await page();
    const positions = COMPONENTS.components.map((component) => html.indexOf(`id="c-${component.id}"`));
    expect(positions.every((position) => position > 0)).toBe(true);
    expect([...positions].sort((a, b) => a - b)).toEqual(positions);
  });

  it("never shows internal service names", async () => {
    const db = await ticked();
    const { html } = await page(db);
    const api = await (await handle(request("/api/status"), db, NOW)).text();
    for (const name of INTERNAL_SERVICE_NAMES) {
      expect(html).not.toContain(name);
      expect(api).not.toContain(name);
    }
  });

  it("never shows the Telegram chat of the tenant", async () => {
    const db = await ticked();
    const { html } = await page(db);
    const api = await (await handle(request("/api/status"), db, NOW)).text();
    const chat = COMPONENTS.notify?.telegram_chat_id ?? "";
    expect(chat).not.toBe("");
    expect(html).not.toContain(chat);
    expect(api).not.toContain(chat);
  });

  it("loads no external assets and forbids them in its CSP", async () => {
    const { response, html } = await page();
    expect(html).not.toMatch(/<script|<link|<img|src=|url\(/i);
    expect(html.replaceAll(`href="${PROJECT_URL}"`, "")).not.toMatch(/href="(?!\?lang=)/);
    expect(response.headers.get("content-security-policy")).toContain("default-src 'none'");
  });

  it("defines a dark palette next to the light one", async () => {
    const { html } = await page();
    expect(html).toContain("prefers-color-scheme:dark");
    expect(html).toContain('name="viewport"');
  });

  it("draws ninety daily bars per component", async () => {
    const { html } = await page();
    const bars = html.match(/class="bar /g) ?? [];
    expect(bars).toHaveLength(HISTORY_DAYS * COMPONENTS.components.length);
  });

  it("escapes HTML that arrives in titles", async () => {
    const db = acmeDatabase();
    db.exec("UPDATE components SET title = '<b>API</b>' WHERE id = 'api'");
    const { html } = await page(db);
    expect(html).toContain("&lt;b&gt;API&lt;/b&gt;");
    expect(html).not.toContain("<b>API</b>");
  });
});

describe("current state", () => {
  it("shows the state of the newest minute and when it was written", async () => {
    const db = await ticked({ "https://acme.example": { status: [0] } });
    const { html } = await page(db, "/?lang=en");
    expect(html).toContain('<span class="state t-outage">Outage</span>');
    expect(html).toContain("Outage in progress");
    expect(html).toContain("Updated ");
  });

  it("falls back to no_data when the cron has been silent for more than three minutes", async () => {
    const db = await ticked();
    const { html } = await page(db, "/?lang=en", {}, NOW + 240);
    expect(html).not.toContain(`class="state t-operational"`);
    expect(html).toContain("Not enough data yet");
  });

  it("says it was never updated before the first cron run", async () => {
    const { html } = await page(acmeDatabase(), "/?lang=en");
    expect(html).toContain("Not updated yet");
  });

  it("shows open incidents and planned maintenance with the components they name", async () => {
    const db = acmeDatabase();
    db.exec(
      "INSERT INTO incidents (tenant_id, title, state, impact, components, started_at, updated_at) VALUES ('acme', 'Payments fail', 'identified', 'outage', '[\"payments-checkout\"]', ?, ?)",
      NOW - 600,
      NOW - 60,
    );
    db.exec(
      "INSERT INTO maintenance (tenant_id, title, components, starts_at, ends_at) VALUES ('acme', 'Database upgrade', '[]', ?, ?)",
      NOW + 3600,
      NOW + 7200,
    );
    const { html } = await page(db, "/?lang=en");
    expect(html).toContain("Payments fail");
    expect(html).toContain("Identified");
    expect(html).toContain("Affects: Payments &amp; checkout");
    expect(html).toContain("Database upgrade");
    expect(html).toContain("Affects: all components");
  });

  it("hides incidents resolved more than a day ago", async () => {
    const db = acmeDatabase();
    db.exec(
      "INSERT INTO incidents (tenant_id, title, state, impact, started_at, updated_at, resolved_at) VALUES ('acme', 'Old one', 'resolved', 'outage', ?, ?, ?)",
      NOW - 200000,
      NOW - 100000,
      NOW - 100000,
    );
    const { html } = await page(db);
    expect(html).not.toContain("Old one");
  });
});

describe("languages", () => {
  it("default to the tenant locale", async () => {
    const { response, html } = await page();
    expect(response.headers.get("content-language")).toBe(COMPONENTS.locale);
    expect(html).toContain(`<html lang="${COMPONENTS.locale}">`);
  });

  it("follow Accept-Language when no lang is asked for", async () => {
    const { html } = await page(acmeDatabase(), "/", { headers: { "accept-language": "ru-UA,ru;q=0.9,en;q=0.5" } });
    expect(html).toContain('<html lang="ru">');
  });

  it("let ?lang override Accept-Language", async () => {
    const { html } = await page(acmeDatabase(), "/?lang=en", { headers: { "accept-language": "ru" } });
    expect(html).toContain('<html lang="en">');
  });

  it("ignore an unknown lang", async () => {
    const { html } = await page(acmeDatabase(), "/?lang=xx");
    expect(html).toContain(`<html lang="${COMPONENTS.locale}">`);
  });
});

describe("public JSON", () => {
  it("returns the overall state and every component by id", async () => {
    const db = await ticked();
    const response = await handle(request("/api/status"), db, NOW);
    const body = (await response.json()) as { overall: string; components: { id: string; state: string }[] };
    expect(body.components.map((component) => component.id)).toEqual(
      COMPONENTS.components.map((component) => component.id),
    );
    expect(body.overall).toBe("no_data");
  });
});
