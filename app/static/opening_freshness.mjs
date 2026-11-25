export const OPENING_REFRESH_MS = 60000;
export const OPENING_TIMEOUT_MS = 10000;

// Use elapsed request time, not the visitor's clock, to age the server's calculation.
export function openingPhase(activity, now = performance.now(), online = navigator.onLine) {
  if (!online) return 'offline';
  const snapshot = activity._openingSnapshot;
  if (!snapshot) return 'expired';
  if (snapshot.phase !== 'fresh') return snapshot.phase;
  const calculated = Date.parse(activity.opening?.as_of);
  if (!Number.isFinite(calculated)) return 'expired';
  const age = Math.max(0, now - snapshot.startedAt);
  let lifetime = OPENING_REFRESH_MS;
  if (activity.opening.state === 'open' && activity.opening.until) {
    const until = Date.parse(activity.opening.until);
    if (!Number.isFinite(until)) return 'expired';
    lifetime = Math.min(lifetime, until - calculated);
  }
  const visit = activity.visit_now;
  if (visit?.state === 'fits' && visit.finishes_at && visit.usable_until) {
    const margin = Date.parse(visit.usable_until) - Date.parse(visit.finishes_at);
    if (!Number.isFinite(margin)) return 'expired';
    // Finishing exactly at closing is valid; a later start is not.
    lifetime = Math.min(lifetime, margin + 1);
  }
  return age < lifetime ? 'fresh' : 'expired';
}

export function openingUpdateMessage(phase) {
  if (phase === 'checking') return 'Checking current opening times…';
  if (phase === 'offline') return 'You are offline. Current opening times cannot be checked.';
  if (phase === 'failed') return 'Opening times could not be updated. Current status is unavailable.';
  return 'The previous opening check has expired. Current status needs updating.';
}
