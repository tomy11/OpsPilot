import {
  Boxes,
  ChevronDown,
  ClipboardCheck,
  Pencil,
  Coins,
  Plus,
  RotateCcw,
  Send,
  Workflow,
} from "lucide-react";
import { useLayoutEffect, useRef } from "react";

import { Answer } from "@/components/answer";
import { ErrorAlert, Loading, StatusBadge } from "@/components/feedback";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { HistoryButton } from "@/features/history";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

const suggestions = [
  {
    title: "ใช้คำสั่งตัวอย่าง",
    description: "เช็ก ORD-1001 และขอโอนสต็อกถ้าของไม่พอ",
    prompt: "เช็ค ORD-1001 ว่าของพอไหม ถ้าไม่พอให้โอนสต็อกจาก bangna",
    Icon: ClipboardCheck,
  },
  {
    title: "ตรวจสถานะออเดอร์",
    description: "ดูรายการและสต็อกของ ORD-1002",
    prompt: "เช็ค ORD-1002 หน่อย",
    Icon: Boxes,
  },
];

export function AgentPage({
  message,
  setMessage,
  onRun,
  onReset,
  onRetry,
  onEdit,
  busy,
  api,
  chat,
  openChat,
  onDeleted,
  credits,
  turns,
  openApproval,
}) {
  const prompt = useRef(null);
  const scroller = useRef(null);
  const follow = useRef(true);

  useLayoutEffect(() => {
    const panel = scroller.current;
    if (panel && follow.current) panel.scrollTop = panel.scrollHeight;
  }, [turns]);

  function submit(event) {
    event.preventDefault();
    if (busy || !message.trim()) return;
    follow.current = true;
    prompt.current?.focus({ preventScroll: true });
    onRun();
  }

  function fillPrompt(text) {
    setMessage(text);
    prompt.current?.focus({ preventScroll: true });
  }

  function trackScroll(event) {
    const panel = event.currentTarget;
    follow.current =
      panel.scrollHeight - panel.clientHeight - panel.scrollTop < 64;
  }

  function handleKeyDown(event) {
    if (
      event.key !== "Enter" ||
      event.shiftKey ||
      event.nativeEvent.isComposing ||
      event.nativeEvent.keyCode === 229
    ) {
      return;
    }
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  return (
    <section
      aria-labelledby="agent-title"
      className="flex min-h-0 flex-1 flex-col bg-card"
    >
      <header className="flex shrink-0 items-center justify-between gap-3 border-b px-5 py-3 sm:px-8">
        <h1 id="agent-title" className="text-base font-semibold tracking-tight">
          Agent
        </h1>
        <div className="flex items-center gap-3">
          {credits !== null && credits !== undefined && (
            <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
              <Coins className="size-4" aria-hidden="true" />{" "}
              <span className="sr-only sm:not-sr-only">เครดิต</span>{" "}
              <span className="font-semibold tabular-nums text-foreground">
                {credits.toLocaleString("th-TH")}
              </span>
            </span>
          )}
        <HistoryButton
          api={api}
          disabled={busy}
          current={chat}
          onOpen={(id) => {
            follow.current = true;
            return openChat(id);
          }}
          onDeleted={onDeleted}
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy || (!turns.length && !message)}
          onClick={() => {
            onReset();
            prompt.current?.focus({ preventScroll: true });
          }}
        >
          <Plus aria-hidden="true" />{" "}
          <span className="sr-only sm:not-sr-only">แชตใหม่</span>
        </Button>
        </div>
      </header>

      <div
        ref={scroller}
        onScroll={trackScroll}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
        tabIndex={0}
        role="region"
        aria-label="บทสนทนา"
      >
        {turns.length === 0 ? (
          <div className="mx-auto flex min-h-full max-w-3xl flex-col justify-center gap-7 px-5 py-8 sm:px-8">
            <div className="space-y-3">
              <AgentMark large />
              <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
                วันนี้ต้องการตรวจอะไรใน ERP?
              </h2>
              <p className="max-w-xl leading-relaxed text-muted-foreground">
                ตรวจออเดอร์ เช็กสต็อก หรือเตรียมคำขอโอนสินค้าให้ผู้จัดการอนุมัติ
              </p>
            </div>
            <div className="grid gap-2.5 sm:grid-cols-2">
              {suggestions.map(
                ({ title, description, prompt: example, Icon }) => (
                  <Button
                    key={title}
                    type="button"
                    variant="outline"
                    onClick={() => fillPrompt(example)}
                    className="h-auto justify-start gap-3 whitespace-normal rounded-xl px-3 py-3 text-left hover:border-primary/35"
                  >
                    <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-secondary text-primary">
                      <Icon className="size-4" aria-hidden="true" />
                    </span>
                    <span className="grid gap-1">
                      <span className="font-semibold">{title}</span>
                      <span className="text-sm font-normal text-muted-foreground">
                        {description}
                      </span>
                    </span>
                  </Button>
                ),
              )}
            </div>
          </div>
        ) : (
          <div
            role="log"
            aria-label="ข้อความในแชต"
            className="mx-auto max-w-3xl space-y-8 px-5 py-6 sm:px-8 sm:py-8"
          >
            {turns.map((turn) => (
              <ChatTurn
                key={turn.id}
                turn={turn}
                busy={busy}
                openApproval={openApproval}
                onRetry={() => {
                  follow.current = true;
                  onRetry(turn.id);
                }}
                onEdit={() => {
                  onEdit(turn.id);
                  prompt.current?.focus({ preventScroll: true });
                }}
              />
            ))}
          </div>
        )}
      </div>

      <div className="shrink-0 border-t bg-background/75 px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-8 sm:pt-4">
        <form className="mx-auto max-w-3xl space-y-2" onSubmit={submit}>
          <Label htmlFor="message" className="sr-only">
            คำสั่งของคุณ
          </Label>
          <div className="flex items-end gap-2 rounded-2xl border border-input bg-card p-2 shadow-sm focus-within:border-primary/70">
            <Textarea
              ref={prompt}
              id="message"
              rows={1}
              maxLength={4000}
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="พิมพ์คำถามเกี่ยวกับออเดอร์หรือสต็อก…"
              className="field-sizing-content max-h-[min(24dvh,11rem)] min-h-10 resize-none overflow-y-auto border-0 bg-transparent px-2.5 py-2 text-base shadow-none focus-visible:ring-0"
              required
            />
            <Button
              type="submit"
              size="icon"
              className="size-10 shrink-0 rounded-xl"
              disabled={busy || !message.trim()}
              aria-label="ส่งให้ Agent"
            >
              <Send aria-hidden="true" />
            </Button>
          </div>
          <p className="px-1 text-center text-xs text-muted-foreground">
            <span className="hidden sm:inline">
              Enter เพื่อส่ง · Shift + Enter เพื่อขึ้นบรรทัดใหม่ ·{" "}
            </span>
            Agent จำข้อความในแชตนี้ เปลี่ยนเรื่องให้เริ่มแชตใหม่
          </p>
        </form>
      </div>
    </section>
  );
}

function AgentMark({ large = false }) {
  return (
    <span
      className={
        large
          ? "grid size-12 shrink-0 place-items-center rounded-xl bg-primary text-primary-foreground"
          : "grid size-8 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground"
      }
    >
      <Workflow className={large ? "size-6" : "size-4"} aria-hidden="true" />
    </span>
  );
}

function ChatTurn({ turn, busy, openApproval, onRetry, onEdit }) {
  return (
    <article className="grid gap-5">
      <div className="max-w-[90%] justify-self-end rounded-2xl rounded-br-sm bg-secondary px-4 py-3 text-foreground sm:max-w-[80%]">
        <p className="mb-1 text-xs font-semibold text-secondary-foreground">
          คุณ
        </p>
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere] leading-relaxed">
          {turn.prompt}
        </p>
      </div>
      <div className="flex items-start gap-3">
        <AgentMark />
        <div className="min-w-0 flex-1 space-y-3 pt-1">
          <p className="text-sm font-semibold">OpsPilot Agent</p>
          {turn.error ? (
            <div className="space-y-2">
              <ErrorAlert message={turn.error} />
              <div className="flex flex-wrap gap-2">
                <Button type="button" size="sm" disabled={busy} onClick={onRetry}>
                  <RotateCcw aria-hidden="true" /> ส่งอีกครั้ง
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={onEdit}
                >
                  <Pencil aria-hidden="true" /> แก้ข้อความ
                </Button>
              </div>
            </div>
          ) : turn.run ? (
            <AgentReply run={turn.run} openApproval={openApproval} />
          ) : (
            <Loading>กำลังตรวจข้อมูลจาก ERP</Loading>
          )}
        </div>
      </div>
    </article>
  );
}

function AgentReply({ run, openApproval }) {
  const tools = run.steps.filter((step) => step.type === "tool");

  return (
    <>
      <Answer text={run.answer} />
      {run.approvals.length === 0 && (
        <p className="text-xs text-muted-foreground">
          ข้อความนี้ไม่ได้สร้างคำขออนุมัติ
        </p>
      )}
      {run.credits && (
        <p className="text-xs text-muted-foreground">
          ใช้ {run.credits.charged.toLocaleString("th-TH")} เครดิต
        </p>
      )}
      {run.approvals.map((row) => (
        <div
          key={row.id}
          className="flex flex-wrap items-center gap-3 rounded-xl border bg-muted/60 p-3"
        >
          <div className="min-w-0 flex-1 space-y-2 basis-40">
            <StatusBadge status={row.status} />
            <p className="text-sm [overflow-wrap:anywhere]">{row.summary}</p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => openApproval(row.id)}
          >
            เปิดคำขออนุมัติ
          </Button>
        </div>
      ))}
      {tools.length > 0 && (
        <Collapsible>
          <CollapsibleTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              className="group h-auto whitespace-normal py-2 text-left text-muted-foreground"
            >
              <ChevronDown
                className="transition-transform group-data-[state=open]:rotate-180"
                aria-hidden="true"
              />
              เครื่องมือที่ Agent เรียกใช้
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <ul className="mt-2 grid gap-2 border-l pl-4 text-sm text-muted-foreground">
              {tools.map((step, index) => (
                <li key={index}>
                  {step.tool} · {step.status}
                </li>
              ))}
            </ul>
          </CollapsibleContent>
        </Collapsible>
      )}
    </>
  );
}
