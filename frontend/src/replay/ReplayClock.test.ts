import { describe, expect, it } from "vitest";
import type { Frame } from "../api/types";
import { ReplayClock } from "./ReplayClock";

const ts = (s: number) => new Date(Date.UTC(2026, 10, 5, 12, 0, 0) + s * 1000).toISOString();
const frame = (s: number): Frame => ({ kind: "event", ts: ts(s), data: { id: s } as never });

function harness(seconds: number[]) {
  let wall = 0;
  const emitted: number[] = [];
  let resets = 0;
  const clock = new ReplayClock(
    seconds.map(frame),
    {
      onFrames: (fs) => emitted.push(...fs.map((f) => (f.data as { id: number }).id)),
      onReset: () => {
        resets++;
        emitted.length = 0;
      },
    },
    { now: () => wall, schedule: () => () => {} },
  );
  return {
    clock,
    emitted,
    resets: () => resets,
    advance: (ms: number) => {
      wall += ms;
      clock.tick();
    },
  };
}

describe("ReplayClock", () => {
  it("releases frames as playback time passes, in robot-time order", () => {
    const h = harness([5, 0, 2, 10]); // unsorted input
    expect(h.clock.durationMs).toBe(10_000);
    h.clock.play();
    h.advance(0);
    expect(h.emitted).toEqual([0]);
    h.advance(2_000);
    expect(h.emitted).toEqual([0, 2]);
    h.advance(3_000);
    expect(h.emitted).toEqual([0, 2, 5]);
  });

  it("speed multiplies playback rate", () => {
    const h = harness([0, 4, 8]);
    h.clock.setSpeed(4);
    h.clock.play();
    h.advance(1_000); // 4s of robot time
    expect(h.emitted).toEqual([0, 4]);
  });

  it("pause freezes the position", () => {
    const h = harness([0, 3, 6]);
    h.clock.play();
    h.advance(1_000);
    h.clock.pause();
    h.advance(10_000);
    expect(h.emitted).toEqual([0]);
    expect(h.clock.position).toBe(1_000);
  });

  it("seeking backwards resets and re-applies up to the target", () => {
    const h = harness([0, 2, 4, 6, 8]);
    h.clock.seek(7_000);
    expect(h.emitted).toEqual([0, 2, 4, 6]);
    h.clock.seek(3_000);
    expect(h.resets()).toBe(1); // only the backwards seek resets
    expect(h.emitted).toEqual([0, 2]);
    h.clock.seek(9_000); // forward: no reset, just the missing frames
    expect(h.emitted).toEqual([0, 2, 4, 6, 8]);
  });

  it("stops at the end and play() restarts from the beginning", () => {
    const h = harness([0, 1]);
    h.clock.play();
    h.advance(5_000);
    expect(h.clock.playing).toBe(false);
    expect(h.emitted).toEqual([0, 1]);
    h.clock.play();
    h.advance(0);
    expect(h.emitted).toEqual([0]); // reset, replaying from the start
  });
});
