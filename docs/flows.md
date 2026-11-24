# OpsPilot: flow การทำงานของแต่ละ service

> แผนภาพ mermaid ทั้งหมดอ้างอิงโค้ดปัจจุบันใน repo GitHub แสดงผลให้อัตโนมัติ แก้โค้ดที่เปลี่ยน flow แล้วควรแก้ไฟล์นี้ด้วย

แพลตฟอร์ม AI สำหรับงานปฏิบัติการ 3 โมดูลบนแกนกลางเดียวกัน แต่ละ service คุยกันผ่าน HTTP เท่านั้น LLM ทำหน้าที่อ่านและเสนอ ส่วนโค้ดเป็นคนตัดสินและลงมือทำ

## สถาปัตยกรรมรวม

uv workspace มี 5 package แต่ละ app ห้าม import กันเอง (ruff `TID251`) ต้องเรียกผ่าน API ทุก service ตรวจ JWT ด้วย secret ชุดเดียวกัน

*service, port และทิศทางการเรียก*

```mermaid
flowchart LR
  user(["ผู้ใช้<br/>Staff / Manager / Admin"])
  subgraph pkg["packages/core · opspilot-core"]
    core["config · JWT auth · audit + PII mask<br/>Claude client · /health /ready /auth /audit"]
  end
  subgraph apps["apps/*"]
    agent["opspilot-agent :8000<br/>LangGraph agent · approvals · web UI"]
    worker["agent worker<br/>Temporal activities"]
    kb["opspilot-knowledge :8200<br/>permission-aware RAG"]
    df["opspilot-docflow :8300<br/>invoice extraction + review"]
    erp["opspilot-erp-mock :8100<br/>orders · stock · vendors · PO · AP"]
  end
  pg[("Postgres :5432")]
  tmp["Temporal :7233"]
  claude["Claude API<br/>claude-opus-5-5"]
  user -->|"browser / REST"| agent
  user -->|REST| kb
  user -->|"REST + upload"| df
  agent -->|"POST /knowledge/search<br/>token ในนามผู้ใช้"| kb
  agent -->|"REST + Idempotency-Key<br/>+ X-OpsPilot-Sandbox"| erp
  df -->|"vendors · PO · AP invoices"| erp
  agent --> tmp
  worker --> tmp
  worker -->|"execute write"| erp
  agent --- pg
  worker --- pg
  kb --- pg
  df --- pg
  agent -.->|"tool use"| claude
  kb -.->|"grounded answer"| claude
  df -.->|"structured output"| claude
  agent -.->|uses| core
  kb -.->|uses| core
  df -.->|uses| core
```

- โหมด offline: `LLM_PROVIDER=fake` ใช้ตัววางแผนแบบกฎตายตัว, `APPROVAL_BACKEND=inline` ไม่ต้องมี Temporal, ใช้ SQLite แทน Postgres ได้
- ทุก service ใช้ฐานข้อมูล Postgres ก้อนเดียวกันแต่ตารางแยกกัน (`create_all` ตอนเริ่ม ยังไม่มี Alembic)

## ตัวตนและ token

มี 3 ทางที่จะได้ token: dev-token (ปิดใน prod), login บัญชี demo (staging) และ token ในนามผู้ใช้ที่ Agent ออกเองตอนค้น Knowledge

*sequence · การออกและตรวจ token*

```mermaid
sequenceDiagram
  autonumber
  actor U as ผู้ใช้
  participant G as Caddy gateway
  participant A as Agent API
  participant L as LoginLimiter
  participant K as Knowledge API
  alt dev / test
    U->>A: POST /auth/dev-token {user_id, role, department}
    A-->>U: JWT HS256 (TTL 60 นาที)
  else staging (APP_ENV=prod)
    U->>G: POST /session/login {staff|manager, password}
    G->>A: ส่งต่อ + X-Forwarded-For
    A->>L: retry_after(ip, username)
    alt เกินโควต้า (5 ครั้ง/IP หรือ 20 ครั้ง/บัญชี ใน 15 นาที)
      A-->>U: 429 + Retry-After
    else
      A->>A: compare_digest(password)
      alt ผิด
        A->>L: failed(ip, username)
        A-->>U: 401
      else ถูก
        A->>L: succeeded(ip)
        A-->>U: JWT (demo-staff / demo-manager, แผนก ops)
      end
    end
  end
  U->>A: POST /agent/runs + Bearer
  A->>A: decode_token → User(id, role, department)
  A->>K: POST /knowledge/search + token ใหม่ในนามผู้ใช้ (TTL 2 นาที)
  K->>K: decode_token → ใช้สิทธิ์ของผู้ใช้คนเดิม
```

- ตอนเริ่มใน prod Agent บังคับให้ `JWT_SECRET` ยาวอย่างน้อย 32 bytes และรหัสผ่าน demo ยาวอย่างน้อย 16 ตัวและไม่ซ้ำกัน
- Agent ค้น Knowledge ด้วยตัวตนของผู้ใช้ ไม่ใช่ของตัวเอง จึงไม่มีทางเห็นเอกสารที่ผู้ใช้เปิดเองไม่ได้

---

# OpsPilot Agent · `:8000`

## Agent run: จากคำสั่งถึงคำขออนุมัติ

ตัวอย่าง: Staff สั่ง “เช็ค ORD-1001 ถ้าของไม่พอให้โอนจาก bangna” Agent วนเรียกเครื่องมืออ่านข้อมูล แล้วสร้างคำขออนุมัติแทนการเขียน ERP เอง

*sequence · POST /agent/runs*

```mermaid
sequenceDiagram
  autonumber
  actor S as Staff
  participant API as Agent API
  participant DB as Postgres
  participant G as LangGraph
  participant LLM as Claude / FakeLLM
  participant T as Tools node
  participant K as Knowledge
  participant E as ERP mock
  S->>API: POST /agent/runs {message}
  API->>DB: insert AgentRun (+ RunSandbox ถ้า ERP_ISOLATE_RUNS)
  API->>G: run_agent(message) ภายใต้ erp.sandbox(run_id)
  loop จนกว่าโมเดลจะหยุดเรียกเครื่องมือ (สูงสุด agent_max_steps = 8)
    G->>LLM: system prompt + messages + tool schemas (strict)
    LLM-->>G: tool_use blocks
    G->>T: ตรวจ args + นโยบายสิทธิ์ทีละ call
    T->>K: search_knowledge (SOP การโอนสต็อก)
    T->>E: get_order ORD-1001 · check_stock silom
    Note over T: พบ SKU-COFFEE ขาด 7
    T->>DB: create_stock_transfer → ApprovalRequest (pending)
    T-->>G: tool_result {pending_approval, approval_id}
  end
  LLM-->>G: คำตอบสรุปภาษาไทย
  API->>DB: บันทึก answer, steps, audit tool.* ทุกขั้น แล้ว commit
  API-->>S: run_id, answer, approvals[], steps[]
  Note over API,DB: ไม่ได้เรียก Temporal ตรงนี้ outbox จะ start workflow ให้เอง
```

## แชตสำหรับผู้ได้รับเชิญและเครดิต

ใช้เมื่อ `CREDITS_ENABLED=true` บัญชี demo ที่ใช้ร่วมกันจะส่งข้อความไม่ได้ในโหมดนี้

```mermaid
sequenceDiagram
  autonumber
  actor O as ผู้ดูแล
  actor U as ผู้ได้รับเชิญ
  participant API as Agent API
  participant DB as Postgres
  participant LLM as Ollama Cloud (GLM)
  O->>DB: accounts_cli invite สร้างบัญชี เครดิต และ hash ของลิงก์
  O-->>U: ส่งลิงก์ #invite=... แบบส่วนตัว
  U->>API: กดเริ่มใช้งาน POST /session/redeem
  API->>DB: ใช้ลิงก์ได้ครั้งเดียว (conditional UPDATE)
  API-->>U: cookie 30 วัน (HttpOnly) และ JWT 15 นาที
  U->>API: POST /agent/runs
  API->>DB: จองเครดิต ตรวจบัญชี ยอด จำนวนต่อวัน และงบเดือน
  alt ไม่ผ่าน
    API-->>U: 402 / 403 / 409 / 429 / 503 โดยไม่เรียกโมเดล
  else ผ่าน
    loop แต่ละรอบของ Agent
      API->>API: กรณีแพงสุดของรอบนี้เกินยอดจอง ให้หยุด
      API->>LLM: POST /v1/messages
      LLM-->>API: คำตอบและ usage
      API->>DB: บันทึกยอดที่ใช้แล้ว
    end
    API->>DB: คิดตาม token จริง คืนส่วนที่เหลือ บันทึก ledger
    API-->>U: คำตอบ เครดิตที่ใช้ และยอดคงเหลือ
  end
```

## Tools node: จุดที่โค้ดคุมโมเดล

*flowchart · การตัดสินใจต่อ 1 tool call*

```mermaid
flowchart TD
  c["tool_use จากโมเดล"] --> k{"รู้จักเครื่องมือนี้?"}
  k -- ไม่ --> e1["is_error: unknown tool"]
  k -- ใช่ --> v{"Pydantic ผ่าน?<br/>extra=forbid"}
  v -- ไม่ --> e2["is_error: invalid arguments"]
  v -- ผ่าน --> p{"authorize_tool(user)"}
  p -- DENY --> e3["is_error: permission denied"]
  p -- READ --> r["รันทันที<br/>get_order · check_stock · search_knowledge"]
  p -- WRITE --> w["propose() สร้าง ApprovalRequest<br/>ไม่เขียน ERP"]
  r --> out["tool_result กลับเข้า loop"]
  w --> out
  e1 --> out
  e2 --> out
  e3 --> out
```

| เครื่องมือ | ประเภท | role ขั้นต่ำ |
|---|---|---|
| `get_order` | READ | staff |
| `check_stock` | READ | staff |
| `search_knowledge` | READ (ผ่าน HTTP) | staff |
| `create_stock_transfer` | WRITE → อนุมัติ | staff |
| `update_order_status` | WRITE → อนุมัติ | staff |
| `adjust_inventory` | WRITE → อนุมัติ | admin |

- แม้ admin เรียก WRITE ก็ยังต้องผ่านการอนุมัติ ไม่มีใครเขียน ERP ตรงจาก agent ได้
- ถ้า Knowledge ล่ม `search_knowledge` คืนผลว่าง พร้อม error แต่ run ยังทำต่อจนจบ

## สถานะคำขออนุมัติ

ทุกการเปลี่ยนสถานะเป็น conditional UPDATE บนสถานะเดิม กดซ้ำ, signal ซ้ำ หรือ activity ที่ retry จึงเปลี่ยนสถานะเดิมซ้ำไม่ได้ (ได้ 409)

*state · ApprovalRequest.status*

```mermaid
stateDiagram-v2
  [*] --> pending: agent propose()
  pending --> approved: manager แผนกเดียวกัน / admin, ไม่ใช่ผู้ขอ
  pending --> rejected: ปฏิเสธ
  pending --> expired: ครบ 24 ชม. ไม่มีใครตัดสิน
  pending --> expired: start workflow ไม่ได้จนเลยกำหนด
  approved --> executed: ERP ตอบสำเร็จ (Idempotency-Key = approval id)
  approved --> failed: ERP 4xx หรือ retry ครบ 6 ครั้ง
  approved --> failed: workflow หาย (NOT_FOUND)
  executed --> [*]
  rejected --> [*]
  expired --> [*]
  failed --> [*]
```

## Outbox และ Temporal workflow

API เขียนแค่ Postgres ส่วน loop `run_delivery` ในแต่ละ API replica เป็นคนคุยกับ Temporal ใช้ `FOR UPDATE SKIP LOCKED` จึงรันหลาย replica ได้

*sequence · การ start, การตัดสินใจ และการลงมือทำ*

```mermaid
sequenceDiagram
  autonumber
  actor M as Manager
  participant API as Agent API
  participant DB as Postgres
  participant D as run_delivery (ทุก 1 วินาที)
  participant WF as ApprovalWorkflow
  participant W as Worker activities
  participant E as ERP mock
  D->>DB: เลือก pending ที่ยังไม่มี workflow_id
  alt อายุเกิน approval timeout
    D->>DB: pending → expired + audit
  else
    D->>WF: start approval-{id} (REJECT_DUPLICATE)
    D->>DB: บันทึก workflow_id
  end
  Note over D,WF: Temporal ล่ม: เว้นช่วงรอบหน้า 2, 4, 8 ... สูงสุด 30 วินาที
  M->>API: POST /approvals/{id}/decision
  API->>API: can_decide() ไม่ผ่าน → 403 + audit
  API->>API: ยังไม่มี workflow_id → 409 "workflow is not ready"
  API->>DB: transaction เดียว: pending → approved, audit, insert ApprovalDelivery
  API-->>M: status approved
  D->>DB: เลือก delivery ที่ถึงเวลา
  D->>WF: signal decide(approve, approver, reason)
  alt สำเร็จ
    D->>DB: ลบ delivery
  else error ชั่วคราว
    D->>DB: next_attempt = now + 10% ของอายุการตัดสินใจ (5 วินาที–5 นาที)
  else NOT_FOUND และสถานะยัง approved
    D->>DB: approved → failed + audit แล้วลบ delivery
  end
  WF->>W: approval.execute (retry 1s×2 สูงสุด 6 ครั้ง)
  W->>E: POST /stock-transfers + Idempotency-Key + Sandbox
  E-->>W: TRF-… completed
  W->>DB: approved → executed + audit approval.executed
  opt ไม่มี signal ภายใน 24 ชม.
    WF->>W: approval.expire
    W->>DB: อ่านสถานะจริง: approved จะลงมือทำต่อ, pending จะเป็น expired
  end
```

> กรณีอนุมัติแล้วแต่ signal ส่งไม่ถึงจนครบ 24 ชั่วโมง workflow จะอ่านการตัดสินใจจากฐานข้อมูลตอนหมดเวลา แล้วลงมือทำต่อ การตัดสินใจที่ commit แล้วจึงไม่หาย

---

# OpsPilot Knowledge · `:8200`

## นำเข้าเอกสาร

*flowchart · POST /knowledge/documents และ seed ตอนเริ่ม*

```mermaid
flowchart TD
  in["Markdown + departments + min_role<br/>(หรือ seed/*.md พร้อม front-matter)"] --> who{"ผู้เผยแพร่"}
  who -- staff --> x1["403"]
  who -- "manager แผนกอื่น หรือ '*'" --> x2["403 เผยแพร่ได้เฉพาะแผนกตัวเอง"]
  who -- "manager แผนกตัวเอง / admin" --> h["sha256(title, content, ACL)"]
  h --> same{"ตรงกับ version ที่ active?"}
  same -- ใช่ --> nochange["created=false ไม่ทำอะไร"]
  same -- ไม่ --> chunk["chunk ตามหัวข้อ<br/>เก็บ path หัวข้อไว้อ้างอิง ≤ 800 ตัวอักษร"]
  chunk --> ver["version เดิม active=false ลบ vector เดิม<br/>สร้าง version n+1"]
  ver --> acl["insert knowledge_acl และ token ของคำ"]
  acl --> aud["audit knowledge.document.ingested"]
  aud --> emb{"โมเดล embedding ตอบไหม?"}
  emb -- ตอบ --> vec["เก็บ vector พร้อมชื่อโมเดล<br/>hash 512 มิติ / bge-m3 1024 มิติ / Voyage"]
  emb -- ไม่ตอบ --> wait["indexed=false ค้นด้วยคำได้ก่อน<br/>ลองใหม่ทุก 30 วินาที"]
  wait --> emb
```

## ค้นหาและตอบคำถาม

กรองสิทธิ์ใน SQL ก่อนให้คะแนน chunk ที่ผู้ใช้อ่านไม่ได้จึงไม่เข้าไปในการจัดอันดับ, prompt หรือการอ้างอิงเลย

*flowchart · POST /knowledge/ask*

```mermaid
flowchart TD
  q["คำถาม + User(role, department)"] --> f["SQL WHERE<br/>active AND min_role_rank ≤ role<br/>AND ACL ∈ {แผนกผู้ใช้, '*'}"]
  f --> tok["tokenize: ไทยเป็น bigram<br/>อังกฤษตัด stopword"]
  tok --> bm["BM25 + coverage ของคำในคำถาม"]
  f --> cos["vector: pgvector HNSW บน Postgres<br/>หรือคิดใน Python บน SQLite"]
  bm --> gate{"coverage ≥ 0.5<br/>หรือ vector ถึงเกณฑ์ของโมเดล?<br/>hash 0.3 · bge-m3 0.52"}
  cos --> gate
  gate -- "ไม่มี chunk ผ่าน" --> nf["ไม่พบข้อมูลที่ยืนยันได้"]
  gate -- ผ่าน --> rrf["RRF fusion (k=60) → top 5"]
  rrf --> pr["prompt: source id=n ครอบด้วย tag<br/>escape HTML · บอกว่าเป็นข้อมูล ไม่ใช่คำสั่ง"]
  pr --> llm["Claude: ต้องอ้าง [n] ทุกประโยค<br/>หรือตอบ NO_ANSWER"]
  llm --> ver{"มีการอ้างอิงที่ชี้ไปยัง source ที่ส่งจริง?"}
  ver -- ไม่ --> nf
  ver -- ใช่ --> ans["คำตอบ + citations<br/>(ตัด [n] ที่ไม่มีจริงทิ้ง)"]
  ans --> aud["audit knowledge.ask<br/>retrieved / cited ids · PII masked"]
  nf --> aud
```

- admin ไม่ได้สิทธิ์อ่านทุกเอกสาร แต่ลบ (deactivate) เอกสารได้ทุกตัว
- ลบเอกสารที่ไม่มีสิทธิ์อ่านจะได้ 404 ไม่ใช่ 403 คนนอกจึงเดาไม่ได้ว่ามีเอกสารนั้นอยู่
- eval 29 เคส (hash): hit@3 0.95 · ปฏิเสธถูก 1.00 · สิทธิ์รั่ว 0 (CI ตกถ้ามีการรั่ว)
- คำถามที่ไม่มีคำตรงกับเอกสาร 13 ข้อ: hash เจอ 0 · bge-m3 เจอ 12

---

# OpsPilot DocFlow · `:8300`

## วงจรเอกสาร: อัปโหลดถึงลงบัญชี

Claude ทำหน้าที่ถอดข้อความเท่านั้น ห้ามคำนวณหรือแก้ตัวเลข ยอดที่ผิดในกระดาษจึงยังผิดอยู่ให้ validator จับ

*sequence · POST /docflow/documents*

```mermaid
sequenceDiagram
  autonumber
  actor S as Staff
  participant API as DocFlow API
  participant X as Extractor
  participant V as validate()
  participant E as ERP mock
  participant DB as Postgres
  S->>API: upload PDF / รูป / text (≤ 10 MB)
  API->>DB: insert เอกสาร + audit docflow.uploaded
  API->>X: extract (Claude structured output หรือ offline text)
  alt ถอดไม่ได้
    API->>DB: extraction_failed (ผู้ตรวจพิมพ์ข้อมูลเองได้)
  else
    API->>E: GET vendor · PO · AP invoices ที่เลขซ้ำ
    API->>DB: หาเอกสารเลขซ้ำที่ยังค้างใน DocFlow
    API->>V: extraction + ข้อมูลจาก ERP
    V-->>API: issues[] (error / warning)
    API->>DB: needs_review + audit docflow.extracted
    opt ไม่มี issue เลย และยอด ≤ 5,000 บาท
      API->>DB: needs_review → approved (system)
      API->>E: POST /ap-invoices Idempotency-Key = document id
      API->>DB: approved → posted / post_failed
    end
  end
  API-->>S: สถานะ + issues
```

*state · DocflowDocument.status*

```mermaid
stateDiagram-v2
  [*] --> extraction_failed: ถอดข้อความไม่ได้
  [*] --> needs_review: ถอดได้
  extraction_failed --> needs_review: ผู้ตรวจกรอกข้อมูล แล้วตรวจใหม่
  needs_review --> needs_review: ผู้ตรวจแก้ field แล้วตรวจใหม่ทั้งหมด
  needs_review --> approved: อัตโนมัติ หรือผู้ตรวจอนุมัติ (ต้องไม่มี error)
  needs_review --> rejected: ปฏิเสธ
  extraction_failed --> rejected: ปฏิเสธ
  approved --> posted: ERP ตอบ 201
  approved --> post_failed: ERP ล่ม / 409
  post_failed --> approved: retry-post
  posted --> [*]
  rejected --> [*]
```

- ผู้ตรวจต้องเป็น manager แผนกเดียวกันหรือ admin และไม่ใช่คนอัปโหลด (กฎเดียวกับ Agent อยู่ใน `approval_denied_reason`)
- ถ้า ERP ล่มตอนตรวจ issue จะเป็น `*_unchecked` แบบ warning ไม่บอกว่าผู้ขายไม่มีอยู่จริง และเอกสารนั้นจะไม่ผ่านอัตโนมัติ
- การแก้ของผู้ตรวจบันทึกค่าก่อนและหลังทีละ field ใน audit และเก็บผลจากโมเดลตัวแรกไว้ใช้เป็นข้อมูล eval

## กฎตรวจสอบใน validate()

| กลุ่ม | code | ระดับ | เงื่อนไข |
|---|---|---|---|
| ประเภท | `not_an_invoice` | **error** | doc_type เป็น other |
| ข้อมูลครบ | `missing_field` | **error** | ไม่มีเลขผู้เสียภาษี, เลขเอกสาร, วันที่, ยอดรวม หรือรายการ |
| ผู้ขาย | `tax_id_invalid` | **error** | ไม่ใช่ 13 หลัก หรือ checksum mod-11 ไม่ผ่าน |
|  | `vendor_unknown` | **error** | ไม่มีในทะเบียนผู้ขายของ ERP |
|  | `vendor_name_mismatch` | **warning** | ชื่อไม่ตรงหลังตัดคำว่า บริษัท / จำกัด / Co., Ltd. |
| ตัวเลข | `line_amount_mismatch` | **error** | qty × unit_price ≠ amount (คลาดได้ 0.01) |
|  | `subtotal_mismatch` | **error** | ผลรวมรายการ ≠ subtotal |
|  | `vat_mismatch` | **error** | VAT ≠ subtotal × 7% |
|  | `total_mismatch` | **error** | subtotal + VAT ≠ total |
| วันที่ | `date_invalid` · `date_in_future` · `due_before_invoice` | **error** | รูปแบบผิด, เป็นวันในอนาคต, ครบกำหนดก่อนวันออกเอกสาร (แปลง พ.ศ. แล้ว) |
| ซ้ำ | `duplicate_invoice` | **error** | เลขเดียวกันของผู้ขายเดียวกันอยู่ใน ERP หรือค้างอยู่ใน DocFlow |
| PO | `po_not_found` · `po_vendor_mismatch` · `po_not_open` | **error** | ไม่มี PO, PO เป็นของผู้ขายอื่น, PO ไม่ได้อยู่สถานะ open |
|  | `po_amount_mismatch` · `no_po` | **warning** | ยอดต่างจาก PO เกิน 2%, ไม่มีเลข PO |
| ERP ล่ม | `vendor_unchecked` · `po_unchecked` · `duplicate_unchecked` | **warning** | เรียก ERP ไม่ได้ชั่วคราว |

**ผ่านอัตโนมัติ** เฉพาะเอกสารที่ไม่มี issue เลย และยอดไม่เกิน `AUTO_POST_LIMIT` (5,000 บาท) ถ้ายังมี error อยู่ ผู้ตรวจกดอนุมัติจะได้ 422

---

# โครงสร้างพื้นฐาน

## ERP mock: idempotency และ sandbox ต่อ run

*flowchart · 1 request เข้า ERP mock โหมด staging (ERP_STATE_PATH)*

```mermaid
flowchart TD
  r["HTTP request"] --> ch{"chaos<br/>CHAOS_ERROR_RATE"}
  ch -- สุ่มโดน --> e503["503"]
  ch -- ผ่าน --> sb{"X-OpsPilot-Sandbox"}
  sb -- "ไม่มี" --> sc0["scope หลัก ''"]
  sb -- "32 hex (run id)" --> sc1["scope ของ run นั้น"]
  sb -- "รูปแบบผิด" --> e400["400"]
  sc0 --> load["โหลด Store ของ scope จาก SQLite<br/>ไม่มี = seed ใหม่ (silom 3 · bangna 50)"]
  sc1 --> load
  load --> w{"เป็น write?"}
  w -- "GET" --> handle["handler"]
  w -- "POST / PATCH" --> lock["asyncio.Lock + BEGIN IMMEDIATE"]
  lock --> idem{"Idempotency-Key เคยใช้?"}
  idem -- ใช่ --> replay["คืนผลเดิม ไม่เขียนซ้ำ"]
  idem -- ไม่ --> handle
  handle --> save["status ต่ำกว่า 400: บันทึก Store กลับ SQLite"]
```

- ทุก run ของ Agent เริ่มจากสต็อกตั้งต้นเดิม demo จึงรันซ้ำได้เสมอ และ scope หลักไม่ถูกแตะ
- header sandbox ไหลตาม ContextVar ทั้งตอน agent run, ตอน worker ลงมือทำ และใน `/approvals/{id}/inventory`

## Staging: สิ่งที่เปิดสู่ภายนอก

*flowchart · docker-compose.yml + compose.staging.yml*

```mermaid
flowchart LR
  net(["Internet :80 / :443"]) --> caddy["Caddy gateway<br/>HTTPS · CSP · no-store"]
  caddy -->|"/ · /assets/* · /session/* · /agent/runs<br/>/approvals* · /audit · /ready"| agent["agent :8000<br/>React UI + API"]
  caddy -->|"อย่างอื่นทั้งหมด"| nf["404"]
  subgraph private["Docker network (ไม่ publish port)"]
    agent
    worker["worker"]
    kb["knowledge"]
    df["docflow"]
    erp["erp-mock + volume erpstate"]
    pg[("postgres + pgdata")]
    tmp["temporal"]
  end
  agent --> kb
  agent --> erp
  agent --> tmp
  worker --> tmp
  worker --> erp
  df --> erp
```

- `/auth/dev-token`, `/_admin/reset` และ `/docs` เข้าจากภายนอกไม่ได้ `scripts/staging-smoke.py` ตรวจข้อนี้ด้วย
- image ของ agent สร้าง React UI ด้วย Vite ใน stage ของ node แล้วคัดลอก `dist` เข้าไป service อื่นไม่มีส่วน frontend
- เชื่อ `X-Forwarded-For` จาก Caddy (`FORWARDED_ALLOW_IPS`) เพื่อให้ rate limit นับตาม IP จริงของผู้ใช้

## ใครเห็น Audit log อะไร

`GET /audit` มีในทุก service staff เปิดไม่ได้ admin เห็นทั้งหมด ส่วน manager เห็นตามตารางนี้

| Service | Manager เห็นรายการที่ | ตัวอย่าง action |
|---|---|---|
| Agent | target เป็น AgentRun หรือ ApprovalRequest ของแผนกตัวเอง | `tool.*` · `approval.requested` · `approval.executed` |
| Knowledge · เอกสาร | เอกสารที่ ACL มีแผนกตัวเอง (ไม่ใช่แผนกของคนกด) | `knowledge.document.ingested` · `deactivated` |
| Knowledge · คำถาม | คนถามอยู่แผนกเดียวกัน หรือเป็นตัวเอง | `knowledge.ask` |
| DocFlow | target เป็นเอกสารของแผนกตัวเอง | `docflow.uploaded` · `corrected` · `posted` |
