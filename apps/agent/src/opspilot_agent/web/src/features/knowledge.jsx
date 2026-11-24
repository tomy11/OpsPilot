import { BookOpen, FileText, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { ErrorAlert, Loading } from "@/components/feedback";
import { PageHeading } from "@/components/workspace";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

const examples = ["ลาพักร้อนได้กี่วันต่อปี", "เบิกค่าใช้จ่ายต้องแนบเอกสารอะไร"];

export function KnowledgePage({ api }) {
  const input = useRef(null);
  const pending = useRef(null);
  const citationTrigger = useRef(null);
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [citation, setCitation] = useState(null);

  useEffect(() => () => pending.current?.abort(), []);

  async function ask(event) {
    event.preventDefault();
    const text = question.trim();
    if (!text || pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setResult(null);
    setError("");
    try {
      const response = await api("/knowledge/ask", {
        method: "POST",
        body: { question: text },
        signal: controller.signal,
      });
      if (!controller.signal.aborted)
        setResult({ ...response, question: text });
    } catch (err) {
      if (!controller.signal.aborted) setError(err.message);
    } finally {
      if (!controller.signal.aborted) {
        pending.current = null;
        setBusy(false);
      }
    }
  }

  function useExample(example) {
    setQuestion(example);
    input.current?.focus();
  }

  function openCitation(source, event) {
    citationTrigger.current = event.currentTarget;
    setCitation(source);
  }

  return (
    <>
      <PageHeading
        title="Knowledge"
        description="ค้นคำตอบจาก SOP และเอกสารภายในที่คุณมีสิทธิ์อ่าน"
      />
      <Card className="shadow-sm">
        <CardContent className="space-y-4">
          <form className="space-y-4" onSubmit={ask}>
            <div className="grid gap-2">
              <Label htmlFor="knowledge-question">คำถามของคุณ</Label>
              <Textarea
                ref={input}
                id="knowledge-question"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                maxLength={2000}
                rows={3}
                placeholder="เช่น พนักงานลาพักร้อนได้กี่วันต่อปี"
                className="min-h-24 resize-y text-base"
                required
              />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" disabled={busy || !question.trim()}>
                <Search aria-hidden="true" /> ถาม Knowledge
              </Button>
              {busy && <Loading>กำลังค้นเอกสาร</Loading>}
            </div>
          </form>
          <div className="flex flex-wrap gap-2" aria-label="คำถามตัวอย่าง">
            {examples.map((example) => (
              <Button
                key={example}
                type="button"
                variant="outline"
                size="sm"
                onClick={() => useExample(example)}
                className="h-auto whitespace-normal text-left"
              >
                {example}
              </Button>
            ))}
          </div>
          <ErrorAlert message={error} />
        </CardContent>
      </Card>

      {result && (
        <KnowledgeAnswer result={result} openCitation={openCitation} />
      )}

      <Dialog
        open={citation !== null}
        onOpenChange={(open) => {
          if (!open) setCitation(null);
        }}
      >
        {citation && (
          <DialogContent
            className="max-h-[85dvh] overflow-y-auto"
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              citationTrigger.current?.focus();
            }}
          >
            <DialogHeader>
              <Badge variant="secondary">อ้างอิง {citation.n}</Badge>
              <DialogTitle className="pr-6 [overflow-wrap:anywhere]">
                {citation.title}
              </DialogTitle>
              <DialogDescription className="leading-relaxed [overflow-wrap:anywhere]">
                {citation.section}
              </DialogDescription>
            </DialogHeader>
            <blockquote className="whitespace-pre-wrap rounded-lg border-l-4 border-l-primary/60 bg-muted p-4 leading-relaxed [overflow-wrap:anywhere]">
              {citation.snippet}
            </blockquote>
            <p className="text-sm text-muted-foreground [overflow-wrap:anywhere]">
              {citation.source} · เวอร์ชัน {citation.version}
            </p>
          </DialogContent>
        )}
      </Dialog>
    </>
  );
}

function KnowledgeAnswer({ result, openCitation }) {
  const sources = result.grounded ? result.citations : [];
  const byNumber = new Map(sources.map((source) => [String(source.n), source]));
  const parts = result.answer.split(/(\[\d+\])/g);

  return (
    <div
      className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]"
      aria-live="polite"
    >
      <Card className="shadow-sm">
        <CardHeader className="gap-3">
          <CardTitle>
            <h2 className="flex items-center gap-2 text-lg">
              <BookOpen className="size-5 text-primary" aria-hidden="true" />{" "}
              คำตอบ
            </h2>
          </CardTitle>
          <p className="text-sm text-muted-foreground [overflow-wrap:anywhere]">
            {result.question}
          </p>
        </CardHeader>
        <CardContent>
          <p className="whitespace-pre-wrap leading-relaxed [overflow-wrap:anywhere]">
            {parts.map((part, index) => {
              const source = byNumber.get(part.slice(1, -1));
              if (!source || part !== `[${source.n}]`) return part;
              return (
                <Button
                  key={index}
                  type="button"
                  variant="secondary"
                  size="xs"
                  className="mx-0.5 align-baseline"
                  aria-label={`เปิดอ้างอิง ${source.n} ${source.title}`}
                  onClick={(event) => openCitation(source, event)}
                >
                  {part}
                </Button>
              );
            })}
          </p>
        </CardContent>
      </Card>
      {sources.length > 0 && (
        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle>
              <h2 className="text-lg">เอกสารอ้างอิง</h2>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="grid gap-2">
              {sources.map((source) => (
                <li key={source.n}>
                  <Button
                    type="button"
                    variant="outline"
                    className="h-auto w-full min-w-0 justify-start gap-3 whitespace-normal py-2.5 text-left"
                    onClick={(event) => openCitation(source, event)}
                  >
                    <FileText className="text-primary" aria-hidden="true" />
                    <span className="grid min-w-0 gap-1 [overflow-wrap:anywhere]">
                      <span>
                        {source.n}. {source.title}
                      </span>
                      <span className="text-xs font-normal text-muted-foreground">
                        {source.section}
                      </span>
                    </span>
                  </Button>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
