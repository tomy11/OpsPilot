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
should show the request, approval and execution. Run the smoke test from outside the VM after
injecting the two demo passwords into the test shell environment.

```bash
python3 scripts/staging-smoke.py https://ops-demo.example.org
```

For a loopback-only rehearsal, use `STAGING_SITE=http://localhost`, `STAGING_BIND=127.0.0.1`,
`STAGING_HTTP_PORT=18080`, and `STAGING_HTTPS_PORT=18443`. The smoke URL is then
`http://localhost:18080`. Use a separate Compose project when rehearsing beside a deployment.

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
