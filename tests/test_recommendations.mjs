import assert from 'node:assert/strict';
import {test} from 'node:test';
import {visibleRecommendationReasons} from '../app/static/recommendations.mjs';

function activity() {
  return {
    _openingSnapshot:{phase:'fresh', startedAt:1000},
    opening:{state:'open', as_of:'2026-11-24T17:00:00+01:00', until:'2026-11-24T18:00:00+01:00'},
    recommendation:{
      basis:[
        {text:'Fits published hours now.',time_sensitive:true,requires_opening_window:true},
        {text:'Published price and visit timing.',time_sensitive:false},
      ],
      filter_reasons:[{text:'Within published hours now.',time_sensitive:true,requires_opening_window:true}],
    },
  };
}

test('fresh results explain both ranking and selected filters', () => {
  assert.deepEqual(visibleRecommendationReasons(activity(), 2000, true), [
    'Fits published hours now.', 'Published price and visit timing.', 'Within published hours now.',
  ]);
});

test('failed, expired and pending refreshes remove live claims while retaining published facts', () => {
  for (const phase of ['failed','expired','checking','offline']) {
    const a=activity();a._openingSnapshot.phase=phase;
    assert.deepEqual(visibleRecommendationReasons(a,2000,true),['Published price and visit timing.']);
  }
});

test('offline and minute-old results cannot repeat current availability in their explanations', () => {
  assert.deepEqual(visibleRecommendationReasons(activity(),2000,false),['Published price and visit timing.']);
  assert.deepEqual(visibleRecommendationReasons(activity(),61000,true),['Published price and visit timing.']);
});

test('a session’s latest start expires its recommendation as well as its badge', () => {
  const a=activity();
  a.visit_now={state:'fits',finishes_at:'2026-11-24T17:59:50+01:00',usable_until:'2026-11-24T18:00:00+01:00'};
  assert.ok(visibleRecommendationReasons(a,11000,true).includes('Fits published hours now.'));
  assert.deepEqual(visibleRecommendationReasons(a,11001,true),['Published price and visit timing.']);
});

test('a future-window explanation is independent of today’s closing boundary', () => {
  const a=activity();a.opening.until='2026-11-24T17:00:01+01:00';
  a.recommendation.basis.unshift({text:'Published hours cover your weekend.',time_sensitive:true,requires_opening_window:false});
  const visible=visibleRecommendationReasons(a,3000,true);
  assert.ok(visible.includes('Published hours cover your weekend.'));
  assert.ok(!visible.includes('Fits published hours now.'));
});

test('repeated explanations are deduplicated and missing metadata gets a cautious fallback', () => {
  const a=activity();a.recommendation.filter_reasons.push(a.recommendation.basis[1]);
  assert.equal(visibleRecommendationReasons(a,2000,true).filter(text=>text==='Published price and visit timing.').length,1);
  assert.deepEqual(visibleRecommendationReasons({},2000,true),['Practical details need confirmation with the venue.']);
});
