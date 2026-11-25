// Anchor expiry to a server timestamp and elapsed time, never the device's date.
export function anchorCommercialClock(activity, startedAt = performance.now()) {
  if (!activity._commercialClock) activity._commercialClock = {
    serverTime:Date.parse(activity.opening?.as_of), startedAt,
  };
  return activity;
}

export function commercialState(activity, field, now = performance.now()) {
  const status = activity.provider_freshness?.[field];
  if (!status) return 'missing';
  if (status.state !== 'current') return status.state;
  const expires = Date.parse(status.expires_at);
  const clock = activity._commercialClock;
  if (!Number.isFinite(expires) || !Number.isFinite(clock?.serverTime) || !Number.isFinite(clock?.startedAt) || now < clock.startedAt) return 'unconfirmed';
  return clock.serverTime + now - clock.startedAt >= expires ? 'expired' : 'current';
}

export function commercialMessage(activity, field, now = performance.now()) {
  const status = activity.provider_freshness?.[field];
  if (!status || !['expired','unconfirmed'].includes(commercialState(activity, field, now))) return '';
  const label = field === 'price' ? 'Price' : 'Student offer';
  return status.message || `${label} needs rechecking.${status.checked_on ? ` Last checked ${status.checked_on}.` : ''}`;
}
