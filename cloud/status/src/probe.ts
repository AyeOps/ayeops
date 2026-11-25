import { SLOW_PROBE_MS, type State } from "./states";

export const PROBE_TIMEOUT_MS = 10000;
export const USER_AGENT = "AyeOps-Status/0.1 (+https://ayeops.dev)";

export type Fetcher = (url: string, init: RequestInit) => Promise<Response>;

export interface ProbeResult {
  ok: boolean;
  latencyMs: number | null;
  status: number | null;
}

export async function probeOnce(
  url: string,
  fetcher: Fetcher,
  clock: () => number = Date.now,
): Promise<ProbeResult> {
  const started = clock();
  try {
    const response = await fetcher(url, {
      redirect: "follow",
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      headers: { "user-agent": USER_AGENT },
    });
    const latencyMs = clock() - started;
    await response.body?.cancel();
    return { ok: response.status >= 200 && response.status < 400, latencyMs, status: response.status };
  } catch {
    return { ok: false, latencyMs: null, status: null };
  }
}

export async function probe(
  url: string,
  fetcher: Fetcher,
  clock: () => number = Date.now,
): Promise<ProbeResult> {
  const first = await probeOnce(url, fetcher, clock);
  return first.ok ? first : probeOnce(url, fetcher, clock);
}

export function probeState(result: ProbeResult): State {
  if (!result.ok) {
    return "outage";
  }
  return result.latencyMs !== null && result.latencyMs > SLOW_PROBE_MS ? "degraded" : "operational";
}
