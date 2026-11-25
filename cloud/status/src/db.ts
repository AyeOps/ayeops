export interface Statement {
  bind(...values: unknown[]): Statement;
  all<T = Record<string, unknown>>(): Promise<{ results: T[] }>;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  run(): Promise<unknown>;
}

export interface Database {
  prepare(query: string): Statement;
  batch(statements: Statement[]): Promise<unknown[]>;
}
