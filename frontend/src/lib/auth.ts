/** Auth helpers shared by the API client and the UI (DECISIONS #022). */

export type UserRole = "VIEWER" | "OPERATOR";

export interface User {
  username: string;
  role: UserRole;
  site_id?: string | null; // the one site this user sees; null = every site (#039)
}

/** Sent on every API request. Cookie-authenticated writes (and login) are refused without
 * it, which a cross-site page can't add without a CORS preflight. */
export const CLIENT_HEADER = "X-OpenFleet-Client";

const RANK: Record<UserRole, number> = { VIEWER: 0, OPERATOR: 1 };

export function hasRole(user: User | null, role: UserRole): boolean {
  return user !== null && RANK[user.role] >= RANK[role];
}

export const canTriage = (user: User | null) => hasRole(user, "OPERATOR");

type Listener = () => void;
const listeners = new Set<Listener>();

/** Called by the API client on any 401: the session ended (expired, revoked, signed out
 * elsewhere). The auth provider listens and shows the login page. */
export function notifyUnauthorized(): void {
  for (const l of [...listeners]) l();
}

export function onUnauthorized(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
