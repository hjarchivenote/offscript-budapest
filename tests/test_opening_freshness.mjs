import assert from 'node:assert/strict';
import {test} from 'node:test';
import {openingPhase} from '../app/static/opening_freshness.mjs';

function activity(overrides = {}) {
  return {
    opening:{state:'open', as_of:'2026-11-24T17:00:00+01:00', until:'2026-11-24T18:00:00+01:00'},
    _openingSnapshot:{phase:'fresh', startedAt:1000},
    ...overrides,
  };
}

test('an opening calculation expires after one minute, independent of the visitor time zone', () => {
  assert.equal(openingPhase(activity(), 60999, true), 'fresh');
  assert.equal(openingPhase(activity(), 61000, true), 'expired');
});

test('a passed closing time invalidates a recent calculation', () => {
  const a = activity(); a.opening.until = '2026-11-24T17:00:20+01:00';
  assert.equal(openingPhase(a, 20999, true), 'fresh');
  assert.equal(openingPhase(a, 21000, true), 'expired');
});

test('a session that fits now cannot still claim to fit after the last possible start', () => {
  const a = activity({visit_now:{state:'fits', finishes_at:'2026-11-24T17:59:50+01:00', usable_until:'2026-11-24T18:00:00+01:00'}});
  assert.equal(openingPhase(a, 11000, true), 'fresh');
  assert.equal(openingPhase(a, 11001, true), 'expired');
});

test('offline information stays unavailable even while the previous calculation is recent', () => {
  assert.equal(openingPhase(activity(), 2000, false), 'offline');
});

test('pending updates and failed updates cannot revive old time-sensitive labels', () => {
  for (const phase of ['checking', 'failed', 'expired', 'offline']) {
    const a = activity({_openingSnapshot:{phase, startedAt:1000}});
    assert.equal(openingPhase(a, 2000, true), phase);
    assert.equal(openingPhase(a, 90000, true), phase);
  }
});

test('a successful retry receives its own fresh request time', () => {
  const a = activity({_openingSnapshot:{phase:'failed', startedAt:1000}});
  assert.equal(openingPhase(a, 90000, true), 'failed');
  a._openingSnapshot = {phase:'fresh', startedAt:90000};
  assert.equal(openingPhase(a, 90001, true), 'fresh');
});

test('missing or invalid calculation timestamps cannot claim current status', () => {
  const a = activity(); delete a.opening.as_of;
  assert.equal(openingPhase(a, 2000, true), 'expired');
  a.opening.as_of = 'not a date';
  assert.equal(openingPhase(a, 2000, true), 'expired');
  delete a._openingSnapshot;
  assert.equal(openingPhase(a, 2000, true), 'expired');
});

test('a response that was in flight too long is already expired when it arrives', () => {
  assert.equal(openingPhase(activity(), 80000, true), 'expired');
});

test('closed and unknown states also expire rather than becoming permanent claims', () => {
  for (const state of ['closed', 'unknown']) {
    const a = activity({opening:{state, as_of:'2026-11-24T17:00:00+01:00'}});
    assert.equal(openingPhase(a, 61000, true), 'expired');
  }
});

test('cutoffs across midnight use elapsed time rather than calendar dates', () => {
  const a = activity({opening:{state:'open', as_of:'2026-11-24T23:59:50+01:00', until:'2026-11-25T00:00:10+01:00'}});
  assert.equal(openingPhase(a, 20999, true), 'fresh');
  assert.equal(openingPhase(a, 21000, true), 'expired');
});
