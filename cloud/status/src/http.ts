export function json(status: number, data: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers },
  });
}

export function problem(status: number, error: string, details: string[] = []): Response {
  return json(status, details.length > 0 ? { error, details } : { error });
}

export async function readJson(request: Request, limit: number): Promise<unknown> {
  return parseJson(await readBody(request, limit));
}

export async function readBody(request: Request, limit: number): Promise<Uint8Array<ArrayBuffer>> {
  const declared = request.headers.get("content-length");
  if (declared !== null && Number(declared) > limit) {
    throw new BodyError(413, "body too large");
  }
  if (request.body === null) {
    return new Uint8Array(0);
  }
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel();
      throw new BodyError(413, "body too large");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

export function parseJson(bytes: Uint8Array): unknown {
  try {
    const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false });
    return JSON.parse(decoder.decode(bytes)) as unknown;
  } catch {
    throw new BodyError(400, "body is not JSON");
  }
}

export class BodyError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
