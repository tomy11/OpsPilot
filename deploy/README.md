# Deploy OpsPilot staging

The gateway serves the Agent workspace and selected API routes on one origin. Postgres and
Temporal share a private Docker network with the application services. ERP uses a persistent
SQLite volume and new Agent runs receive isolated synthetic stock when staging mode is enabled.
For reviewer access, use the distinct `staff` and `manager` demo accounts. These are shared test
accounts for the Operations department. Give the credentials privately to the people testing
the demo. Use only synthetic records and uploads in this environment.

## Prepare the host

Prepare a VM with Docker Engine and Docker Compose 2.24.4+ (`!reset` support), Git, and a DNS
record pointing to the VM. Allow inbound TCP 80 and 443 for Caddy. Clone the release source
on the VM and place this configuration in a private `.env.staging` file with mode `0600`.

```dotenv
STAGING_SITE=ops-demo.example.org
JWT_SECRET=<random-hex-secret>
POSTGRES_PASSWORD=<random-hex-password>
DEMO_STAFF_PASSWORD=<different-random-password>
DEMO_MANAGER_PASSWORD=<different-random-password>
LLM_PROVIDER=fake
```

Generate each secret independently, for example with `openssl rand -hex 24`. Start with fake LLM
for the repeatable smoke test. For the separate Claude test, set `LLM_PROVIDER=anthropic`,
`ANTHROPIC_API_KEY` and `LLM_MODEL` to a model available to your account. Run the same case
and record the result as a live-model evaluation. Keep the repeatable fake-model test in CI
or a separate staging environment.

## Deploy and verify

From the repo root on the VM, tag the images from the release checkout.

```bash
export STAGING_REVISION="$(git rev-parse --short=12 HEAD)"
stage() {
  docker compose --env-file .env.staging -p opspilot-staging \
    -f docker-compose.yml -f compose.staging.yml "$@"
}
stage config --quiet
stage build agent knowledge docflow erp-mock
stage up -d --no-build --wait
```

At the website, sign in as Staff, run the sample, and open its approval. In a separate browser
session, sign in as Manager, open the request, and approve it. The transfer should move 7 units
from Bangna to Silom. The approval detail should show 43 and 10 on hand and the Activity tab
should show the request, approval and execution.

Open **Knowledge** as Staff and ask `ลาพักร้อนได้กี่วันต่อปี`. The answer links to the leave
policy excerpt. As the Ops Manager, ask `งบปรับเงินเดือนประจำปีกี่เปอร์เซ็นต์` and verify the
restricted HR document is absent from the answer and citations. The gateway forwards
`POST /knowledge/ask` to Knowledge, where the signed user identity controls document access.

Run the smoke test from outside the VM with both demo passwords in the shell environment.

```bash
python3 scripts/staging-smoke.py https://ops-demo.example.org
```

For a loopback-only rehearsal, use `STAGING_SITE=http://localhost`, `STAGING_BIND=127.0.0.1`,
`STAGING_HTTP_PORT=18080`, and `STAGING_HTTPS_PORT=18443`. The smoke URL is then
`http://localhost:18080`. Use a separate Compose project when rehearsing beside a deployment.

## Invite-only chat on Ollama Cloud

Add these to `.env.staging`. Only the Agent container receives the key; Knowledge and DocFlow
keep their own `LLM_PROVIDER`. In prod the Agent refuses to start with `ollama` unless credits
are on, and the shared demo accounts then cannot chat, so run the smoke test with the fake model.

```dotenv
AGENT_LLM_PROVIDER=ollama
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_API_KEY=<key from ollama.com/settings/keys>
OLLAMA_MODEL=glm-5.3-flash
CREDITS_ENABLED=true
CREDIT_MONTHLY_BUDGET_USD=20
```

To go through an Ollama server on the host instead, run `ollama signin` there, leave
`OLLAMA_BASE_URL` and `OLLAMA_API_KEY` unset and use `OLLAMA_MODEL=glm-5.3-flash:cloud`. The
container reaches it at `host.docker.internal:11434`. Every chat is then paid by the account
signed in on that server.

Accounts are managed from the host only. Credits are 0.001 USD of model usage each.

```bash
accounts() { stage exec agent python -m opspilot_agent.accounts_cli "$@"; }
accounts invite --label "Nok" --credits 500 --base-url https://ops-demo.example.org
accounts list
accounts grant invite-<id> --credits 200
accounts link invite-<id> --base-url https://ops-demo.example.org   # new device
accounts disable invite-<id>
```

The link works once, expires in 7 days (`--days`) and is stored only as a hash. Send it
privately: whoever opens it first gets the account. The browser then holds a 30-day session
cookie and renews a 15-minute access token from it. Disabling blocks new messages at once;
other read-only pages stop within 15 minutes.

Each message reserves up to 150 credits, is charged for the tokens the model reported, and gets
the rest back. A run stops before a call its reservation cannot cover. Calls that fail without
reporting usage are not charged to the user, so also watch usage in the Ollama account.
Prices come from a built-in list for the GLM models; any other model needs
`CREDIT_PRICE_INPUT_USD` and `CREDIT_PRICE_OUTPUT_USD` (and optionally
`CREDIT_PRICE_CACHED_USD`), or the Agent does not start.

## Search by meaning

Set `EMBEDDING_PROVIDER=ollama` after `ollama pull bge-m3` (1.2 GB) on the host. The Knowledge
container reaches it at `host.docker.internal:11434`; on Linux make Ollama listen on the Docker
bridge (`OLLAMA_HOST`) and keep port 11434 closed to the internet, because Ollama has no login.
On the next start every document is embedded again, before the service reports ready. The
service also starts and answers by words when Ollama is down. The database needs pgvector 0.8 or
newer, and the database user must be allowed to run `CREATE EXTENSION vector` once.

## Operations

Failed sign-ins are throttled per client IP (5 per 15 minutes) and per account (20 per 15
minutes), answering 429 with `Retry-After`. The counts live in the Agent process, which is why
the staging setup runs one Agent replica, and the client IP comes from Caddy's
`X-Forwarded-For` (`FORWARDED_ALLOW_IPS` is set because only the gateway reaches the Agent).
Tune with `LOGIN_MAX_FAILURES_PER_IP`, `LOGIN_MAX_FAILURES_PER_USER` and `LOGIN_WINDOW_S`.


Use `stage ps` and `stage logs --tail=100 agent worker temporal` when investigating. Keep the
Postgres and ERP volumes when redeploying. Back them up before a release that changes their
schema. To roll back a compatible release, set `STAGING_REVISION` to its retained image tag
and run `stage up -d --no-build --wait`. Use the earlier checkout as well when its proxy
or Compose configuration differs. The mock ERP staging config runs one service replica.
