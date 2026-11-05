/**
 * Client-paced replay (DECISIONS #008). Holds a run's frames (sorted by robot time) and
 * releases them as playback time advances, at 1x/2x/4x... The server keeps no session.
 *
 * Seeking backwards resets the view and re-applies frames from the start up to the target
 * (the reducer is cheap, and this keeps state derivation in exactly one place).
 *
 * `now` and `schedule` are injectable so the clock is testable without real time.
 */
import type { Frame } from "../api/types";

export interface ReplayCallbacks {
  /** Frames to apply, in order. */
  onFrames: (frames: Frame[]) => void;
  /** Clear the view before re-applying from the start (backwards seek / restart). */
  onReset: () => void;
  /** Playback position changed (ms since the first frame) or play state changed. */
  onTick?: (positionMs: number, playing: boolean) => void;
}

export interface ReplayOptions {
  now?: () => number;
  schedule?: (fn: () => void) => () => void; // returns a cancel function
}

const defaultSchedule = (fn: () => void) => {
  const id = window.setInterval(fn, 50);
  return () => window.clearInterval(id);
};

export class ReplayClock {
  readonly frames: Frame[];
  readonly t0: number;
  readonly durationMs: number;
  private offsets: number[];
  private cursor = 0; // next frame index to emit
  private positionMs = 0; // playback position when last paused / seeked
  private playStartedAt: number | null = null; // wall time when play() was called
  private cancel: (() => void) | null = null;
  speed = 1;

  constructor(
    frames: Frame[],
    private cb: ReplayCallbacks,
    private opts: ReplayOptions = {},
  ) {
    this.frames = [...frames].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
    this.t0 = this.frames.length ? Date.parse(this.frames[0].ts) : 0;
    this.offsets = this.frames.map((f) => Date.parse(f.ts) - this.t0);
    this.durationMs = this.offsets.at(-1) ?? 0;
  }

  private now(): number {
    return (this.opts.now ?? (() => performance.now()))();
  }

  get playing(): boolean {
    return this.playStartedAt !== null;
  }

  get position(): number {
    if (this.playStartedAt === null) return this.positionMs;
    return Math.min(this.durationMs, this.positionMs + (this.now() - this.playStartedAt) * this.speed);
  }

  play(): void {
    if (this.playing) return;
    if (this.positionMs >= this.durationMs && this.frames.length) this.seek(0);
    this.playStartedAt = this.now();
    this.cancel = (this.opts.schedule ?? defaultSchedule)(() => this.tick());
    this.cb.onTick?.(this.position, true);
  }

  pause(): void {
    if (!this.playing) return;
    this.positionMs = this.position;
    this.playStartedAt = null;
    this.cancel?.();
    this.cancel = null;
    this.cb.onTick?.(this.positionMs, false);
  }

  setSpeed(speed: number): void {
    const wasPlaying = this.playing;
    if (wasPlaying) this.pause();
    this.speed = speed;
    if (wasPlaying) this.play();
  }

  /** Emit every frame up to the current position. Called by the scheduler. */
  tick(): void {
    const pos = this.position;
    this.emitUpTo(pos);
    if (pos >= this.durationMs) this.pause();
    else this.cb.onTick?.(pos, this.playing);
  }

  seek(positionMs: number): void {
    const target = Math.max(0, Math.min(this.durationMs, positionMs));
    const wasPlaying = this.playing;
    if (wasPlaying) this.pause();
    if (target < this.positionMs || target === 0) {
      this.cb.onReset();
      this.cursor = 0;
    }
    this.positionMs = target;
    this.emitUpTo(target);
    if (wasPlaying && target < this.durationMs) this.play();
    else this.cb.onTick?.(target, false);
  }

  restart(): void {
    this.seek(0);
  }

  dispose(): void {
    this.cancel?.();
    this.cancel = null;
    this.playStartedAt = null;
  }

  private emitUpTo(pos: number): void {
    let end = this.cursor;
    while (end < this.frames.length && this.offsets[end] <= pos) end++;
    if (end > this.cursor) {
      this.cb.onFrames(this.frames.slice(this.cursor, end));
      this.cursor = end;
    }
  }
}
