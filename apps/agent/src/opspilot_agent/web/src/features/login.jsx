import { Box, ClipboardCheck, Sparkles } from "lucide-react";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";

export function Login({ onLogin }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await onLogin(Object.fromEntries(new FormData(event.currentTarget)));
    } catch (err) {
      setError(
        err.message === "invalid username or password"
          ? "บัญชีหรือรหัสผ่านไม่ถูกต้อง"
          : err.message,
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="min-h-dvh p-4 sm:p-8 lg:grid lg:grid-cols-[minmax(20rem,0.9fr)_1fr] lg:gap-8">
      <section className="relative flex min-h-56 flex-col justify-between gap-8 overflow-hidden rounded-2xl bg-sidebar px-6 py-6 text-white sm:px-10 sm:py-9 lg:min-h-[calc(100dvh-4rem)] lg:p-12">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_bottom_right,#2854a9_0%,transparent_58%)]"
        />
        <div className="relative">
          <Brand light />
        </div>
        <div className="relative space-y-4">
          <p className="text-sm font-semibold uppercase tracking-wider text-blue-200">
            ERP / POS Workspace
          </p>
          <p className="max-w-md text-3xl font-semibold leading-tight tracking-tight sm:text-4xl xl:text-5xl">
            จัดการสต็อกและงานอนุมัติ
          </p>
          <ul className="hidden space-y-4 pt-4 text-sm text-blue-100 sm:block">
            <li className="flex items-center gap-3">
              <Sparkles className="size-4" aria-hidden="true" />{" "}
              ตรวจออเดอร์และสต็อกด้วย Agent
            </li>
            <li className="flex items-center gap-3">
              <ClipboardCheck className="size-4" aria-hidden="true" />{" "}
              อนุมัติการโอนก่อนส่งเข้า ERP
            </li>
            <li className="flex items-center gap-3">
              <Box className="size-4" aria-hidden="true" />{" "}
              ตรวจผลการโอนและประวัติการทำงาน
            </li>
          </ul>
        </div>
      </section>
      <section className="grid place-items-center px-1 py-6 sm:px-4 lg:py-0">
        <Card className="w-full max-w-md shadow-sm">
          <CardHeader>
            <CardTitle>
              <h1 className="text-2xl tracking-tight">เข้าสู่ OpsPilot</h1>
            </CardTitle>
            <CardDescription className="leading-relaxed">
              เลือกบัญชีทดสอบเพื่อเข้าสู่พื้นที่ทำงาน
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={submit} className="space-y-5">
              <div className="grid gap-2 [&_[data-slot=native-select-wrapper]]:w-full">
                <Label htmlFor="username">บัญชีทดสอบ</Label>
                <NativeSelect id="username" name="username" className="w-full">
                  <NativeSelectOption value="staff">
                    Staff · Operations
                  </NativeSelectOption>
                  <NativeSelectOption value="manager">
                    Manager · Operations
                  </NativeSelectOption>
                </NativeSelect>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="password">รหัสผ่าน</Label>
                <Input
                  id="password"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  maxLength={256}
                  required
                />
              </div>
              <ErrorAlert message={error} />
              <Button type="submit" className="w-full" disabled={busy}>
                {busy ? "กำลังเข้าสู่ระบบ…" : "เข้าสู่ระบบ"}
              </Button>
            </form>
          </CardContent>
        </Card>
      </section>
    </main>
  );
}
