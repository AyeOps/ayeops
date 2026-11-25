import { describe, expect, it } from "vitest";
import { DEFAULT_LOCALE, LOCALES, LOCALE_ORDER, SOURCE_LOCALE, pickLocale, placeholders } from "../src/i18n";

const source = LOCALES[SOURCE_LOCALE];
if (source === undefined) {
  throw new Error("the source locale is missing");
}
const locales = Object.entries(LOCALES);

describe("locales", () => {
  it("offer exactly the languages the switcher shows", () => {
    expect(Object.keys(LOCALES).sort()).toEqual([...LOCALE_ORDER].sort());
  });

  it.each(locales)("%s has every key of the Ukrainian source and no other", (_, messages) => {
    expect(Object.keys(messages).sort()).toEqual(Object.keys(source).sort());
  });

  it.each(locales)("%s keeps the placeholders of every source string", (_, messages) => {
    for (const [key, text] of Object.entries(source)) {
      expect([key, placeholders(messages[key as keyof typeof messages])]).toEqual([key, placeholders(text)]);
    }
  });

  it.each(locales)("%s has no empty string", (_, messages) => {
    expect(Object.entries(messages).filter(([, text]) => text.trim() === "")).toEqual([]);
  });

  it.each(locales)("%s never mixes Latin and Cyrillic letters in one word", (_, messages) => {
    const mixed = Object.values(messages)
      .flatMap((text) => text.split(/[^\p{L}]+/u))
      .filter((word) => /[a-z]/i.test(word) && /\p{Script=Cyrillic}/u.test(word));
    expect(mixed).toEqual([]);
  });
});

describe("language choice", () => {
  it("prefers ?lang, then Accept-Language by weight, then the tenant locale", () => {
    expect(pickLocale("ru", "en", "uk")).toBe("ru");
    expect(pickLocale(null, "de-DE,en;q=0.4,ru;q=0.8", "uk")).toBe("ru");
    expect(pickLocale(null, "de", "en")).toBe("en");
    expect(pickLocale("xx", null, "zz")).toBe(DEFAULT_LOCALE);
    expect(DEFAULT_LOCALE).toBe("en");
  });
});
