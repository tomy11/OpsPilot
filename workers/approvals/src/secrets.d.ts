// Secrets are not in wrangler.jsonc, so `wrangler types` only sees them when a .dev.vars exists.
// Declaring them here keeps the types the same locally and in CI.
interface Env {
  WORKER_SECRET: string;
  CALLBACK_SECRET: string;
}
