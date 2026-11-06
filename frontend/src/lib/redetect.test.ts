import { describe, expect, it } from "vitest";
import { overridesFrom } from "./redetect";

describe("overridesFrom", () => {
  const server = { cpu_high_pct: 90, topic_degraded_ratio: 0.7, battery_low_pct: 25 };

  it("sends only changed numeric fields", () => {
    expect(
      overridesFrom({ cpu_high_pct: "80", topic_degraded_ratio: "0.7", battery_low_pct: " 30 " }, server),
    ).toEqual({ cpu_high_pct: 80, battery_low_pct: 30 });
  });

  it("ignores blank and invalid input", () => {
    expect(overridesFrom({ cpu_high_pct: "", mem_high_pct: "abc", battery_low_pct: "  " }, server)).toEqual({});
  });

  it("sends a field the server value is unknown for", () => {
    expect(overridesFrom({ mem_high_pct: "50" }, server)).toEqual({ mem_high_pct: 50 });
  });
});
