import { useEffect, useRef, useState } from "react";
import { api, wsUrl } from "../api/client";
import type { ServerMessage } from "../api/types";

export type SocketState = "connecting" | "open" | "closed";

/**
 * Subscribe to a realtime channel with automatic reconnect.
 * `onOpen` runs on every (re)connect: the caller reloads current state over REST there,
 * because WS messages are only deltas (ARCHITECTURE §8).
 */
export function useLiveSocket(
  path: string | null,
  onMessage: (msg: ServerMessage) => void,
  onOpen?: () => void,
): SocketState {
  const [state, setState] = useState<SocketState>("connecting");
  const handlers = useRef({ onMessage, onOpen });
  handlers.current = { onMessage, onOpen };

  useEffect(() => {
    if (!path) return;
    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let attempt = 0;
    let stopped = false;

    const connect = () => {
      setState("connecting");
      ws = new WebSocket(wsUrl(path));
      ws.onopen = () => {
        attempt = 0;
        setState("open");
        handlers.current.onOpen?.();
      };
      ws.onmessage = (ev) => {
        try {
          handlers.current.onMessage(JSON.parse(ev.data) as ServerMessage);
        } catch {
          /* ignore malformed */
        }
      };
      ws.onclose = () => {
        setState("closed");
        if (stopped) return;
        // Browsers hide why a handshake was refused; if the session ended, this 401s and
        // the app returns to the login page (lib/auth.ts).
        api.auth.me().catch(() => {});
        const delay = Math.min(10_000, 500 * 2 ** attempt++);
        retry = window.setTimeout(connect, delay);
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, [path]);

  return state;
}

/** Re-render periodically (for "x seconds ago" labels). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
