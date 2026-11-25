import { describe, expect, it } from "vitest";
import {
  SILENCE_SECONDS,
  componentState,
  dayState,
  emptyCounts,
  overall,
  uptime,
  type Sources,
} from "../src/states";

const NOW = 1_800_000_000;

function sources(overrides: Partial<Sources>): Sources {
  return {
    now: NOW,
    probes: [],
    pushed: null,
    pushExpected: true,
    incident: null,
    maintenance: false,
    ...overrides,
  };
}

describe("component state", () => {
  it("turns a pushed operational into no_data after more than five minutes of silence", () => {
    const pushed = { ts: NOW - SILENCE_SECONDS - 1, state: "operational" as const };
    expect(componentState(sources({ pushed }))).toBe("no_data");
  });

  it("still trusts a push that is exactly five minutes old", () => {
    const pushed = { ts: NOW - SILENCE_SECONDS, state: "operational" as const };
    expect(componentState(sources({ pushed }))).toBe("operational");
  });

  it("is no_data, never operational, when a push is expected and none ever came", () => {
    expect(componentState(sources({ probes: ["operational"] }))).toBe("no_data");
  });

  it("is no_data when nothing at all watches the component", () => {
    expect(componentState(sources({ pushExpected: false }))).toBe("no_data");
  });

  it("lets a probe-only component ignore a silent reporter", () => {
    expect(componentState(sources({ probes: ["operational"], pushExpected: false }))).toBe(
      "operational",
    );
  });

  it("still shows a fresh pushed outage on a probe-only component", () => {
    const pushed = { ts: NOW, state: "outage" as const };
    expect(
      componentState(sources({ probes: ["operational"], pushed, pushExpected: false })),
    ).toBe("outage");
  });

  it("takes the worst of its sources", () => {
    const pushed = { ts: NOW, state: "degraded" as const };
    expect(componentState(sources({ probes: ["operational", "outage"], pushed }))).toBe("outage");
    expect(componentState(sources({ probes: ["operational"], pushed }))).toBe("degraded");
  });

  it("shows maintenance during a window whatever the probes say", () => {
    expect(componentState(sources({ probes: ["outage"], maintenance: true }))).toBe("maintenance");
  });

  it("shows maintenance when the reporter pushes it", () => {
    const pushed = { ts: NOW, state: "maintenance" as const };
    expect(componentState(sources({ probes: ["outage"], pushed }))).toBe("maintenance");
  });

  it("raises the component to the impact of an open incident", () => {
    const pushed = { ts: NOW, state: "operational" as const };
    expect(componentState(sources({ pushed, incident: "degraded" }))).toBe("degraded");
  });
});

describe("overall banner", () => {
  it("ranks outage over degraded over maintenance over no_data over operational", () => {
    expect(overall(["operational", "no_data", "maintenance", "degraded", "outage"])).toBe("outage");
    expect(overall(["operational", "no_data", "maintenance", "degraded"])).toBe("degraded");
    expect(overall(["operational", "no_data", "maintenance"])).toBe("maintenance");
    expect(overall(["operational", "no_data"])).toBe("no_data");
    expect(overall(["operational"])).toBe("operational");
  });

  it("is no_data for a page without components", () => {
    expect(overall([])).toBe("no_data");
  });
});

describe("daily bars", () => {
  it("mark a day with a single outage minute as an outage day", () => {
    expect(dayState({ ...emptyCounts(), operational: 1439, outage: 1 })).toBe("outage");
  });

  it("mark a day that is mostly silent as no_data", () => {
    expect(dayState({ ...emptyCounts(), operational: 600, no_data: 840 })).toBe("no_data");
  });

  it("mark a day without minutes as no_data", () => {
    expect(dayState(emptyCounts())).toBe("no_data");
  });

  it("count degraded as up and leave minutes without data out of uptime", () => {
    const counts = { ...emptyCounts(), operational: 700, degraded: 100, outage: 200, no_data: 440 };
    expect(uptime(counts)).toBeCloseTo(0.8);
    expect(uptime({ ...emptyCounts(), no_data: 1440 })).toBeNull();
  });
});
