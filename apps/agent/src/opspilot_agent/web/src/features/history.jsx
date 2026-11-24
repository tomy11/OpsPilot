import { History, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { ErrorAlert, Loading } from "@/components/feedback";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatTime } from "@/lib/api";

export function HistoryButton({ api, disabled, current, onOpen, onDeleted }) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        <History aria-hidden="true" />{" "}
        <span className="sr-only sm:not-sr-only">ประวัติ</span>
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>ประวัติแชต</DialogTitle>
            <DialogDescription>
              การลบจะเอาแชตออกจากประวัติและความจำของ Agent
              แต่ข้อความยังเก็บไว้ในบันทึกการทำงานของระบบ
            </DialogDescription>
          </DialogHeader>
          {open && (
            <ChatList
              api={api}
              current={current}
              onDeleted={onDeleted}
              onOpen={async (id) => {
                await onOpen(id);
                setOpen(false);
              }}
            />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

function ChatList({ api, current, onOpen, onDeleted }) {
  const [chats, setChats] = useState(null);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    api("/agent/conversations", { signal: controller.signal })
      .then(setChats)
      .catch((err) => {
        if (!controller.signal.aborted) setError(err.message);
      });
    return () => controller.abort();
  }, [api]);

  async function run(action) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const remove = (id) =>
    run(async () => {
      await api("/agent/conversations/" + id, { method: "DELETE" });
      setChats((list) => list.filter((chat) => chat.id !== id));
      setConfirming(null);
      onDeleted(id);
    });

  return (
    <div className="max-h-[60dvh] space-y-2 overflow-y-auto">
      <ErrorAlert message={error} />
      {!chats && !error && <Loading>กำลังโหลดประวัติ</Loading>}
      {chats?.length === 0 && (
        <p className="text-muted-foreground">ยังไม่มีแชตในประวัติ</p>
      )}
      {chats?.map((chat) => (
        <div
          key={chat.id}
          className="flex items-center gap-2 rounded-lg border p-2"
        >
          <Button
            variant="ghost"
            disabled={busy}
            aria-current={chat.id === current ? "true" : undefined}
            onClick={() => run(() => onOpen(chat.id))}
            className="h-auto min-w-0 flex-1 flex-col items-start gap-1 whitespace-normal py-2 text-left"
          >
            <span className="[overflow-wrap:anywhere]">{chat.title}</span>
            <span className="text-xs font-normal text-muted-foreground">
              {formatTime(chat.updated_at)}
              {chat.id === current ? " · กำลังเปิดอยู่" : ""}
            </span>
          </Button>
          {confirming === chat.id ? (
            <>
              <Button
                size="sm"
                variant="destructive"
                disabled={busy}
                onClick={() => remove(chat.id)}
              >
                ยืนยันลบ
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => setConfirming(null)}
              >
                ไม่ลบ
              </Button>
            </>
          ) : (
            <Button
              size="icon"
              variant="ghost"
              disabled={busy}
              aria-label={"ลบแชต " + chat.title}
              onClick={() => setConfirming(chat.id)}
            >
              <Trash2 aria-hidden="true" />
            </Button>
          )}
        </div>
      ))}
    </div>
  );
}
