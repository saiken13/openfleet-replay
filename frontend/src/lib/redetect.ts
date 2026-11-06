import { REDETECT_THRESHOLDS, type RedetectOverrides, type RedetectThreshold, type RedetectVerdict } from "../api/types";

export const VERDICT_LABEL: Record<RedetectVerdict, string> = {
  both: "CAUGHT",
  recorded_only: "MISSED",
  replay_only: "NEW",
};

/**
 * Threshold form → request body: only fields the user changed from the server's values.
 * Blank or non-numeric fields fall back to the server value (they are not sent).
 */
export function overridesFrom(
  form: Partial<Record<RedetectThreshold, string>>,
  server: Partial<Record<RedetectThreshold, number>>,
): RedetectOverrides {
  const out: RedetectOverrides = {};
  for (const name of REDETECT_THRESHOLDS) {
    const raw = form[name]?.trim();
    if (!raw) continue;
    const value = Number(raw);
    if (!Number.isFinite(value) || value === server[name]) continue;
    out[name] = value;
  }
  return out;
}
