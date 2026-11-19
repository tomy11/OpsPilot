import { CircleAlert, LoaderCircle } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";

const statuses = {
  pending: ["รออนุมัติ", "bg-amber-100 text-amber-900"],
  approved: ["อนุมัติแล้ว · กำลังดำเนินการ", "bg-blue-100 text-blue-900"],
  executed: ["ดำเนินการสำเร็จ", "bg-emerald-100 text-emerald-900"],
  failed: ["ดำเนินการไม่สำเร็จ", "bg-rose-100 text-rose-900"],
  rejected: ["ปฏิเสธ", "bg-rose-100 text-rose-900"],
  expired: ["หมดเวลา", "bg-slate-200 text-slate-800"],
};

export function StatusBadge({ status }) {
  const [label, color] = statuses[status] || [
    status,
    "bg-slate-100 text-slate-800",
  ];
  return (
    <Badge className={color + " h-auto whitespace-normal text-sm"}>
      {label}
    </Badge>
  );
}

export function ErrorAlert({ message }) {
  if (!message) return null;
  return (
    <Alert variant="destructive">
      <CircleAlert aria-hidden="true" />
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  );
}

export function Loading({ children }) {
  return (
    <p
      role="status"
      className="flex items-center gap-2 text-sm text-muted-foreground"
    >
      <LoaderCircle
        className="size-4 motion-safe:animate-spin"
        aria-hidden="true"
      />
      {children}
    </p>
  );
}
