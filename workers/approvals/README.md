# OpsPilot approvals on Cloudflare Workflows

`APPROVAL_BACKEND=cloudflare` runs each approval as one Cloudflare Workflow instance instead of a
Temporal workflow. The steps are the same as `apps/agent/src/opspilot_agent/worker/workflows.py`:

1. `waitForEvent("decision")` for up to `APPROVAL_TIMEOUT_HOURS`. A decision sent before the
   instance reaches this step is buffered by Cloudflare.
2. Timeout: `expire` step. If the agent answers that a decision got there first and it was an
   approval, the workflow goes on to execute.
3. Rejected: done.
4. Approved: `execute` step, retried with backoff (1 s doubling, at most 1 min, 10 retries).
   An ERP refusal (HTTP 422 from the agent) stops the retries at once.
5. Still failing: `mark failed` step.

The Workflow holds only durable state. Every step is a call back to the agent
(`POST /internal/approvals/{id}/execute|expire|fail`), which owns Postgres and the ERP client and
answers the same way when a step is repeated. The agent still writes decisions to its outbox
first and delivers them here, so a Worker outage only delays a decision.

```
agent ──POST /approvals, /approvals/:id/decision (Bearer WORKER_SECRET)──▶ this Worker
agent ◀──POST /internal/approvals/:id/... (Bearer CALLBACK_SECRET)──────── Workflow steps
```

## Local

```bash
cd workers/approvals
npm ci
cp .dev.vars.example .dev.vars   # then put two random secrets in it
npm run dev                      # http://localhost:8787, state in .wrangler/
```

The agent needs the same two secrets:

```bash
APPROVAL_BACKEND=cloudflare
APPROVALS_WORKER_URL=http://localhost:8787          # docker: http://host.docker.internal:8787
APPROVALS_WORKER_SECRET=<WORKER_SECRET>
APPROVALS_CALLBACK_SECRET=<CALLBACK_SECRET>
```

`AGENT_URL` in `wrangler.jsonc` is where the Workflow calls back. Override it for a run with
`npx wrangler dev --var AGENT_URL:http://127.0.0.1:8000`. With local staging, the gateway at
`http://localhost:18090` already routes `/internal/approvals/*` to the agent.

`npm run typecheck` regenerates `worker-configuration.d.ts` from `wrangler.jsonc` and runs `tsc`.

## Deploy

Not done yet. The agent must be reachable from Cloudflare at `AGENT_URL`, for example through a
Cloudflare Tunnel to the gateway.

```bash
npx wrangler secret put WORKER_SECRET
npx wrangler secret put CALLBACK_SECRET
npx wrangler deploy --var AGENT_URL:https://<your agent host>
```

Then set `APPROVALS_WORKER_URL` on the agent to the deployed Worker URL.

## Not covered yet

- Cloudflare keeps finished instances for 7 days by default (30 at most); the audit log in
  Postgres stays the record.
- Instances still waiting when new code is deployed: check how Cloudflare versions running
  instances before changing the step names or order.
