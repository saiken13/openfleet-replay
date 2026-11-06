import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import type { SocketState } from "./hooks/useLiveSocket";
import { ComparePage } from "./pages/ComparePage";
import { FleetPage } from "./pages/FleetPage";
import { IncidentPage } from "./pages/IncidentPage";
import { IncidentsPage } from "./pages/IncidentsPage";
import { RobotPage } from "./pages/RobotPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";

const ConnectionContext = createContext<(s: SocketState | null) => void>(() => {});

/** Pages with a live socket report its state to the header indicator. */
export function useConnection(state: SocketState) {
  const set = useContext(ConnectionContext);
  useEffect(() => {
    set(state);
    return () => set(null);
  }, [state, set]);
}

const NAV = [
  ["/", "Fleet"],
  ["/incidents", "Incidents"],
  ["/runs", "Runs"],
  ["/compare", "Compare"],
] as const;

function UserMenu() {
  const { user, logout } = useAuth();
  if (!user) return null;
  return (
    <span className="flex items-center gap-2 text-xs text-slate-400">
      <span>
        {user.username} <span className="text-slate-500">· {user.role.toLowerCase()}</span>
      </span>
      <button className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200" onClick={() => void logout()}>
        Sign out
      </button>
    </span>
  );
}

export default function App() {
  const [conn, setConn] = useState<SocketState | null>(null);
  return (
    <ConnectionContext.Provider value={setConn}>
      <header className="border-b border-slate-800 bg-slate-950/80">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
          <NavLink to="/" className="flex items-center gap-2 font-semibold">
            <span className="inline-block h-3 w-3 rounded-full bg-cyan-400 shadow-[0_0_12px] shadow-cyan-400/60" />
            OpenFleet <span className="text-slate-400">Replay</span>
          </NavLink>
          <nav className="flex gap-1 text-sm">
            {NAV.map(([to, label]) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) =>
                  `rounded px-3 py-1 ${isActive ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"}`
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>
          <span className="ml-auto" />
          {conn && (
            <span className="flex items-center gap-1.5 text-xs text-slate-400">
              <span
                className={`h-2 w-2 rounded-full ${conn === "open" ? "bg-emerald-400" : conn === "connecting" ? "bg-amber-400" : "bg-rose-500"}`}
              />
              {conn === "open" ? "live" : conn}
            </span>
          )}
          <UserMenu />
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <Routes>
          <Route path="/" element={<FleetPage />} />
          <Route path="/robots/:robotId" element={<RobotPage />} />
          <Route path="/incidents" element={<IncidentsPage />} />
          <Route path="/incidents/:incidentId" element={<IncidentPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="*" element={<div className="text-slate-400">Not found.</div>} />
        </Routes>
      </main>
    </ConnectionContext.Provider>
  );
}
