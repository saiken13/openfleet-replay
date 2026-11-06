import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { onUnauthorized, type User } from "../lib/auth";

type AuthState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "signed-in"; user: User };

interface AuthApi {
  user: User | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthApi>({
  user: null,
  login: async () => {},
  logout: async () => {},
});

export const useAuth = () => useContext(AuthContext);

/** Resolves the session (GET /auth/me) and shows the login page until there is one.
 * Any later 401 from the API (expired or revoked session) returns to the login page. */
export function AuthProvider({ children, login: loginPage }: { children: ReactNode; login: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  useEffect(() => {
    api.auth.me().then(
      (user) => setState({ status: "signed-in", user }),
      () => setState({ status: "anonymous" }),
    );
    return onUnauthorized(() => setState({ status: "anonymous" }));
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const user = await api.auth.login(username, password);
    setState({ status: "signed-in", user });
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.auth.logout();
    } finally {
      setState({ status: "anonymous" });
    }
  }, []);

  if (state.status === "loading") return <div className="p-8 text-sm text-slate-500">Loading…</div>;
  const user = state.status === "signed-in" ? state.user : null;
  return (
    <AuthContext.Provider value={{ user, login, logout }}>
      {user ? children : loginPage}
    </AuthContext.Provider>
  );
}
