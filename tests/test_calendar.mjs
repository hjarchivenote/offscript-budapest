import assert from 'node:assert/strict';
import {test} from 'node:test';
import {budapestDay} from '../app/static/calendar.mjs';

test('the Budapest day changes at local midnight rather than UTC midnight', () => {
  assert.equal(budapestDay('2026-11-24T22:59:59Z'), '2026-11-24');
  assert.equal(budapestDay('2026-11-24T23:00:00Z'), '2026-11-25');
});

test('summer midnight uses the summer offset', () => {
  assert.equal(budapestDay('2026-07-04T21:59:59Z'), '2026-07-04');
  assert.equal(budapestDay('2026-07-04T22:00:00Z'), '2026-07-05');
});

test('both occurrences of the repeated autumn hour have the same date', () => {
  assert.equal(budapestDay('2026-10-25T02:30:00+02:00'), '2026-10-25');
  assert.equal(budapestDay('2026-10-25T02:30:00+01:00'), '2026-10-25');
});

test('the spring clock change does not invent another calendar day', () => {
  assert.equal(budapestDay('2027-03-28T01:30:00+01:00'), '2027-03-28');
  assert.equal(budapestDay('2027-03-28T03:30:00+02:00'), '2027-03-28');
});

test('month, leap-day and year boundaries use actual calendar dates', () => {
  assert.equal(budapestDay('2026-11-30T23:01:00Z'), '2026-12-01');
  assert.equal(budapestDay('2028-02-28T23:01:00Z'), '2028-02-29');
  assert.equal(budapestDay('2026-12-31T23:01:00Z'), '2027-01-01');
});

test('an offset from another country still identifies the Budapest day', () => {
  assert.equal(budapestDay('2026-11-24T15:00:00-08:00'), '2026-11-25');
  assert.equal(budapestDay('2026-11-25T08:00:00+09:00'), '2026-11-25');
});

test('missing, invalid or device-local timestamps cannot establish the current date', () => {
  for (const value of [null, undefined, '', 'invalid', '2026-11-25', '2026-11-25T00:01:00']) {
    assert.equal(budapestDay(value), null);
  }
});
