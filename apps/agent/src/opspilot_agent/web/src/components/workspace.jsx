import {
  Activity,
  ClipboardCheck,
  LogOut,
  Sparkles,
  Workflow,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { cn } from "cn";

const pages = [
  { id: "agent", title: "Agent", icon: Sparkles },
  { id: "approvals", title: "Approvals", icon: ClipboardCheck },
  { id: "activity", title: "Activity", icon: Activity },
];

export function Brand({ light = false }) {
  return (
    <div className="flex items-center gap-3">
      <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-primary text-primary-foreground">
        <Workflow className="size-5" aria-hidden="true" />
      </span>
      <span
        className={cn(
          "text-lg font-semibold tracking-tight",
          light && "text-white",
        )}
      >
        OpsPilot
      </span>
    </div>
  );
}

export function Workspace({ user, page, onNavigate, onLogout, children }) {
  const role = { staff: "Staff", manager: "Manager", admin: "Admin" }[
    user.role
  ];

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
      <aside className="flex flex-col gap-4 bg-sidebar px-4 py-4 text-sidebar-foreground sm:px-6 lg:sticky lg:top-0 lg:h-dvh lg:gap-8 lg:px-5 lg:py-7">
        <div className="flex items-center justify-between gap-3 lg:block">
          <Brand light />
          <Button
            variant="ghost"
            size="icon"
            className="text-sidebar-foreground hover:bg-sidebar-accent hover:text-white lg:hidden"
            onClick={onLogout}
            aria-label="ออกจากระบบ"
          >
            <LogOut aria-hidden="true" />
          </Button>
        </div>
        <nav aria-label="เมนูหลัก" className="flex flex-wrap gap-2 lg:grid">
          {pages
            .filter(({ id }) => id !== "activity" || user.role !== "staff")
            .map(({ id, title, icon: Icon }) => (
              <Button
                type="button"
                key={id}
                variant="ghost"
                aria-current={page === id ? "page" : undefined}
                onClick={() => onNavigate(id)}
                className={cn(
                  "justify-start rounded-lg px-3 text-sm text-sidebar-foreground hover:bg-sidebar-accent hover:text-white lg:h-10",
                  page === id && "bg-sidebar-accent font-semibold text-white",
                )}
              >
                <Icon aria-hidden="true" />
                {title}
              </Button>
            ))}
        </nav>
        <div className="mt-auto hidden space-y-3 lg:block">
          <Separator className="bg-sidebar-border" />
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="text-sm font-semibold text-white">{role}</div>
              <div className="truncate text-sm text-sidebar-foreground">
                {user.department}
              </div>
            </div>
            <Button
              variant="ghost"
              size="icon"
              onClick={onLogout}
              aria-label="ออกจากระบบ"
              className="text-sidebar-foreground hover:bg-sidebar-accent hover:text-white"
            >
              <LogOut aria-hidden="true" />
            </Button>
          </div>
        </div>
      </aside>
      <main className="min-w-0">
        <div className="border-b bg-card px-5 py-3.5 sm:px-8">
          <div className="mx-auto flex max-w-6xl items-center justify-between gap-2">
            <span className="text-sm font-semibold text-muted-foreground">
              ERP / POS Workspace
            </span>
            <Badge variant="secondary" className="text-sm">
              {role} · {user.department}
            </Badge>
          </div>
        </div>
        <div className="mx-auto max-w-6xl space-y-6 px-5 py-8 sm:px-8 sm:py-10">
          {children}
        </div>
      </main>
    </div>
  );
}

export function PageHeading({ title, description }) {
  return (
    <header className="space-y-2">
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
        {title}
      </h1>
      <p className="max-w-2xl leading-relaxed text-muted-foreground">
        {description}
      </p>
    </header>
  );
}
