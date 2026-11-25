import { ACCESS_KEYS, type AccessKeys } from "./access";
import { handleAdmin } from "./admin";
import type { Database } from "./db";
import { json, problem } from "./http";
import { pickLocale } from "./i18n";
import { PAGE_HEADERS, renderPage } from "./page";
import { handlePush } from "./push";
import { tenantByHost } from "./tenant";
import { loadStatus, publicJson } from "./view";

export async function handle(
  request: Request,
  db: Database,
  now: number,
  keys: AccessKeys = ACCESS_KEYS,
): Promise<Response> {
  const url = new URL(request.url);
  const tenant = await tenantByHost(db, url.hostname);
  if (url.pathname === "/api/push") {
    if (request.method !== "POST") {
      return new Response(null, { status: 405, headers: { allow: "POST" } });
    }
    return handlePush(request, db, now, tenant);
  }
  if (tenant === null) {
    return new Response("unknown status page\n", {
      status: 404,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  }
  if (url.pathname.startsWith("/api/admin/")) {
    return handleAdmin(request, db, tenant, url.pathname, now, keys);
  }
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response(null, { status: 405, headers: { allow: "GET, HEAD" } });
  }
  if (url.pathname === "/api/status") {
    return json(200, publicJson(await loadStatus(db, tenant, now)), {
      "cache-control": "public, max-age=30",
      "access-control-allow-origin": "*",
    });
  }
  if (url.pathname === "/") {
    const locale = pickLocale(
      url.searchParams.get("lang"),
      request.headers.get("accept-language"),
      tenant.locale,
    );
    const html = renderPage(await loadStatus(db, tenant, now), { locale });
    return new Response(request.method === "HEAD" ? null : html, {
      headers: { ...PAGE_HEADERS, "content-language": locale },
    });
  }
  return problem(404, "not found");
}
