import { Boxes, ClipboardCheck, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { ErrorAlert, Loading, StatusBadge } from "@/components/feedback";
import { PageHeading } from "@/components/workspace";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { formatTime } from "@/lib/api";

// Temporal or Cloudflare: decisions wait until the approval's workflow has started.
const durable = (user) => user.approval_backend !== "inline";

function watchApproval(user, row) {
  return (
    row.status === "approved" ||
    (row.status === "pending" &&
      (user.role === "staff" ||
        (durable(user) && !row.workflow_id)))
  );
}

export function ApprovalsPage({ api, user, selected, onSelect, openActivity }) {
  const [queue, setQueue] = useState(null);
  const [detail, setDetail] = useState(null);
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [revision, setRevision] = useState(0);

  function refresh() {
    setListError("");
    setDetailError("");
    setRevision((value) => value + 1);
  }

  useEffect(() => {
    const controller = new AbortController();
    api("/approvals", { signal: controller.signal })
      .then(setQueue)
      .catch((error) => {
        if (!controller.signal.aborted) setListError(error.message);
      });
    return () => controller.abort();
  }, [api, revision]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    const { signal } = controller;
    const path = "/approvals/" + encodeURIComponent(selected);
    let timer;
    let row;

    async function load() {
      try {
        row = await api(path, { signal });
        let stock;
        let inventoryError;
        if (row.tool === "create_stock_transfer") {
          try {
            stock = await api(path + "/inventory", { signal });
          } catch (error) {
            if (signal.aborted) return;
            inventoryError = error.message;
          }
        }
        if (signal.aborted) return;
        setDetail({ row, stock, inventoryError });
        setDetailError("");
        setQueue(
          (current) =>
            current?.map((item) => (item.id === row.id ? row : item)) ??
            current,
        );
      } catch (error) {
        if (!signal.aborted) setDetailError(error.message);
      } finally {
        if (!signal.aborted && row && watchApproval(user, row)) {
          timer = setTimeout(load, 1500);
        }
      }
    }

    load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, user, selected, revision]);

  function select(id) {
    if (id !== selected) {
      setDetailError("");
      setDetail(null);
      onSelect(id);
    }
  }

  const current = detail?.row.id === selected ? detail : null;

  return (
    <>
      <PageHeading
        title="Approvals"
        description="ติดตามคำขอ ตรวจสต็อก และดูผลหลังอนุมัติ"
      />
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <Card className="shadow-sm">
          <CardHeader className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>
              <h2 className="text-lg">คำขอในระบบ</h2>
            </CardTitle>
            <Button variant="outline" onClick={refresh}>
              <RefreshCw aria-hidden="true" /> รีเฟรช
            </Button>
          </CardHeader>
          <CardContent className="max-h-[28rem] space-y-3 overflow-y-auto">
            <ErrorAlert message={listError} />
            {!queue && !listError && <Loading>กำลังโหลดคำขอ</Loading>}
            {queue?.length === 0 && (
              <p className="text-muted-foreground">ยังไม่มีคำขอ</p>
            )}
            {queue?.map((row) => (
              <Button
                key={row.id}
                variant="outline"
                data-approval={row.id}
                aria-pressed={row.id === selected}
                onClick={() => select(row.id)}
                className="flex h-auto w-full min-w-0 flex-col items-start justify-start gap-2 whitespace-normal px-3 py-2.5 text-left aria-pressed:border-primary/50 aria-pressed:bg-accent"
              >
                <span className="[overflow-wrap:anywhere]">{row.summary}</span>
                <StatusBadge status={row.status} />
              </Button>
            ))}
          </CardContent>
        </Card>
        <Card className="shadow-sm">
          {current ? (
            <ApprovalDetail
              {...current}
              api={api}
              user={user}
              refresh={refresh}
              openActivity={openActivity}
              error={detailError}
            />
          ) : (
            <CardContent className="flex min-h-40 flex-col justify-center gap-3">
              <ErrorAlert message={detailError} />
              {selected && !detailError && <Loading>กำลังโหลดรายละเอียด</Loading>}
              {!selected && (
                <>
                  <ClipboardCheck
                    className="size-6 text-muted-foreground"
                    aria-hidden="true"
                  />
                  <p className="text-muted-foreground">
                    เลือกรายการเพื่อดูรายละเอียด
                  </p>
                </>
              )}
            </CardContent>
          )}
        </Card>
      </div>
    </>
  );
}

function ApprovalDetail({
  row,
  stock,
  inventoryError,
  api,
  user,
  refresh,
  openActivity,
  error,
}) {
  const awaitingWorkflow = durable(user) && !row.workflow_id;
  const canDecide =
    row.status === "pending" &&
    !awaitingWorkflow &&
    user.role !== "staff" &&
    row.requested_by !== user.id;

  return (
    <>
      <CardHeader className="gap-3">
        <div aria-live="polite">
          <StatusBadge status={row.status} />
        </div>
        <CardTitle>
          <h2 className="text-lg leading-relaxed [overflow-wrap:anywhere]">
            {row.summary}
          </h2>
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          ผู้ขอ {row.requested_by} · {formatTime(row.created_at)}
        </p>
      </CardHeader>
      <CardContent className="space-y-4 [overflow-wrap:anywhere]">
        <ErrorAlert message={error || row.error} />
        {row.decided_by && (
          <p className="text-sm">
            ผู้ตัดสินใจ {row.decided_by}
            {row.decision_reason ? " · " + row.decision_reason : ""}
          </p>
        )}
        {row.result?.id && (
          <p className="text-sm">
            รายการ ERP{" "}
            <span className="font-mono font-medium">{row.result.id}</span>
          </p>
        )}
        {row.tool === "create_stock_transfer" && (
          <section className="space-y-3 rounded-lg border bg-muted/50 p-4">
            <h3 className="flex items-center gap-2 text-sm font-semibold">
              <Boxes className="size-4 text-primary" aria-hidden="true" />{" "}
              สต็อกปัจจุบัน · {row.args.sku}
            </h3>
            {inventoryError && (
              <p className="text-sm text-muted-foreground">{inventoryError}</p>
            )}
            {stock && (
              <div className="grid grid-cols-2 gap-2">
                {stock.stock.map((item) => (
                  <div
                    className="rounded-md border bg-card p-2.5 text-sm"
                    key={item.branch}
                  >
                    {item.branch} ·{" "}
                    <span className="font-semibold tabular-nums">
                      {item.on_hand}
                    </span>{" "}
                    หน่วย
                  </div>
                ))}
              </div>
            )}
          </section>
        )}
        {awaitingWorkflow && row.status === "pending" && (
          <Loading>กำลังเตรียม workflow</Loading>
        )}
        {canDecide && (
          <DecisionForm key={row.id} row={row} api={api} refresh={refresh} />
        )}
        {user.role !== "staff" && (
          <Button variant="outline" onClick={() => openActivity(row.id)}>
            ดู Audit Log ของคำขอนี้
          </Button>
        )}
      </CardContent>
    </>
  );
}

function DecisionForm({ row, api, refresh }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    const submitter = event.nativeEvent.submitter;
    if (!submitter) return;
    const reason =
      new FormData(event.currentTarget).get("reason")?.trim() || null;
    setBusy(true);
    setError("");
    try {
      await api("/approvals/" + encodeURIComponent(row.id) + "/decision", {
        method: "POST",
        body: { approve: submitter.value === "approve", reason },
      });
      refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      <Separator />
      <div className="grid gap-2">
        <Label htmlFor="approval-reason">เหตุผลประกอบ</Label>
        <Input id="approval-reason" name="reason" maxLength={1000} />
      </div>
      <ErrorAlert message={error} />
      <div className="flex flex-wrap gap-2">
        <Button type="submit" value="approve" disabled={busy}>
          อนุมัติ
        </Button>
        <Button type="submit" value="reject" variant="outline" disabled={busy}>
          ปฏิเสธ
        </Button>
      </div>
    </form>
  );
}
