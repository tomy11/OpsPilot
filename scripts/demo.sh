#!/usr/bin/env bash
# Happy-path demo against the running services (agent :8000, knowledge :8200, docflow :8300,
# ERP mock :8100).
# staff asks the agent -> agent proposes a stock transfer -> manager approves -> ERP updated.
set -euo pipefail
API=${API:-http://localhost:8000}
ERP=${ERP:-http://localhost:8100}
KB=${KB:-http://localhost:8200}
DF=${DF:-http://localhost:8300}
SAMPLES="$(cd "$(dirname "$0")/../apps/docflow/samples" && pwd)"

tok() {
  curl -sf "$API/auth/dev-token" -H 'content-type: application/json' \
    -d "{\"user_id\":\"$1\",\"role\":\"$2\",\"department\":\"ops\"}" |
    python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'
}
STAFF=$(tok s1 staff)
MANAGER=$(tok m1 manager)

echo "== 1. staff asks the agent"
RUN=$(curl -sf "$API/agent/runs" -H "authorization: Bearer $STAFF" -H 'content-type: application/json' \
  -d '{"message":"เช็ค ORD-1001 ว่าของพอไหม ถ้าไม่พอให้โอนสต็อกจาก bangna"}')
echo "$RUN" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["answer"]);[print(" -",a["id"],a["status"],a["summary"]) for a in d["approvals"]]'
AID=$(echo "$RUN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["approvals"][0]["id"])')

echo "== 2. staff tries to approve own request (expect 403)"
curl -s -o /dev/null -w '%{http_code}\n' "$API/approvals/$AID/decision" \
  -H "authorization: Bearer $STAFF" -H 'content-type: application/json' -d '{"approve":true}'

echo "== 3. manager approves"
curl -sf "$API/approvals/$AID/decision" -H "authorization: Bearer $MANAGER" \
  -H 'content-type: application/json' -d '{"approve":true,"reason":"ok"}' |
  python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"], d["result"] or d["error"] or "")'

echo "== 4. stock after (Temporal backend executes asynchronously; re-run if still pending)"
curl -sf "$ERP/inventory?sku=SKU-COFFEE"; echo

echo "== 5. audit trail"
curl -sf "$API/audit?target=$AID" -H "authorization: Bearer $MANAGER" |
  python3 -c 'import sys,json;[print(" -",a["created_at"][:19],a["actor"],a["action"],a["status"]) for a in reversed(json.load(sys.stdin))]'

echo "== 6. staff asks the knowledge base (answer cites its source)"
curl -sf "$KB/knowledge/ask" -H "authorization: Bearer $STAFF" -H 'content-type: application/json' \
  -d '{"question":"ลาพักร้อนได้กี่วันต่อปี"}' |
  python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["answer"]);[print("  [%d] %s > %s" % (c["n"], c["source"], c["section"])) for c in d["citations"]]'

show_doc() {
  python3 -c 'import sys,json;d=json.load(sys.stdin);print(" ", d["status"], d.get("erp_invoice_id") or "", "|", ", ".join(i["code"] for i in d["issues"]) or "no issues")'
}
up() { curl -sf "$DF/docflow/documents" -H "authorization: Bearer $STAFF" -F "file=@$SAMPLES/$1;type=text/plain"; }

echo "== 7. DocFlow: small clean tax invoice (Thai, 2,728.50 THB) -> posted automatically"
up th-dairy-small.txt | show_doc

echo "== 8. DocFlow: invoice with wrong line amount -> review queue, approval blocked"
BAD=$(up siam-coffee-bad-math.txt); echo "$BAD" | show_doc
BID=$(echo "$BAD" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -o /dev/null -w '  approve without fixing -> %{http_code}\n' "$DF/docflow/documents/$BID/decision" \
  -H "authorization: Bearer $MANAGER" -H 'content-type: application/json' -d '{"approve":true}'

echo "== 9. DocFlow: large clean invoice (9,630 THB) -> manager approves -> posted to ERP"
DOC=$(up siam-coffee-large.txt); echo "$DOC" | show_doc
DID=$(echo "$DOC" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -sf "$DF/docflow/documents/$DID/decision" -H "authorization: Bearer $MANAGER" \
  -H 'content-type: application/json' -d '{"approve":true,"reason":"matches PO-2001"}' | show_doc
