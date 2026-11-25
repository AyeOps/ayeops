import en from "./locales/en.json";
import ru from "./locales/ru.json";
import uk from "./locales/uk.json";

export type Messages = Record<keyof typeof uk, string>;
export type MessageKey = keyof Messages;

export const SOURCE_LOCALE = "uk";
export const DEFAULT_LOCALE = "en";
export const LOCALES: Record<string, Messages> = { uk, en, ru };
export const LOCALE_ORDER = ["en", "uk", "ru"] as const;

export function isLocale(value: string | null | undefined): value is string {
  return typeof value === "string" && Object.hasOwn(LOCALES, value);
}

export function pickLocale(
  requested: string | null,
  acceptLanguage: string | null,
  fallback: string,
): string {
  if (isLocale(requested)) {
    return requested;
  }
  const ranked = (acceptLanguage ?? "")
    .split(",")
    .map((part, index) => {
      const [tag = "", ...params] = part.trim().toLowerCase().split(";");
      const q = params.map((p) => p.trim()).find((p) => p.startsWith("q="));
      return { base: tag.split("-")[0] ?? "", q: q ? Number(q.slice(2)) : 1, index };
    })
    .filter((entry) => entry.base !== "" && entry.q > 0)
    .sort((a, b) => b.q - a.q || a.index - b.index);
  for (const entry of ranked) {
    if (isLocale(entry.base)) {
      return entry.base;
    }
  }
  return isLocale(fallback) ? fallback : DEFAULT_LOCALE;
}

export function translate(
  messages: Messages,
  key: MessageKey,
  values: Record<string, string | number> = {},
): string {
  return messages[key].replace(/\{([a-z_]+)\}/g, (match, name: string) =>
    Object.hasOwn(values, name) ? String(values[name]) : match,
  );
}

export function placeholders(text: string): string[] {
  return [...text.matchAll(/\{([a-z_]+)\}/g)].map((match) => match[1] ?? "").sort();
}
