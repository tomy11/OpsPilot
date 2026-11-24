import { useState } from "react";

import { ErrorAlert } from "@/components/feedback";
import { Brand } from "@/components/workspace";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

// The link is used only after this click, so a link preview cannot consume it.
export function InvitePage({ onAccept, onCancel }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function accept() {
    setBusy(true);
    setError("");
    try {
      await onAccept();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <main className="grid min-h-dvh place-items-center bg-sidebar p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="gap-4">
          <Brand />
          <CardTitle>
            <h1 className="text-2xl tracking-tight">คุณได้รับเชิญให้ใช้ OpsPilot</h1>
          </CardTitle>
          <CardDescription className="leading-relaxed">
            ลิงก์นี้ใช้ได้ครั้งเดียว และจะผูกกับเบราว์เซอร์นี้
            ข้อความที่ส่งจะถูกประมวลผลโดยผู้ให้บริการ AI ภายนอก
            กรุณาใช้เฉพาะข้อมูลตัวอย่าง
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <ErrorAlert message={error} />
          <Button className="w-full" disabled={busy} onClick={accept}>
            {busy ? "กำลังเปิด…" : "เริ่มใช้งาน"}
          </Button>
          {error && (
            <Button variant="ghost" className="w-full" onClick={onCancel}>
              ไปหน้าเข้าสู่ระบบ
            </Button>
          )}
        </CardContent>
      </Card>
    </main>
  );
}
