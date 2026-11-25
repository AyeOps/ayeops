import { handle } from "./app";
import { runMinute } from "./cron";

export interface Env {
  DB: D1Database;
  TELEGRAM_BOT_TOKEN?: string;
}

export default {
  async fetch(request, env): Promise<Response> {
    return handle(request, env.DB, Math.floor(Date.now() / 1000));
  },
  async scheduled(controller, env, ctx): Promise<void> {
    ctx.waitUntil(
      runMinute(
        env.DB,
        Math.floor(controller.scheduledTime / 1000),
        (url, init) => fetch(url, init),
        Date.now,
        env.TELEGRAM_BOT_TOKEN || null,
      ),
    );
  },
} satisfies ExportedHandler<Env>;
