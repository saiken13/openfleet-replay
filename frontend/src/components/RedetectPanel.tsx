import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { REDETECT_THRESHOLDS, type RedetectResult, type RedetectThreshold } from "../api/types";
import { fmtDuration } from "../lib/format";
import { overridesFrom, VERDICT_LABEL } from "../lib/redetect";
import { Badge, SeverityBadge } from "./Badges";

type Form = Partial<Record<RedetectThreshold, string>>;

/** Re-detection replay (DECISIONS #023): would today's rules (or these thresholds) catch it? */
export function RedetectPanel({ runId, onSeek }: { runId: string; onSeek: (iso: string) => void }) {
  const [result, setResult] = useState<RedetectResult | null>(null);
  const [server, setServer] = useState<RedetectResult["config"] | null>(null);
  const [form, setForm] = useState<Form>({});
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(withOverrides: boolean) {
    setBusy(true);
    setError(null);
    try {
      const body = withOverrides && server ? overridesFrom(form, server) : {};
      const r = await api.redetect(runId, body);
      setResult(r);
      if (!server && !Object.keys(r.overrides).length) {
        setServer(r.config);
        setForm(Object.fromEntries(REDETECT_THRESHOLDS.map((k) => [k, String(r.config[k])])));
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const firstOpened = (key: string) => result?.incidents.find((i) => i.dedup_key === key)?.opened_at;
  const firstRecorded = (key: string) => result?.recorded.find((i) => i.dedup_key === key);

  return (
    <div className="card">
      <div className="card-h flex items-center gap-2">
        <span>Re-detection · would today's rules catch this run?</span>
      </div>
      <div className="space-y-3 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn-primary" disabled={busy} onClick={() => run(false)}>
            {busy ? "Replaying…" : result ? "Re-run current rules" : "Run current rules"}
          </button>
          {server && (
            <button className="btn" onClick={() => setShowForm((v) => !v)}>
              {showForm ? "Hide thresholds" : "Try other thresholds…"}
            </button>
          )}
          {result && (
            <span className="text-sm text-slate-400">
              {result.batches} batches ·{" "}
              <b className="text-emerald-300">{result.caught} caught</b> ·{" "}
              <b className={result.missed ? "text-rose-300" : "text-slate-300"}>{result.missed} missed</b> ·{" "}
              <b className={result.new ? "text-amber-300" : "text-slate-300"}>{result.new} new</b>
            </span>
          )}
        </div>

        {showForm && server && (
          <form
            className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5"
            onSubmit={(e) => {
              e.preventDefault();
              run(true);
            }}
          >
            {REDETECT_THRESHOLDS.map((k) => (
              <label key={k} className="text-[11px] text-slate-400">
                {k}
                <input
                  inputMode="decimal"
                  value={form[k] ?? ""}
                  onChange={(e) => setForm((f) => ({ ...f, [k]: e.target.value }))}
                  className={`mt-0.5 w-full rounded border bg-slate-950 px-2 py-1 font-mono text-xs text-slate-200 ${
                    form[k] !== undefined && Number(form[k]) !== server[k] ? "border-amber-500/60" : "border-slate-700"
                  }`}
                />
              </label>
            ))}
            <div className="col-span-full flex gap-2">
              <button type="submit" className="btn-primary" disabled={busy}>Re-run with these</button>
              <button
                type="button"
                className="btn"
                onClick={() => setForm(Object.fromEntries(REDETECT_THRESHOLDS.map((k) => [k, String(server[k])])))}
              >
                Reset to server values
              </button>
            </div>
          </form>
        )}

        {error && <div className="text-sm text-rose-300">{error}</div>}
        {result?.notes.map((n) => (
          <div key={n} className="text-xs text-slate-500">{n}</div>
        ))}
      </div>

      {result && (
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr>
              <th className="px-4 py-2">Verdict</th>
              <th className="px-2 py-2">Failure</th>
              <th className="px-2 py-2">Recorded live</th>
              <th className="px-2 py-2">Replay</th>
              <th className="px-4 py-2 text-right">First detected</th>
            </tr>
          </thead>
          <tbody>
            {result.keys.map((k) => {
              const opened = firstOpened(k.dedup_key);
              const recorded = firstRecorded(k.dedup_key);
              return (
                <tr key={k.dedup_key} className="table-row">
                  <td className="px-4 py-1.5">
                    <Badge value={VERDICT_LABEL[k.verdict]} />
                  </td>
                  <td className="px-2 py-1.5 font-mono text-xs">
                    {recorded ? (
                      <Link to={`/incidents/${recorded.id}`} className="hover:text-cyan-300">{k.dedup_key}</Link>
                    ) : (
                      k.dedup_key
                    )}
                  </td>
                  <td className="px-2 py-1.5">
                    {k.recorded_severity ? (
                      <span className="flex items-center gap-1">
                        <SeverityBadge severity={k.recorded_severity} />
                        {k.recorded_count > 1 && <span className="text-xs text-slate-500">×{k.recorded_count}</span>}
                      </span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </td>
                  <td className="px-2 py-1.5">
                    {k.replayed_severity ? (
                      <span className="flex items-center gap-1">
                        <SeverityBadge severity={k.replayed_severity} />
                        {k.replayed_count > 1 && <span className="text-xs text-slate-500">×{k.replayed_count}</span>}
                      </span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </td>
                  <td className="px-4 py-1.5 text-right font-mono text-xs">
                    {opened && k.first_replayed_offset_s !== null ? (
                      <button className="text-cyan-400 hover:underline" onClick={() => onSeek(opened)}>
                        +{fmtDuration(k.first_replayed_offset_s)}
                      </button>
                    ) : k.first_recorded_offset_s !== null ? (
                      <span className="text-slate-500">live +{fmtDuration(k.first_recorded_offset_s)}</span>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              );
            })}
            {!result.keys.length && (
              <tr>
                <td colSpan={5} className="px-4 py-3 text-xs text-slate-500">
                  No incidents, recorded or replayed.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
