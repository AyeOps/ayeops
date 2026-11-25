import { LOCALES, LOCALE_ORDER, translate, type MessageKey, type Messages } from "./i18n";
import type { ComponentView, DayView, IncidentView, MaintenanceView, StatusView } from "./view";

export const PROJECT_URL = "https://github.com/AyeOps/ayeops";

const ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (char) => ESCAPES[char] ?? char);
}

const STYLE = `
:root{color-scheme:light dark;--bg:#f7f8fa;--card:#fff;--text:#16181d;--muted:#5d6472;--line:#e3e6eb;
--operational:#1f9d55;--degraded:#b87500;--outage:#d64545;--maintenance:#3b6fd8;--no_data:#a7adb8}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e8eaef;--muted:#9aa1ad;
--line:#262a33;--operational:#23935a;--degraded:#a86c00;--outage:#c94040;--maintenance:#3d6dd6;--no_data:#4a505c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;margin:0 0 16px;overflow-wrap:anywhere}
h2{font-size:1.05rem;margin:32px 0 12px}
section,.card{background:var(--card);border:1px solid var(--line);border-radius:12px}
.banner{padding:16px;font-size:1.25rem;font-weight:700;color:#fff;border:0}
.banner.s-no_data{color:var(--text)}
.s-operational{background:var(--operational)}.s-degraded{background:var(--degraded)}
.s-outage{background:var(--outage)}.s-maintenance{background:var(--maintenance)}.s-no_data{background:var(--no_data)}
ul{list-style:none;margin:0;padding:0}
.component{padding:16px;border-top:1px solid var(--line)}
.component:first-child{border-top:0}
.row{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.name{font-weight:600;overflow-wrap:anywhere}
.state{font-size:.875rem;white-space:nowrap}
.state::before{content:"";display:inline-block;width:.6em;height:.6em;border-radius:50%;margin-right:.4em;background:var(--dot)}
.t-operational{--dot:var(--operational)}.t-degraded{--dot:var(--degraded)}.t-outage{--dot:var(--outage)}
.t-maintenance{--dot:var(--maintenance)}.t-no_data{--dot:var(--no_data)}
.bars{display:flex;gap:1px;height:32px;margin-top:10px}
.bar{flex:1;min-width:0;border-radius:2px}
.axis{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:.75rem;margin-top:4px}
.card{padding:16px;margin-bottom:12px}
.card h3{margin:0 0 4px;font-size:1rem}
.meta{color:var(--muted);font-size:.875rem;margin:0}
.body{margin:8px 0 0;white-space:pre-line}
footer{margin-top:32px;color:var(--muted);font-size:.875rem;display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px}
footer a{color:inherit}
footer a[aria-current]{font-weight:600;text-decoration:none}
@media (max-width:480px){.bars{height:28px}main{padding-top:16px}}
`;

export interface PageContext {
  locale: string;
}

type Translator = (key: MessageKey, values?: Record<string, string | number>) => string;

function translator(messages: Messages): Translator {
  return (key, values = {}) => translate(messages, key, values);
}

export function formatTime(seconds: number, locale: string): string {
  const text = new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(seconds * 1000));
  return `${text} UTC`;
}

export function formatPercent(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Math.floor(value * 10000) / 10000);
}

function dayTitle(day: DayView, t: Translator, locale: string): string {
  if (day.uptime === null) {
    return t("day_empty", { day: day.day });
  }
  const summary = t("day_summary", {
    day: day.day,
    state: t(`state.${day.state}`),
    value: formatPercent(day.uptime, locale),
  });
  return day.counts.no_data > 0
    ? `${summary}; ${t("day_gap", { minutes: day.counts.no_data })}`
    : summary;
}

function totalUptime(component: ComponentView): number | null {
  let known = 0;
  let down = 0;
  for (const day of component.days) {
    const { operational, maintenance, degraded, outage } = day.counts;
    known += operational + maintenance + degraded + outage;
    down += outage;
  }
  return known === 0 ? null : (known - down) / known;
}

function renderComponent(component: ComponentView, t: Translator, locale: string): string {
  const bars = component.days
    .map(
      (day) =>
        `<span class="bar s-${day.state}" title="${escapeHtml(dayTitle(day, t, locale))}"></span>`,
    )
    .join("");
  const total = totalUptime(component);
  const uptimeText = total === null ? "" : t("uptime", { value: formatPercent(total, locale) });
  return [
    `<li class="component" id="c-${escapeHtml(component.id)}">`,
    `<div class="row"><span class="name">${escapeHtml(component.title)}</span>`,
    `<span class="state t-${component.state}">${escapeHtml(t(`state.${component.state}`))}</span></div>`,
    `<div class="bars" role="img" aria-label="${escapeHtml(t("history_label"))}">${bars}</div>`,
    `<div class="axis"><span>${escapeHtml(t("history_start"))}</span>`,
    `<span>${escapeHtml(uptimeText)}</span><span>${escapeHtml(t("history_end"))}</span></div>`,
    "</li>",
  ].join("");
}

function scopeText(components: string[], t: Translator): string {
  return t("affects", {
    components: components.length > 0 ? components.join(", ") : t("all_components"),
  });
}

function card(title: string, meta: string[], body: string): string {
  const lines = meta.map((line) => `<p class="meta">${escapeHtml(line)}</p>`).join("");
  const text = body ? `<p class="body">${escapeHtml(body)}</p>` : "";
  return `<article class="card"><h3>${escapeHtml(title)}</h3>${lines}${text}</article>`;
}

function renderIncident(incident: IncidentView, t: Translator, locale: string): string {
  const stateKey = `incident.${incident.state}`;
  const state = isMessageKey(stateKey) ? t(stateKey) : incident.state;
  const started = t("incident_started", { time: formatTime(incident.startedAt, locale) });
  return card(incident.title, [`${state} · ${started}`, scopeText(incident.components, t)], incident.body);
}

function renderMaintenance(window: MaintenanceView, t: Translator, locale: string): string {
  const span = t("maintenance_window", {
    start: formatTime(window.startsAt, locale),
    end: formatTime(window.endsAt, locale),
  });
  const when = window.active ? `${t("maintenance_now")} · ${span}` : span;
  return card(window.title, [when, scopeText(window.components, t)], window.body);
}

function isMessageKey(key: string): key is MessageKey {
  return Object.hasOwn(LOCALES.uk ?? {}, key);
}

function renderLanguages(current: string, t: Translator): string {
  const links = LOCALE_ORDER.map((locale) => {
    const name = escapeHtml(LOCALES[locale]?.language_name ?? locale);
    const mark = locale === current ? ' aria-current="true"' : "";
    return `<a href="?lang=${locale}" hreflang="${locale}" lang="${locale}"${mark}>${name}</a>`;
  }).join(" · ");
  return `<nav aria-label="${escapeHtml(t("languages"))}">${links}</nav>`;
}

function section(heading: string, items: string[]): string {
  return items.length > 0 ? `<h2>${escapeHtml(heading)}</h2>${items.join("")}` : "";
}

export function renderPage(view: StatusView, context: PageContext): string {
  const messages = LOCALES[context.locale] ?? LOCALES.en;
  if (messages === undefined) {
    throw new Error(`locale ${context.locale} is missing`);
  }
  const t = translator(messages);
  const locale = context.locale;
  const updated =
    view.updatedAt === null ? t("updated_never") : t("updated", { time: formatTime(view.updatedAt, locale) });
  const components = view.components.map((component) => renderComponent(component, t, locale));
  return [
    "<!doctype html>",
    `<html lang="${escapeHtml(locale)}">`,
    "<head>",
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width,initial-scale=1">',
    '<meta name="color-scheme" content="light dark">',
    `<title>${escapeHtml(t("page_title", { title: view.tenant.title }))}</title>`,
    `<style>${STYLE}</style>`,
    "</head>",
    "<body>",
    "<main>",
    `<h1>${escapeHtml(view.tenant.title)}</h1>`,
    `<section class="banner s-${view.overall}" role="status">${escapeHtml(t(`overall.${view.overall}`))}</section>`,
    section(t("incidents"), view.incidents.map((incident) => renderIncident(incident, t, locale))),
    section(t("maintenance"), view.maintenance.map((window) => renderMaintenance(window, t, locale))),
    `<h2>${escapeHtml(t("components"))}</h2>`,
    `<section><ul>${components.join("")}</ul></section>`,
    `<footer><span>${escapeHtml(updated)}</span>${renderLanguages(locale, t)}`,
    `<a href="${PROJECT_URL}" rel="noopener noreferrer">${escapeHtml(t("powered_by"))}</a></footer>`,
    "</main>",
    "</body>",
    "</html>",
    "",
  ].join("\n");
}

export const PAGE_HEADERS: Record<string, string> = {
  "content-type": "text/html; charset=utf-8",
  "content-security-policy":
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "cache-control": "public, max-age=30",
  vary: "Accept-Language",
};
