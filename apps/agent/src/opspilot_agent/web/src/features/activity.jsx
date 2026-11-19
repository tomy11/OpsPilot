import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { ErrorAlert, Loading } from "@/components/feedback";
import { PageHeading } from "@/components/workspace";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatTime } from "@/lib/api";

export function ActivityPage({ api, target }) {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const path =
      "/audit" + (target ? "?target=" + encodeURIComponent(target) : "");
    api(path, { signal: controller.signal })
      .then(setEvents)
      .catch((err) => {
        if (!controller.signal.aborted) setError(err.message);
      });
    return () => controller.abort();
  }, [api, target, revision]);

  function refresh() {
    setError("");
    setRevision((value) => value + 1);
  }

  return (
    <>
      <PageHeading
        title="Activity"
        description="ตรวจว่าใครเป็นผู้ขอ ผู้อนุมัติ และระบบบันทึกผลอย่างไร"
      />
      <Card className="shadow-sm">
        <CardHeader className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>
            <h2 className="text-lg">Audit Log</h2>
          </CardTitle>
          <Button variant="outline" onClick={refresh}>
            <RefreshCw aria-hidden="true" /> รีเฟรช
          </Button>
        </CardHeader>
        <CardContent className="space-y-3">
          <ErrorAlert message={error} />
          {target && (
            <p className="break-all text-sm text-muted-foreground">
              คำขอ {target}
            </p>
          )}
          {!events && !error && <Loading>กำลังโหลด Audit Log</Loading>}
          {events?.length === 0 && (
            <p className="text-muted-foreground">ยังไม่มีกิจกรรม</p>
          )}
          {events && (
            <ul className="divide-y">
              {events.map((event) => (
                <li
                  key={event.id}
                  className="flex flex-wrap justify-between gap-x-4 gap-y-1 py-3 [overflow-wrap:anywhere]"
                >
                  <div>
                    <p className="font-medium">
                      {event.action} · {event.status}
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {event.actor}
                    </p>
                  </div>
                  <time
                    dateTime={event.created_at}
                    className="text-sm tabular-nums text-muted-foreground"
                  >
                    {formatTime(event.created_at)}
                  </time>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </>
  );
}
