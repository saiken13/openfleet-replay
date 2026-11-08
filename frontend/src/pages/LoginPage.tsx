import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const [sso, setSso] = useState<{ name: string; login_url: string } | null>(null);

  useEffect(() => {
    api.auth.config().then(
      (c) => {
        setHint(c.login_hint);
        setSso(c.oidc);
      },
      () => {},
    );
    // A refused single sign-on comes back as /login?error=... (DECISIONS #038).
    const sp = new URLSearchParams(window.location.search);
    const ssoError = sp.get("error");
    if (ssoError) setError(`Sign-in failed: ${ssoError}.`);
    if (window.location.pathname === "/login") window.history.replaceState(null, "", "/");
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setError("Wrong username or password.");
      // Throttled (DECISIONS #027): the server says how long to wait.
      else if (err instanceof ApiError && err.status === 429) setError(`Too many sign-in attempts: ${err.message.replace(/^.*?; /, "")}.`);
      else setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-4 p-6">
        <div className="flex items-center gap-2 text-lg font-semibold">
          <span className="inline-block h-3 w-3 rounded-full bg-cyan-400 shadow-[0_0_12px] shadow-cyan-400/60" />
          OpenFleet <span className="text-slate-400">Replay</span>
        </div>
        <label className="block space-y-1 text-sm">
          <span className="text-slate-400">Username</span>
          <input
            className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2"
            autoComplete="username"
            autoFocus
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label className="block space-y-1 text-sm">
          <span className="text-slate-400">Password</span>
          <input
            type="password"
            className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && <div className="text-sm text-rose-300">{error}</div>}
        <button className="btn-primary w-full" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {sso && (
          <>
            <div className="flex items-center gap-3 text-xs text-slate-500">
              <span className="h-px flex-1 bg-slate-800" />
              or
              <span className="h-px flex-1 bg-slate-800" />
            </div>
            {/* A full navigation: the backend redirects to the provider and back. */}
            <a href={sso.login_url} className="btn block w-full text-center">
              Sign in with {sso.name}
            </a>
          </>
        )}
        {hint && <div className="rounded-md bg-slate-800/60 px-3 py-2 text-xs text-slate-400">{hint}</div>}
      </form>
    </div>
  );
}
