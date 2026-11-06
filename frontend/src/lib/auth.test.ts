import { describe, expect, it, vi } from "vitest";
import { canTriage, hasRole, notifyUnauthorized, onUnauthorized } from "./auth";

describe("roles", () => {
  it("operators can triage, viewers and anonymous users cannot", () => {
    expect(canTriage({ username: "o", role: "OPERATOR" })).toBe(true);
    expect(canTriage({ username: "v", role: "VIEWER" })).toBe(false);
    expect(canTriage(null)).toBe(false);
  });

  it("roles include the ones below them", () => {
    expect(hasRole({ username: "o", role: "OPERATOR" }, "VIEWER")).toBe(true);
    expect(hasRole({ username: "v", role: "VIEWER" }, "OPERATOR")).toBe(false);
  });
});

describe("unauthorized notifications", () => {
  it("reaches every listener until it unsubscribes", () => {
    const a = vi.fn();
    const b = vi.fn();
    const offA = onUnauthorized(a);
    const offB = onUnauthorized(b);
    notifyUnauthorized();
    offA();
    notifyUnauthorized();
    offB();
    notifyUnauthorized();
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(2);
  });
});
