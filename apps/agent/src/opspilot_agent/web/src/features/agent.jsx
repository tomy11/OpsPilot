import { ChevronDown, ClipboardList, Send, Sparkles } from "lucide-react";
import { useRef } from "react";

import { ErrorAlert, Loading, StatusBadge } from "@/components/feedback";
import { PageHeading } from "@/components/workspace";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

const sample = "เช็ค ORD-1001 ว่าของพอไหม ถ้าไม่พอให้โอนสต็อกจาก bangna";

export function AgentPage({
  message,
  setMessage,
  onRun,
  busy,
  run,
  error,
  openApproval,
}) {
  const prompt = useRef(null);

  function useSample() {
    setMessage(sample);
    prompt.current?.focus();
  }

  const tools = run?.steps.filter((step) => step.type === "tool") || [];

  return (
    <>
      <PageHeading
        title="Agent"
        description="ตรวจออเดอร์ เช็กสต็อก หรือส่งคำขอโอนสินค้าให้ผู้จัดการ"
      />
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.65fr)_minmax(0,1fr)]">
        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle>
              <h2 className="flex items-center gap-2 text-lg">
                <Sparkles className="size-5 text-primary" aria-hidden="true" />{" "}
                สั่งงาน Agent
              </h2>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <form className="space-y-4" onSubmit={onRun}>
              <div className="grid gap-2">
                <Label htmlFor="message">คำสั่งของคุณ</Label>
                <Textarea
                  ref={prompt}
                  id="message"
                  rows={5}
                  maxLength={4000}
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                  placeholder="ระบุเลขออเดอร์และสิ่งที่ต้องการให้ตรวจสอบ"
                  className="min-h-36 resize-y text-base"
                  required
                />
              </div>
              <ErrorAlert message={error} />
              <div className="flex flex-wrap items-center gap-3">
                <Button type="submit" disabled={busy || !message.trim()}>
                  <Send aria-hidden="true" /> ส่งให้ Agent
                </Button>
                {busy && <Loading>กำลังตรวจข้อมูลจาก ERP</Loading>}
              </div>
            </form>
          </CardContent>
        </Card>
        <Card className="border-blue-200 bg-blue-50/70 shadow-none">
          <CardHeader>
            <CardDescription className="font-medium text-blue-800">
              เคสตัวอย่าง
            </CardDescription>
            <CardTitle>
              <h2 className="text-lg">ออเดอร์ ORD-1001</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="leading-relaxed text-slate-700">
              สาขาสีลมต้องใช้กาแฟ 10 หน่วย มีในสต็อก 3 หน่วย ลองให้ Agent
              ขอเบิกส่วนที่ขาดจากบางนา
            </p>
            <Button type="button" variant="outline" onClick={useSample}>
              <ClipboardList aria-hidden="true" /> ใช้คำสั่งตัวอย่าง
            </Button>
          </CardContent>
        </Card>
      </div>
      {run && (
        <Card aria-live="polite" className="shadow-sm">
          <CardHeader>
            <CardTitle>
              <h2 className="text-lg">ผลการตรวจสอบ</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="whitespace-pre-wrap [overflow-wrap:anywhere] leading-relaxed">
              {run.answer}
            </p>
            {run.approvals.map((row) => (
              <div
                key={row.id}
                className="flex flex-wrap items-center gap-2 rounded-lg border bg-muted/45 p-3"
              >
                <StatusBadge status={row.status} />
                <span className="flex-1 text-sm [overflow-wrap:anywhere]">
                  {row.summary}
                </span>
                <Button variant="outline" onClick={() => openApproval(row.id)}>
                  เปิดคำขออนุมัติ
                </Button>
              </div>
            ))}
            {tools.length > 0 && (
              <Collapsible>
                <CollapsibleTrigger asChild>
                  <Button
                    variant="ghost"
                    className="group text-muted-foreground"
                  >
                    <ChevronDown
                      className="transition-transform group-data-[state=open]:rotate-180"
                      aria-hidden="true"
                    />{" "}
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
          </CardContent>
        </Card>
      )}
    </>
  );
}
