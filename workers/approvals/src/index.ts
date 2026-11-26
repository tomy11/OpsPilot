/**
 * OpsPilot approvals on Cloudflare Workflows: one instance per approval request.
 *
 * The same steps as the Temporal ApprovalWorkflow (apps/agent/.../worker/workflows.py):
 * wait up to `timeoutHours` for a decision, then execute the approved ERP action with retries.
 * The Workflow holds only the durable state (waiting, timers, retries). Everything that touches
 * Postgres or the ERP is a callback to the agent (/internal/approvals/...), and every callback is
 * safe to repeat, because a step can run more than once.
 *
 * HTTP API for the agent (Bearer WORKER_SECRET):
 *   POST /approvals                      {approval_id, timeout_hours} -> 201 created, 200 existed
 *   POST /approvals/:id/decision         {approve, approver_id, reason} -> 202, 404 missing,
 *                                        409 finished (cannot take a decision any more)
 *   GET  /approvals/:id                  instance status, for debugging
 */
import { WorkflowEntrypoint, type WorkflowEvent, type WorkflowStep } from "cloudflare:workers";
import { NonRetryableError } from "cloudflare:workflows";

type Params = { approvalId: string; timeoutHours: number };
type Decision = { approve: boolean; approver_id: string; reason: string | null };
type Outcome = "executed" | "rejected" | "expired" | "failed";

const ID = /^[A-Za-z0-9_-]{1,64}$/;
const instanceId = (approvalId: string) => `approval-${approvalId}`;
// Still able to receive a decision. Anything else has finished or errored.
const LIVE = new Set(["queued", "running", "paused", "waiting", "waitingForPause"]);

const backoff =
  (firstSeconds: number, capSeconds: number) =>
  ({ ctx }: { ctx: { attempt: number } }) =>
    `${Math.min(capSeconds, firstSeconds * 2 ** (ctx.attempt - 1))} seconds` as const;

// ERP writes: 1 s doubling to 1 min, about 5 minutes in all, then the approval is marked failed.
// (Temporal tries 6 times in ~30 s; this is longer because an unreachable agent also counts.)
const EXECUTE = { retries: { limit: 10, delay: backoff(1, 60) }, timeout: "60 seconds" } as const;
// Bookkeeping must eventually land, so it keeps trying for about a day.
const RECORD = { retries: { limit: 300, delay: backoff(5, 300) }, timeout: "30 seconds" } as const;

class Unauthorized extends Error {}

export class ApprovalWorkflow extends WorkflowEntrypoint<Env, Params> {
  async run(event: Readonly<WorkflowEvent<Params>>, step: WorkflowStep): Promise<Outcome> {
    const { approvalId, timeoutHours } = event.payload;

    let decision: Decision | undefined;
    try {
      const received = await step.waitForEvent<Decision>("wait for decision", {
        type: "decision",
        timeout: `${timeoutHours} hours`,
      });
      decision = received.payload;
    } catch {
      // The only way this wait fails is the timeout. A decision may still have reached the
      // database just before it, so the agent answers the status it found.
      const { status } = await step.do("expire", RECORD, () =>
        this.callback<{ status: string }>(approvalId, "expire"),
      );
      if (status !== "approved") return status === "rejected" ? "rejected" : "expired";
    }
    if (decision && !decision.approve) return "rejected";

    try {
      await step.do("execute", EXECUTE, async () => {
        await this.callback(approvalId, "execute");
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await step.do("mark failed", RECORD, async () => {
        await this.callback(approvalId, "fail", { error: message.slice(0, 2000) });
      });
      return "failed";
    }
    return "executed";
  }

  private async callback<T = unknown>(approvalId: string, action: string, body: object = {}) {
    let response: Response;
    try {
      response = await fetch(`${this.env.AGENT_URL}/internal/approvals/${approvalId}/${action}`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.env.CALLBACK_SECRET}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      });
    } catch (error) {
      throw new Error(`agent unreachable: ${error}`); // retried
    }
    const text = await response.text();
    if (response.status === 422) throw new NonRetryableError(detail(text), "PermanentActionError");
    if (!response.ok) throw new Error(`agent ${action}: HTTP ${response.status} ${detail(text)}`);
    return JSON.parse(text) as T;
  }
}

function detail(text: string): string {
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
  } catch {
    // not JSON
  }
  return text.slice(0, 500);
}

function authorize(request: Request, env: Env): void {
  const given = new TextEncoder().encode(request.headers.get("Authorization") ?? "");
  const expected = new TextEncoder().encode(`Bearer ${env.WORKER_SECRET}`);
  if (!env.WORKER_SECRET || given.byteLength !== expected.byteLength) throw new Unauthorized();
  if (!crypto.subtle.timingSafeEqual(given, expected)) throw new Unauthorized();
}

const json = (body: unknown, status = 200) => Response.json(body, { status });

async function createInstance(request: Request, env: Env): Promise<Response> {
  const body = (await request.json().catch(() => null)) as {
    approval_id?: unknown;
    timeout_hours?: unknown;
  } | null;
  const approvalId = body?.approval_id;
  const hours = body?.timeout_hours;
  if (typeof approvalId !== "string" || !ID.test(approvalId)) return json({ detail: "bad id" }, 400);
  if (typeof hours !== "number" || !Number.isInteger(hours) || hours < 1 || hours > 8760)
    return json({ detail: "timeout_hours must be 1-8760" }, 400);

  const id = instanceId(approvalId);
  try {
    await env.APPROVALS.create({ id, params: { approvalId, timeoutHours: hours } });
  } catch (error) {
    // An earlier start succeeded before the agent saw the answer.
    if (error instanceof Error && error.message.includes("(instance.already_exists)"))
      return json({ id, existing: true });
    throw error;
  }
  return json({ id }, 201);
}

async function getInstance(env: Env, approvalId: string) {
  try {
    return await env.APPROVALS.get(instanceId(approvalId));
  } catch {
    return null; // get() throws for an unknown id
  }
}

async function sendDecision(request: Request, env: Env, approvalId: string): Promise<Response> {
  const body = (await request.json().catch(() => null)) as Partial<Decision> | null;
  if (typeof body?.approve !== "boolean" || typeof body.approver_id !== "string")
    return json({ detail: "approve and approver_id are required" }, 400);

  const instance = await getInstance(env, approvalId);
  if (!instance) return json({ detail: "workflow not found" }, 404);
  const { status } = await instance.status();
  if (!LIVE.has(status)) return json({ detail: `workflow is ${status}` }, 409);
  // Buffered if the instance has not reached waitForEvent yet; a second copy is never read.
  await instance.sendEvent({
    type: "decision",
    payload: { approve: body.approve, approver_id: body.approver_id, reason: body.reason ?? null },
  });
  return json({ id: instanceId(approvalId) }, 202);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      authorize(request, env);
    } catch {
      return json({ detail: "unauthorized" }, 401);
    }
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);
    if (parts[0] !== "approvals") return json({ detail: "not found" }, 404);

    if (parts.length === 1 && request.method === "POST") return createInstance(request, env);
    const approvalId = parts[1];
    if (!approvalId || !ID.test(approvalId)) return json({ detail: "not found" }, 404);
    if (parts.length === 3 && parts[2] === "decision" && request.method === "POST")
      return sendDecision(request, env, approvalId);
    if (parts.length === 2 && request.method === "GET") {
      const instance = await getInstance(env, approvalId);
      return instance ? json(await instance.status()) : json({ detail: "not found" }, 404);
    }
    return json({ detail: "not found" }, 404);
  },
} satisfies ExportedHandler<Env>;
