export const STATES = ["operational", "degraded", "outage", "maintenance", "no_data"] as const;
export type State = (typeof STATES)[number];

export const SILENCE_SECONDS = 300;
export const STALE_SECONDS = 180;
export const SLOW_PROBE_MS = 3000;

export const SEVERITY: Record<State, number> = {
  operational: 0,
  maintenance: 0,
  no_data: 1,
  degraded: 2,
  outage: 3,
};

export function isState(value: unknown): value is State {
  return typeof value === "string" && (STATES as readonly string[]).includes(value);
}

export function worst(states: readonly State[]): State {
  let result: State | null = null;
  for (const state of states) {
    if (result === null || SEVERITY[state] > SEVERITY[result]) {
      result = state;
    }
  }
  return result ?? "no_data";
}

export interface Pushed {
  ts: number;
  state: State;
}

export function pushedState(pushed: Pushed | null, now: number): State {
  if (pushed === null || now - pushed.ts > SILENCE_SECONDS) {
    return "no_data";
  }
  return pushed.state;
}

export interface Sources {
  now: number;
  probes: readonly State[];
  pushed: Pushed | null;
  pushExpected: boolean;
  incident: State | null;
  maintenance: boolean;
}

export function componentState(sources: Sources): State {
  const pushed = pushedState(sources.pushed, sources.now);
  if (sources.maintenance || pushed === "maintenance") {
    return "maintenance";
  }
  const states = [...sources.probes];
  if (sources.pushExpected || pushed !== "no_data") {
    states.push(pushed);
  }
  if (sources.incident !== null) {
    states.push(sources.incident);
  }
  return worst(states);
}

export function overall(states: readonly State[]): State {
  for (const state of ["outage", "degraded", "maintenance", "no_data"] as const) {
    if (states.includes(state)) {
      return state;
    }
  }
  return states.length === 0 ? "no_data" : "operational";
}

export type Counts = Record<State, number>;

export function emptyCounts(): Counts {
  return { operational: 0, degraded: 0, outage: 0, maintenance: 0, no_data: 0 };
}

export function dayState(counts: Counts): State {
  if (counts.outage > 0) {
    return "outage";
  }
  if (counts.degraded > 0) {
    return "degraded";
  }
  if (counts.no_data > counts.operational + counts.maintenance) {
    return "no_data";
  }
  if (counts.maintenance > 0) {
    return "maintenance";
  }
  return counts.operational > 0 ? "operational" : "no_data";
}

export function uptime(counts: Counts): number | null {
  const known = counts.operational + counts.maintenance + counts.degraded + counts.outage;
  return known === 0 ? null : (known - counts.outage) / known;
}
