import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { MAX_COMPONENTS } from "../src/push";
import { STATES } from "../src/states";
import { COMPONENTS, CONTRACT, acmeDatabase } from "./helpers";

interface PushSchema {
  required: string[];
  properties: {
    tenant: { pattern: string };
    components: {
      maxItems: number;
      items: { required: string[]; properties: { id: { pattern: string }; state: { enum: string[] } } };
    };
  };
}

const schema = JSON.parse(readFileSync(join(CONTRACT, "push.schema.json"), "utf-8")) as PushSchema;

describe("push schema", () => {
  it("lists the same five states as the Worker", () => {
    expect(schema.properties.components.items.properties.state.enum).toEqual([...STATES]);
  });

  it("caps components at the Worker limit", () => {
    expect(schema.properties.components.maxItems).toBe(MAX_COMPONENTS);
  });
});

describe("seed from aye status", () => {
  it("loads into the migrated schema as the components document describes", () => {
    const db = acmeDatabase();
    const tenant = db.rows(
      "SELECT id, hostname, title, locale, notify_chat, notify_min, access_team, access_aud FROM tenants",
    );
    expect(tenant).toEqual([
      {
        id: COMPONENTS.tenant,
        hostname: COMPONENTS.hostname,
        title: COMPONENTS.title,
        locale: COMPONENTS.locale,
        notify_chat: COMPONENTS.notify?.telegram_chat_id,
        notify_min: COMPONENTS.notify?.min_state,
        access_team: COMPONENTS.access?.team,
        access_aud: COMPONENTS.access?.aud,
      },
    ]);
    const components = db.rows<{ id: string; title: string; push: number }>(
      "SELECT id, title, push FROM components ORDER BY position",
    );
    expect(components).toEqual(
      COMPONENTS.components.map((component) => ({
        id: component.id,
        title: component.title,
        push: component.push ? 1 : 0,
      })),
    );
    const probes = db.rows<{ component_id: string; url: string }>(
      "SELECT component_id, url FROM probes ORDER BY component_id, url",
    );
    expect(probes).toEqual(
      COMPONENTS.components
        .flatMap((component) => component.probes.map((url) => ({ component_id: component.id, url })))
        .sort((a, b) => a.component_id.localeCompare(b.component_id) || a.url.localeCompare(b.url)),
    );
  });

  it("drops the notices of components that left the manifest", () => {
    const db = acmeDatabase();
    db.exec("INSERT INTO notices (tenant_id, component_id, state, since) VALUES ('acme', 'gone', 'outage', 0)");
    db.exec("INSERT INTO notices (tenant_id, component_id, state, since) VALUES ('acme', 'docs', 'outage', 0)");
    db.raw.exec(readFileSync(join(CONTRACT, "fixtures", "acme.seed.sql"), "utf-8"));
    expect(db.rows("SELECT component_id FROM notices")).toEqual([{ component_id: "docs" }]);
  });

  it("can be applied twice", () => {
    const db = acmeDatabase();
    db.raw.exec(readFileSync(join(CONTRACT, "fixtures", "acme.seed.sql"), "utf-8"));
    expect(db.rows("SELECT id FROM components")).toHaveLength(COMPONENTS.components.length);
  });
});
