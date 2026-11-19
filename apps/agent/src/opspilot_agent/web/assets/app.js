const el = (id) => document.getElementById(id);
const statusNames = {
  pending: "รออนุมัติ", approved: "อนุมัติแล้ว · กำลังดำเนินการ", executed: "ดำเนินการสำเร็จ",
  rejected: "ปฏิเสธ", expired: "หมดเวลา", failed: "ดำเนินการไม่สำเร็จ",
};
const sample = "เช็ค ORD-1001 ว่าของพอไหม ถ้าไม่พอให้โอนสต็อกจาก bangna";
let token = sessionStorage.getItem("opspilot-token");
let user;
let view = "agent";
let selected;
let auditTarget;
let poll;
let detailVersion = 0;
let queueVersion = 0;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function time(value) {
  const date = new Date(value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : value + "Z");
  return new Intl.DateTimeFormat("th-TH", { dateStyle: "short", timeStyle: "medium" }).format(date);
}

function error(message = "") {
  el("workspace-error").textContent = message;
  el("workspace-error").hidden = !message;
}

async function api(path, options = {}) {
  const { body, ...rest } = options;
  const headers = {};
  if (token) headers.Authorization = "Bearer " + token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...rest, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/session/login") logout();
    const message = typeof data.detail === "string" ? data.detail : "บริการไม่พร้อม กรุณาลองใหม่";
    throw new Error(message);
  }
  return data;
}

function logout() {
  token = null;
  user = null;
  selected = null;
  auditTarget = null;
  clearTimeout(poll);
  detailVersion++;
  queueVersion++;
  sessionStorage.removeItem("opspilot-token");
  el("identity").hidden = true;
  el("workspace").hidden = true;
  el("login-view").hidden = false;
  el("run-result").hidden = true;
  el("message").value = "";
  el("approval-detail").replaceChildren(element("p", "เลือกรายการเพื่อดูรายละเอียด", "muted"));
  error();
}

async function showSession() {
  user = await api("/session/me");
  el("account").textContent = (user.role === "staff" ? "Staff" : "Manager") + " · " + user.department;
  document.querySelector('[data-view="activity"]').hidden = user.role === "staff";
  el("identity").hidden = false;
  el("login-view").hidden = true;
  el("workspace").hidden = false;
  await openView("agent");
}

async function openView(name) {
  if (!user) return;
  clearTimeout(poll);
  detailVersion++;
  error();
  view = name;
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === name;
    el("view-" + button.dataset.view).hidden = !active;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (name === "approvals") {
    await loadQueue();
    if (selected) await loadApproval();
  }
  if (name === "activity") await loadEvents();
}

function badge(status) {
  return element("span", statusNames[status] || status, "badge " + status);
}

async function loadQueue() {
  const version = ++queueVersion;
  const rows = await api("/approvals");
  if (version !== queueVersion || view !== "approvals") return;
  const items = rows.map((row) => {
    const button = element("button", "", "item");
    button.type = "button";
    button.dataset.approval = row.id;
    button.setAttribute("aria-pressed", String(row.id === selected));
    button.append(element("span", row.summary), badge(row.status));
    button.onclick = () => {
      selected = row.id;
      loadQueue().catch((err) => error(err.message));
      loadApproval().catch((err) => error(err.message));
    };
    return button;
  });
  el("queue").replaceChildren(...(items.length ? items : [element("p", "ยังไม่มีคำขอ", "muted")]));
}

async function loadApproval() {
  clearTimeout(poll);
  const id = selected;
  const version = ++detailVersion;
  const row = await api("/approvals/" + id);
  const current = () => view === "approvals" && version === detailVersion;
  if (!current()) return;
  const content = [badge(row.status), element("h2", row.summary), element("p", "ผู้ขอ " + row.requested_by + " · " + time(row.created_at), "muted")];
  if (row.decided_by) content.push(element("p", "ผู้ตัดสินใจ " + row.decided_by + (row.decision_reason ? " · " + row.decision_reason : "")));
  if (row.result?.id) content.push(element("p", "รายการ ERP " + row.result.id));
  if (row.error) content.push(element("p", row.error, "error"));
  if (row.tool === "create_stock_transfer") {
    const stock = element("div", "", "stock");
    try {
      const snapshot = await api("/approvals/" + id + "/inventory");
      stock.append(...snapshot.stock.map((item) => element("span", item.branch + " · " + item.on_hand + " หน่วย")));
    } catch (err) {
      stock.append(element("p", err.message, "muted"));
    }
    content.push(element("h2", "สต็อกปัจจุบัน · " + row.args.sku), stock);
  }
  if (!current() || !user) return;
  const waitingForWorkflow = user.approval_backend === "temporal" && !row.workflow_id;
  if (row.status === "pending" && !waitingForWorkflow && user.role !== "staff" && row.requested_by !== user.id) {
    content.push(decisionForm(id));
  }
  if (user.role !== "staff") {
    const activity = element("button", "ดู Audit Log ของคำขอนี้");
    activity.onclick = () => {
      auditTarget = id;
      openView("activity").catch((err) => error(err.message));
    };
    content.push(activity);
  }
  el("approval-detail").replaceChildren(...content);
  if (row.status === "approved" || (row.status === "pending" && (waitingForWorkflow || user.role === "staff"))) {
    poll = setTimeout(async () => {
      try {
        await loadApproval();
        await loadQueue();
      } catch (err) {
        error(err.message);
      }
    }, 1200);
  }
}

function decisionForm(id) {
  const form = element("form", "", "stack");
  const label = element("label", "เหตุผลประกอบ");
  const input = element("input");
  input.name = "reason";
  input.maxLength = 1000;
  label.append(input);
  const buttons = element("div", "", "actions");
  const approve = element("button", "อนุมัติ", "primary");
  const reject = element("button", "ปฏิเสธ");
  approve.value = "approve";
  buttons.append(approve, reject);
  form.append(label, buttons);
  form.onsubmit = async (event) => {
    event.preventDefault();
    const choice = event.submitter?.value === "approve";
    approve.disabled = reject.disabled = true;
    error();
    try {
      await api("/approvals/" + id + "/decision", { method: "POST", body: { approve: choice, reason: input.value.trim() || null } });
      await loadApproval();
      await loadQueue();
    } catch (err) {
      error(err.message);
    } finally {
      approve.disabled = reject.disabled = false;
    }
  };
  return form;
}

async function loadEvents() {
  const target = auditTarget;
  const rows = await api("/audit" + (target ? "?target=" + encodeURIComponent(target) : ""));
  if (view !== "activity" || target !== auditTarget) return;
  const events = rows.map((row) => {
    const line = element("article", "", "event");
    line.append(element("p", row.action + " · " + row.status), element("p", row.actor + " · " + time(row.created_at), "muted"));
    return line;
  });
  el("audit-context").hidden = !target;
  el("audit-context").textContent = target ? "คำขอ " + target : "";
  el("events").replaceChildren(...(events.length ? events : [element("p", "ยังไม่มีกิจกรรม", "muted")]));
}

el("login").onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  const message = el("login-error");
  button.disabled = true;
  message.hidden = true;
  try {
    const result = await api("/session/login", { method: "POST", body: Object.fromEntries(new FormData(form)) });
    token = result.access_token;
    sessionStorage.setItem("opspilot-token", token);
    form.elements.password.value = "";
    await showSession();
  } catch (err) {
    message.textContent = err.message;
    message.hidden = false;
  } finally {
    button.disabled = false;
  }
};

el("composer").onsubmit = async (event) => {
  event.preventDefault();
  const message = el("message").value.trim();
  if (!message) return;
  const activeToken = token;
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  el("run-progress").textContent = "กำลังตรวจข้อมูลจาก ERP";
  error();
  try {
    const result = await api("/agent/runs", { method: "POST", body: { message } });
    if (activeToken !== token) return;
    el("answer").textContent = result.answer;
    el("run-approvals").replaceChildren(...result.approvals.map((row) => {
      const link = element("button", "เปิดคำขออนุมัติ");
      link.onclick = () => {
        selected = row.id;
        openView("approvals").catch((err) => error(err.message));
      };
      return link;
    }));
    el("tool-list").replaceChildren(...result.steps.filter((step) => step.type === "tool")
      .map((step) => element("li", step.tool + " · " + step.status)));
    el("run-result").hidden = false;
  } catch (err) {
    error(err.message);
  } finally {
    button.disabled = false;
    el("run-progress").textContent = "";
  }
};

el("sample").onclick = () => { el("message").value = sample; el("message").focus(); };
el("logout").onclick = logout;
document.querySelectorAll("[data-view]").forEach((button) => {
  button.onclick = () => {
    if (button.dataset.view === "activity") auditTarget = null;
    openView(button.dataset.view).catch((err) => error(err.message));
  };
});
document.querySelectorAll("[data-refresh]").forEach((button) => {
  button.onclick = () => openView(button.dataset.refresh).catch((err) => error(err.message));
});
if (token) showSession().catch(logout);
