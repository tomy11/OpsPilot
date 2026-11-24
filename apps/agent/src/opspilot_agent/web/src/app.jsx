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
const chatKey = "opspilot-chat"; // the open chat, so a refresh brings it back

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
    sessionStorage.removeItem(chatKey);
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
  const [chat, setChat] = useState(null);
  const [restoring, setRestoring] = useState(false);
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

  const openChat = useCallback(
    async (id, signal) => {
      const { turns: saved } = await api("/agent/conversations/" + id, {
        signal,
      });
      setTurns(saved.map((turn) => ({ id: nextTurn.current++, ...turn })));
      setChat(id);
      setMessage("");
    },
    [api],
  );

  useEffect(() => {
    const saved = JSON.parse(sessionStorage.getItem(chatKey) ?? "null");
    if (saved?.user !== user.id) return;
    const controller = new AbortController();
    setRestoring(true);
    openChat(saved.chat, controller.signal)
      .catch(() => {
        if (!controller.signal.aborted) sessionStorage.removeItem(chatKey);
      })
      .finally(() => {
        if (!controller.signal.aborted) setRestoring(false);
      });
    return () => controller.abort();
  }, [openChat, user.id]);

  useEffect(() => {
    if (chat)
      sessionStorage.setItem(
        chatKey,
        JSON.stringify({ user: user.id, chat }),
      );
  }, [chat, user.id]);

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
        body: { message: prompt, conversation_id: chat },
      });
      setChat(run.conversation_id);
      if (run.credits) setCredits(run.credits.balance);
      setTurns((current) =>
        current.map((turn) => (turn.id === id ? { ...turn, run } : turn)),
      );
    } catch (error) {
      // Deleted in another tab: the next attempt starts a new chat.
      if (error.status === 404) forgetChat();
      setTurns((current) =>
        current.map((turn) =>
          turn.id === id ? { ...turn, error: error.message } : turn,
        ),
      );
    } finally {
      setBusy(false);
    }
  }

  function forgetChat() {
    sessionStorage.removeItem(chatKey);
    setChat(null);
  }

  function resetChat() {
    if (busy) return;
    forgetChat();
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
          busy={busy || restoring}
          api={api}
          chat={chat}
          openChat={openChat}
          onDeleted={(id) => {
            if (id === chat) resetChat();
          }}
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
