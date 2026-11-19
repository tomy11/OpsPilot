import { useCallback, useEffect, useRef, useState } from "react";

import { Loading } from "@/components/feedback";
import { Workspace } from "@/components/workspace";
import { ActivityPage } from "@/features/activity";
import { AgentPage } from "@/features/agent";
import { ApprovalsPage } from "@/features/approvals";
import { Login } from "@/features/login";
import { request } from "@/lib/api";

const tokenKey = "opspilot-token";

export function App() {
  const [savedToken] = useState(() => sessionStorage.getItem(tokenKey));
  const [session, setSession] = useState(null);
  const [restoring, setRestoring] = useState(Boolean(savedToken));

  const logout = useCallback(() => {
    sessionStorage.removeItem(tokenKey);
    setSession(null);
  }, []);

  useEffect(() => {
    if (!savedToken) return;
    const controller = new AbortController();
    request("/session/me", { token: savedToken, signal: controller.signal })
      .then((user) => setSession({ token: savedToken, user }))
      .catch(() => {
        if (!controller.signal.aborted) logout();
      })
      .finally(() => {
        if (!controller.signal.aborted) setRestoring(false);
      });
    return () => controller.abort();
  }, [savedToken, logout]);

  async function login(body) {
    const { access_token: token } = await request("/session/login", {
      method: "POST",
      body,
    });
    const user = await request("/session/me", { token });
    sessionStorage.setItem(tokenKey, token);
    setSession({ token, user });
  }

  if (restoring)
    return (
      <main className="grid min-h-dvh place-items-center">
        <Loading>กำลังเปิด OpsPilot</Loading>
      </main>
    );
  if (!session) return <Login onLogin={login} />;
  return <SignedIn key={session.token} {...session} logout={logout} />;
}

function SignedIn({ token, user, logout }) {
  const [page, setPage] = useState("agent");
  const [message, setMessage] = useState("");
  const [turns, setTurns] = useState([]);
  const [busy, setBusy] = useState(false);
  const nextTurn = useRef(0);
  const [approval, setApproval] = useState(null);
  const [auditTarget, setAuditTarget] = useState(null);

  const api = useCallback(
    async (path, options) => {
      try {
        return await request(path, { ...options, token });
      } catch (error) {
        if (error.status === 401 && sessionStorage.getItem(tokenKey) === token)
          logout();
        throw error;
      }
    },
    [token, logout],
  );

  function runAgent() {
    const prompt = message.trim();
    if (busy || !prompt) return;
    const id = nextTurn.current++;
    setMessage("");
    setTurns((current) => [...current, { id, prompt }]);
    send(id, prompt);
  }

  // A failed turn keeps its prompt, so the user can resend it or pull it back to edit.
  function retryTurn(id) {
    const turn = turns.find((item) => item.id === id);
    if (busy || !turn?.error) return;
    setTurns((current) =>
      current.map((item) => (item.id === id ? { id, prompt: item.prompt } : item)),
    );
    send(id, turn.prompt);
  }

  function editTurn(id) {
    const turn = turns.find((item) => item.id === id);
    if (busy || !turn?.error) return;
    setTurns((current) => current.filter((item) => item.id !== id));
    setMessage(turn.prompt);
  }

  async function send(id, prompt) {
    setBusy(true);
    try {
      const run = await api("/agent/runs", {
        method: "POST",
        body: { message: prompt },
      });
      setTurns((current) =>
        current.map((turn) => (turn.id === id ? { ...turn, run } : turn)),
      );
    } catch (error) {
      setTurns((current) =>
        current.map((turn) =>
          turn.id === id ? { ...turn, error: error.message } : turn,
        ),
      );
    } finally {
      setBusy(false);
    }
  }

  function resetChat() {
    if (busy) return;
    setTurns([]);
    setMessage("");
  }

  function navigate(next) {
    if (next === "activity") setAuditTarget(null);
    setPage(next);
  }

  return (
    <Workspace user={user} page={page} onNavigate={navigate} onLogout={logout}>
      {page === "agent" && (
        <AgentPage
          message={message}
          setMessage={setMessage}
          onRun={runAgent}
          onReset={resetChat}
          onRetry={retryTurn}
          onEdit={editTurn}
          busy={busy}
          turns={turns}
          openApproval={(id) => {
            setApproval(id);
            setPage("approvals");
          }}
        />
      )}
      {page === "approvals" && (
        <ApprovalsPage
          api={api}
          user={user}
          selected={approval}
          onSelect={setApproval}
          openActivity={(id) => {
            setAuditTarget(id);
            setPage("activity");
          }}
        />
      )}
      {page === "activity" && user.role !== "staff" && (
        <ActivityPage
          key={auditTarget ?? "all"}
          api={api}
          target={auditTarget}
        />
      )}
    </Workspace>
  );
}
