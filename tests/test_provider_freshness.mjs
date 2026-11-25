import test from 'node:test';
import assert from 'node:assert/strict';
import {anchorCommercialClock, commercialState, commercialMessage} from '../app/static/provider_freshness.mjs';
import {visibleRecommendationReasons} from '../app/static/recommendations.mjs';

function activity() {
  return anchorCommercialClock({
    opening:{as_of:'2027-02-22T23:59:50+01:00'},
    provider_freshness:{price:{state:'current', checked_on:'2026-11-24', expires_at:'2027-02-23T00:00:00+01:00'}},
  },1000);
}

test('a current price expires at the Budapest deadline using monotonic elapsed time',()=>{
  assert.equal(commercialState(activity(),'price',10999),'current');
  assert.equal(commercialState(activity(),'price',11000),'expired');
});
test('pending requests and offline states cannot reset the commercial clock',()=>{
  const a=activity();a._openingSnapshot={phase:'checking',startedAt:10999};
  anchorCommercialClock(a,10999);
  assert.equal(commercialState(a,'price',11001),'expired');
});
test('missing or invalid server anchors cannot establish a current commercial fact',()=>{
  const a=activity();delete a._commercialClock;
  assert.equal(commercialState(a,'price',1000),'unconfirmed');
  assert.equal(commercialState(activity(),'price',999),'unconfirmed');
  assert.equal(commercialState({},'price',1000),'missing');
});
test('expired and unconfirmed server statuses do not become current after a retry',()=>{
  for(const state of ['expired','unconfirmed']){
    const a=activity();a.provider_freshness.price.state=state;
    assert.equal(commercialState(a,'price',1000),state);
    assert.match(commercialMessage(a,'price',1000),/needs rechecking/);
  }
});
test('expired pricing explanations disappear while unrelated search reasons remain',()=>{
  const a=activity();a.recommendation={basis:[{text:'Name matches your search.'},{text:'Published price fits.',commercial_field:'price'}]};
  assert.deepEqual(visibleRecommendationReasons(a,10999,false),['Name matches your search.','Published price fits.']);
  assert.deepEqual(visibleRecommendationReasons(a,11000,false),['Name matches your search.']);
});
