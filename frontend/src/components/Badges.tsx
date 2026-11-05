import type { NodeState, RobotStatus, RunStatus, Severity, TopicStatus } from "../api/types";

const tone = {
  green: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  amber: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  red: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
  slate: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
  cyan: "bg-cyan-500/15 text-cyan-300 ring-cyan-500/30",
};
type Tone = keyof typeof tone;

const TONES: Record<string, Tone> = {
  ONLINE: "green", HEALTHY: "green", RUNNING: "green", SUCCESS: "green", RESOLVED: "green",
  DEGRADED: "amber", WARNING: "amber", INVESTIGATING: "amber",
  ERROR: "red", FAILED: "red", CRITICAL: "red", STOPPED: "red", OPEN: "red",
  OFFLINE: "slate", UNKNOWN: "slate", ABORTED: "slate", INFO: "cyan",
};

export function Badge({ value, className = "" }: { value: string; className?: string }) {
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-semibold ring-1 ring-inset ${tone[TONES[value] ?? "slate"]} ${className}`}
    >
      {value}
    </span>
  );
}

export const StatusBadge = ({ status }: { status: RobotStatus | null }) => <Badge value={status ?? "UNKNOWN"} />;
export const SeverityBadge = ({ severity }: { severity: Severity }) => <Badge value={severity} />;
export const RunStatusBadge = ({ status }: { status: RunStatus }) => <Badge value={status} />;

const DOT: Record<string, string> = {
  green: "bg-emerald-400", amber: "bg-amber-400", red: "bg-rose-500", slate: "bg-slate-500", cyan: "bg-cyan-400",
};

export function Dot({ value }: { value: TopicStatus | NodeState | RobotStatus | string }) {
  const t = TONES[value] ?? "slate";
  return <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${DOT[t]}`} title={value} />;
}
