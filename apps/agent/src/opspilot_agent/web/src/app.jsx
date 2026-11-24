import { useCallback, useEffect, useRef, useState } from "react";

import { Loading } from "@/components/feedback";
import { Workspace } from "@/components/workspace";
import { ActivityPage } from "@/features/activity";
import { AgentPage } from "@/features/agent";
import { ApprovalsPage } from "@/features/approvals";
import { InvitePage } from "@/features/invite";
import { KnowledgePage } from "@/features/knowledge";
import { Login } from "@/features/login";
import { request } from "@/lib/api";

const tokenKey = "opspilot-token";

// Read once, then drop it from the address bar and history.
function takeInvite() {
  const match = /^#invite=([\w-]+)$/.exec(window.location.hash);
  if (!match) return null;
  window.history.replaceState(null, "", window.location.pathname);
  return match[1];
}

const firstInvite = takeInvite();

async function refreshToken(signal) {
  const { access_token: token } = await request("/session/refresh", {
    method: "POST",
    signal,
  });
  return token;
}

export function App() {
  const [invite, setInvite] = useState(firstInvite);
  const [session, setSession] = useState(null);
  const [restoring, setRestoring] = useState(!firstInvite);

  useEffect(() => {
    function onHashChange() {
      const token = takeInvite();
      if (token) setInvite(token);
    }
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const open = useCallback(async (token, signal) => {
    const user = await request("/session/me", { token, signal });
    sessionStorage.setItem(tokenKey, token);
    setSession({ token, user });
  }, []);

  const logout = useCallback(() => {
    sessionStorage.removeItem(tokenKey);
    setSession(null);
    // Ends the invite session too, if this browser has one.
    request("/session/logout", { method: "POST" }).catch(() => {});
  }, []);

  useEffect(() => {
    if (firstInvite) return;
    const controller = new AbortController();
    const { signal } = controller;
    async function restore() {
      const saved = sessionStorage.getItem(tokenKey);
      try {
        if (!saved) throw new Error("no saved token");
        await open(saved, signal);
      } catch {
        if (signal.aborted) return;
        sessionStorage.removeItem(tokenKey);
        try {
          await open(await refreshToken(signal), signal);
        } catch {
          // Not signed in: the login page shows.
        }
      } finally {
        if (!signal.aborted) setRestoring(false);
      }
    }
    restore();
    return () => controller.abort();
  }, [open]);

  async function login(body) {
    const { access_token: token } = await request("/session/login", {
      method: "POST",
      body,
    });
    await open(token);
  }

  async function acceptInvite() {
    const { access_token: token } = await request("/session/redeem", {
      method: "POST",
      body: { token: invite },
    });
    await open(token);
    setInvite(null);
  }

  if (invite)
    return (
      <InvitePage
        key={invite}
        onAccept={acceptInvite}
        onCancel={() => setInvite(null)}
      />
    );
  if (restoring)
    return (
      <main className="grid min-h-dvh place-items-center">
        <Loading>กำลังเปิด OpsPilot</Loading>
      </main>
    );
  if (!session) return <Login onLogin={login} />;
  return <SignedIn key={session.user.id} {...session} logout={logout} />;
}

function SignedIn({ token, user, logout }) {
  const [page, setPage] = useState("agent");
  const [message, setMessage] = useState("");
  const [turns, setTurns] = useState([]);
  const [busy, setBusy] = useState(false);
  const nextTurn = useRef(0);
  const [approval, setApproval] = useState(null);
  const [auditTarget, setAuditTarget] = useState(null);

  const tokenRef = useRef(token);
  const refreshing = useRef(null);
  const [credits, setCredits] = useState(user.credits);

  const api = useCallback(
    async (path, options) => {
      const used = tokenRef.current;
      try {
        return await request(path, { ...options, token: used });
      } catch (error) {
        if (error.status !== 401) throw error;
        if (!user.invited) {
          logout();
          throw error;
        }
      }
      // Invited users get short tokens; renew from the session cookie and retry once.
      try {
        if (tokenRef.current === used) {
          refreshing.current ??= refreshToken().finally(() => {
            refreshing.current = null;
          });
          tokenRef.current = await refreshing.current;
          sessionStorage.setItem(tokenKey, tokenRef.current);
        }
      } catch (error) {
        if (error.status === 401) logout();
        throw error;
      }
      return request(path, { ...options, token: tokenRef.current });
    },
    [user.invited, logout],
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
      current.map((item) =>
        item.id === id ? { id, prompt: item.prompt } : item,
      ),
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
      if (run.credits) setCredits(run.credits.balance);
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
          credits={credits}
          turns={turns}
          openApproval={(id) => {
            setApproval(id);
            setPage("approvals");
          }}
        />
      )}
      {page === "knowledge" && <KnowledgePage api={api} />}
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
