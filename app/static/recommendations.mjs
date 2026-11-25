import {OPENING_REFRESH_MS, openingPhase} from './opening_freshness.mjs';
import {commercialState} from './provider_freshness.mjs';

export function visibleRecommendationReasons(activity, now = performance.now(), online = navigator.onLine) {
  const snapshot = activity._openingSnapshot;
  const fresh = online && snapshot?.phase === 'fresh' && now - snapshot.startedAt < OPENING_REFRESH_MS;
  const recommendation = activity.recommendation;
  const reasons = recommendation
    ? [...(recommendation.basis || []), ...(recommendation.filter_reasons || [])]
    : [];
  const visible = reasons.filter(reason => reason?.text && (!reason.commercial_field || commercialState(activity, reason.commercial_field, now) === 'current') && (
    !reason.time_sensitive || (fresh && (!reason.requires_opening_window || openingPhase(activity, now, online) === 'fresh'))
  )).map(reason => reason.text);
  return [...new Set(visible.length ? visible : ['Practical details need confirmation with the venue.'])];
}
