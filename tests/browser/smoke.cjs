const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

const root=process.env.OFFSCRIPT_BASE_URL;
if(!root) throw new Error('OFFSCRIPT_BASE_URL is required; use the isolated container check.');
const reports=path.resolve(process.env.CI_OUTPUT_DIR || '.local/ci','reports');
fs.mkdirSync(reports,{recursive:true});

async function settled(page){
  await page.waitForFunction(()=>document.querySelector('#activity-grid')?.getAttribute('aria-busy')==='false');
}
async function change(page,action){
  await Promise.all([page.waitForResponse(r=>new URL(r.url()).pathname==='/api/activities'),action()]);
  await settled(page);
}
async function ready(page){
  await page.waitForFunction(()=>document.querySelector('#startup-update')?.hidden&&!document.querySelector('#new-plan')?.disabled);
  await settled(page);
}
async function openAnotherDetail(page,currentId){
  await page.getByRole('button',{name:'Close activity details',exact:true}).click();
  const id=await page.locator('[data-detail]').evaluateAll((nodes,current)=>nodes.find(n=>n.dataset.detail!==current)?.dataset.detail,currentId);
  assert.ok(id);
  await page.locator(`[data-detail="${id}"]`).first().click();
  await page.locator('#detail-dialog[open]').waitFor();
  return page.locator('#detail-title').innerText();
}
async function reviewResponseChecks(page,context,today){
  for(const scenario of ['edited-draft','closed-modal','another-activity']){
    let started,release,saved;
    const held=new Promise(resolve=>started=resolve),responseRelease=new Promise(resolve=>release=resolve);
    await page.route('**/api/activities/mecca-day/reviews',async route=>{
      if(route.request().method()==='GET')return route.fulfill({json:Array.from({length:100},(_,i)=>({id:900000+i,nickname:'Other QA '+i,rating:4,text:'Synthetic review for the display limit check.',visited_on:today}))});
      const response=await route.fetch();assert.equal(response.status(),200);
      saved=(await response.json()).review;started();await responseRelease;await route.fulfill({response});
    });
    try{
      await page.goto(root+'/?activity=mecca-day');await page.locator('#detail-dialog[open]').waitFor();
      const submitted='Submitted content for the delayed '+scenario+' check.';
      await page.locator('#review-form [name=nickname]').fill('Delayed review QA');
      await page.locator('#review-form [name=visited_on]').fill(today);
      await page.locator('#review-form [name=text]').fill(submitted);
      await page.locator('#review-form [type=submit]').click();await held;
      const draft='Unsaved edits must survive an older response.';
      let otherTitle;
      if(scenario==='closed-modal')await page.getByRole('button',{name:'Close activity details',exact:true}).click();
      else{
        if(scenario==='another-activity')otherTitle=await openAnotherDetail(page,'mecca-day');
        await page.locator('#review-form [name=text]').fill(draft);
      }
      const completed=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/mecca-day/reviews'));
      release();await completed;
      await page.getByText('Thanks — your submitted review is published.',{exact:true}).waitFor();
      if(scenario==='closed-modal')assert.equal(await page.locator('#detail-dialog').evaluate(e=>e.open),false);
      else{
        assert.equal(await page.locator('#detail-dialog').evaluate(e=>e.open),true);
        assert.equal(await page.locator('#review-form [name=text]').inputValue(),draft);
        if(otherTitle)assert.equal(await page.locator('#detail-title').innerText(),otherTitle);
        else{
          assert.equal(await page.locator('.review').count(),100);
          assert.equal(await page.locator('.review').first().getAttribute('data-review-id'),String(saved.id));
          assert.equal(await page.locator('.review').first().locator('p').innerText(),submitted);
        }
      }
    }finally{release();await page.unroute('**/api/activities/mecca-day/reviews');}
  }
  for(const scenario of ['closed-modal','another-activity']){
    await page.goto(root+'/?activity=mecca-day');await page.locator('#detail-dialog[open]').waitFor();
    await page.locator('#review-form [name=nickname]').fill('Background failure QA');
    await page.locator('#review-form [name=visited_on]').fill(today);
    await page.locator('#review-form [name=text]').fill('This submission will receive a delayed failure.');
    let started,release;
    const held=new Promise(resolve=>started=resolve),responseRelease=new Promise(resolve=>release=resolve);
    await page.route('**/api/activities/mecca-day/reviews',async route=>{
      if(route.request().method()!=='POST')return route.continue();
      started();await responseRelease;await route.fulfill({status:503,json:{detail:'Storage is temporarily unavailable.'}});
    });
    try{
      await page.locator('#review-form [type=submit]').click();await held;
      if(scenario==='closed-modal')await page.getByRole('button',{name:'Close activity details',exact:true}).click();
      else{
        await openAnotherDetail(page,'mecca-day');
        await page.locator('#review-form [name=text]').fill('Retain this other draft after the failed background save.');
      }
      release();
      await page.locator('#toast').filter({hasText:'Could not confirm your submission. Storage is temporarily unavailable.'}).waitFor();
      if(scenario==='closed-modal')assert.equal(await page.locator('#detail-dialog').evaluate(e=>e.open),false);
      else{
        assert.equal(await page.locator('#review-form [name=text]').inputValue(),'Retain this other draft after the failed background save.');
        assert.equal(await page.locator('#review-form .form-error').innerText(),'');
      }
    }finally{release();await page.unroute('**/api/activities/mecca-day/reviews');}
  }
  await page.goto(root+'/?activity=mecca-day');await page.locator('#detail-dialog[open]').waitFor();
  const retryNickname='Retry QA '+Date.now();
  await page.locator('#review-form [name=nickname]').fill(retryNickname);
  await page.locator('#review-form [name=visited_on]').fill(today);
  await page.locator('#review-form [name=text]').fill('Retain this review after the failed request.');
  await page.route('**/api/activities/mecca-day/reviews',route=>route.request().method()==='POST'?route.fulfill({status:503,json:{detail:'Storage is temporarily unavailable.'}}):route.continue());
  await page.locator('#review-form [type=submit]').click();
  await page.locator('#review-form .form-error').filter({hasText:'temporarily unavailable'}).waitFor();
  assert.equal(await page.locator('#review-form [name=text]').inputValue(),'Retain this review after the failed request.');
  assert.equal(await page.locator('#review-form [type=submit]').isEnabled(),true);
  await page.unroute('**/api/activities/mecca-day/reviews');
  await page.locator('#review-form [type=submit]').click();
  await page.locator('.review').filter({has:page.locator('strong',{hasText:retryNickname})}).locator('p').filter({hasText:'Retain this review after the failed request.'}).waitFor();
  assert.equal(await page.locator('#review-form [name=text]').inputValue(),'');
  const rows=await(await context.request.get(root+'/api/activities/mecca-day/reviews')).json();
  assert.equal(rows.filter(r=>r.nickname===retryNickname).length,1);
  console.log('PASS review responses: new drafts, closed/switched dialogs, older reviews beyond the display limit, background failures and failed-save retry');
}
async function suggestionResponseChecks(context,errors){
  const config=await(await context.request.get(root+'/api/config')).json();
  const created=await context.request.post(root+'/api/plans',{data:{title:'Delayed suggestion check',nickname:'Suggestion QA',day:config.today,use_today:true}});
  assert.equal(created.status(),201);const plan=await created.json();
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
  let started,release;
  const held=new Promise(resolve=>started=resolve),responseRelease=new Promise(resolve=>release=resolve);
  const path='**/api/plans/'+plan.id+'/candidates';
  await page.route(path,async route=>{
    const response=await route.fetch();assert.equal(response.status(),201);
    started();await responseRelease;await route.fulfill({response});
  });
  try{
    await page.goto(root+'/?plan='+plan.id+'&activity=mecca-day');await page.locator('#detail-dialog[open]').waitFor();
    await page.locator('[data-suggest]').click();await held;
    const title=await openAnotherDetail(page,'mecca-day');
    await page.locator('#review-form [name=text]').fill('Keep the draft in this other activity.');
    const completed=page.waitForResponse(r=>r.url().endsWith('/'+plan.id+'/candidates'));
    release();await completed;
    await page.getByText('Added to your plan. Let the voting begin.',{exact:true}).waitFor();
    assert.equal(await page.locator('#detail-dialog').evaluate(e=>e.open),true);
    assert.equal(await page.locator('#detail-title').innerText(),title);
    assert.equal(await page.locator('#review-form [name=text]').inputValue(),'Keep the draft in this other activity.');
    assert.equal((await(await context.request.get(root+'/api/plans/'+plan.id)).json()).candidates.length,1);
  }finally{release();await page.close();}
  console.log('PASS delayed suggestions: saved candidate without closing another activity or discarding its review draft');
}
async function startupChecks(page,context){
  let failConfig=true,configCalls=0;
  await page.route('**/api/config',route=>{
    configCalls++;
    if(failConfig){failConfig=false;return route.fulfill({status:503,json:{detail:'Storage is temporarily unavailable.'}});}
    return route.continue();
  });
  await page.goto(root);
  await page.locator('#retry-startup').waitFor();
  assert.equal(await page.locator('#new-plan').isDisabled(),true);
  assert.equal(await page.locator('#explore-view').evaluate(e=>e.inert),true);
  await page.locator('#retry-startup').click();await ready(page);
  assert.equal(configCalls,2);
  failConfig=true;
  await page.reload();await page.locator('#retry-startup').waitFor();
  await page.evaluate(()=>{window.dispatchEvent(new Event('online'));window.dispatchEvent(new Event('online'));});
  await ready(page);assert.equal(configCalls,4);
  await page.unroute('**/api/config');

  let release;
  const held=new Promise(resolve=>release=resolve);
  await page.route('**/api/config',async route=>{await held;await route.abort().catch(()=>{});});
  try{
    await page.reload();
    assert.equal(await page.locator('#new-plan').isDisabled(),true);
    await page.locator('#startup-message').filter({hasText:'Connection timed out'}).waitFor({timeout:15000});
  }finally{release();await page.unroute('**/api/config');}
  await page.locator('#retry-startup').click();await ready(page);

  const config=await(await context.request.get(root+'/api/config')).json();
  const created=await context.request.post(root+'/api/plans',{data:{title:'Startup recovery check',nickname:'Startup QA',day:config.today,use_today:true}});
  assert.equal(created.status(),201);
  const plan=await created.json();let failPlan=true;
  const planRoute='**/api/plans/'+plan.id;
  await page.route(planRoute,route=>failPlan?route.fulfill({status:503,json:{detail:'Storage is temporarily unavailable.'}}):route.continue());
  await page.goto(root+'/?plan='+plan.id);
  await page.locator('#retry-startup').waitFor();
  assert.equal(await page.locator('#new-plan').isDisabled(),true);
  assert.equal(await page.locator('#back-to-plan').getAttribute('href'),'/plan/'+plan.id);
  failPlan=false;await page.locator('#retry-startup').click();await ready(page);
  assert.equal(await page.locator('#active-plan-banner strong').innerText(),plan.title);
  await page.unroute(planRoute);
  console.log('PASS startup: manual/online recovery, duplicate-event guard, bounded timeout and preserved plan context');
}

(async()=>{
  const browser=await chromium.launch({headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await context.tracing.start({screenshots:true,snapshots:true});
  let friendContext;
  try{
    await startupChecks(page,context);
    const reference=await context.newPage(),referenceErrors=[],requests=[];
    reference.on('console',message=>{if(message.type()==='error') referenceErrors.push(message.text());});
    await reference.route('**/*',route=>{
      requests.push(route.request().url());
      return new URL(route.request().url()).origin===new URL(root).origin?route.continue():route.abort();
    });
    for(const route of ['/docs','/redoc']){
      await reference.goto(root+route);
      assert.equal(await reference.locator('h1').innerText(),'Offscript Budapest API');
      const endpoint=reference.locator('.operation').filter({has:reference.locator('summary code',{hasText:'/api/activities'})}).first();
      await endpoint.locator('summary').click();
      assert.match(await endpoint.innerText(),/max_price/);
      assert.ok(await reference.locator('#models').isVisible());
    }
    assert.ok(requests.every(url=>new URL(url).origin===new URL(root).origin));
    assert.deepEqual(referenceErrors,[]);
    await reference.close();
    console.log('PASS API references: both documentation URLs render under CSP with all external requests blocked');
    const info=await(await context.request.get(root+'/api/activities/overture-64ec40bf-c520-4656-9152-5f78d059c904-place')).json();
    const currentPrice=info.provider_freshness.price.state==='current';
    await page.goto(root);await settled(page);
    await ready(page);
    for(const width of [320,360,390,768,1440]){
      await page.setViewportSize({width,height:1000});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    }
    assert.equal(await page.locator('html').getAttribute('lang'),'en');
    await change(page,()=>page.locator('[data-when=anytime]').click());
    await change(page,()=>page.locator('[name=q]').fill('De Lanna'));
    assert.equal(await page.locator('.activity-card').count(),1);
    await change(page,()=>page.locator('[name=max_price]').selectOption('10000'));
    assert.equal(await page.locator('.activity-card').count(),0);
    await change(page,()=>page.locator('[name=max_price]').selectOption(currentPrice?'20000':''));
    assert.equal(await page.locator('.activity-card').count(),1);
    await page.locator('.card-title').click();
    await page.locator('#detail-dialog[open]').waitFor();
    assert.match(await page.locator('.meta-price').innerText(),currentPrice?/12,900 Ft/:/Price needs rechecking/);
    assert.match(await page.locator('.detail-meta').innerText(),/60 min session/);
    assert.match(await page.locator('.source-note').innerText(),/CDLA-Permissive-2.0/);
    assert.equal(await page.getByRole('link',{name:'Choose branch, treatment & time'}).getAttribute('href'),'https://de-lanna-thai-kft.salonic.hu/');
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.ok(await page.locator('#detail-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth));
    await page.screenshot({path:path.join(reports,'browser-mobile.png')});

    await page.locator('#detail-dialog [data-suggest]').click();
    await page.locator('#create-plan-form [name=nickname]').fill('Browser host');
    await page.locator('#create-plan-form [name=title]').fill('CI browser plan');
    let attemptedCreation;
    await page.route('**/api/plans',async route=>{
      attemptedCreation=route.request().postDataJSON();
      await route.fulfill({status:422,json:{detail:'Choose another date or idea.'}});
    });
    await page.locator('#create-plan-form [type=submit]').click();
    await page.locator('#create-plan-form .form-error').filter({hasText:'Choose another date'}).waitFor();
    assert.equal(attemptedCreation.activity_id,info.id);
    assert.equal(await page.locator('#create-plan-form [name=nickname]').inputValue(),'Browser host');
    assert.equal(await page.locator('#create-plan-form [name=title]').inputValue(),'CI browser plan');
    await page.unroute('**/api/plans');
    await page.locator('#create-plan-form [type=submit]').click();
    await page.waitForURL('**/plan/**');
    await page.locator('.candidate').waitFor();
    const invite=page.url();
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>window.copiedShareLink=text}}));
    await page.locator('[data-detail]').first().click();
    await page.locator('#detail-dialog[open]').waitFor();
    await page.locator('#review-form [name=nickname]').fill('Preserved share draft');
    await page.locator('#detail-dialog [data-share]').click();
    const activityLink=new URL('/?activity='+info.id,root).href;
    await page.waitForFunction(expected=>window.copiedShareLink===expected,activityLink);
    assert.equal(page.url(),invite);
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new DOMException('Permission denied','NotAllowedError');}}}));
    await page.locator('#detail-dialog [data-share]').click();
    await page.locator('#share-dialog[open]').waitFor();
    assert.equal(await page.locator('#share-url').inputValue(),activityLink);
    assert.equal(await page.locator('#share-url').evaluate(e=>e.selectionEnd-e.selectionStart),activityLink.length);
    await page.locator('#share-url').evaluate(e=>e.setSelectionRange(0,0));
    await page.locator('#select-share-link').click();
    assert.equal(await page.locator('#share-url').evaluate(e=>e.selectionEnd-e.selectionStart),activityLink.length);
    assert.ok(await page.locator('#share-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth));
    assert.equal(page.url(),invite);
    await page.getByRole('button',{name:'Close sharing',exact:true}).click();
    assert.equal(await page.locator('#detail-dialog [data-share]').evaluate(e=>e===document.activeElement),true);
    assert.equal(await page.locator('#review-form [name=nickname]').inputValue(),'Preserved share draft');
    await page.getByRole('button',{name:'Close activity details',exact:true}).click();
    await page.locator('#copy-plan').click();await page.locator('#share-dialog[open]').waitFor();
    assert.equal(await page.locator('#share-url').inputValue(),invite);
    assert.equal(page.url(),invite);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#share-dialog').evaluate(e=>e.open),false);
    assert.equal(await page.locator('#copy-plan').evaluate(e=>e===document.activeElement),true);
    await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:undefined}));
    await page.locator('#copy-plan').click();await page.locator('#share-dialog[open]').waitFor();
    assert.equal(await page.locator('#share-url').inputValue(),invite);
    assert.equal(page.url(),invite);
    await page.getByRole('button',{name:'Close sharing',exact:true}).click();
    console.log('PASS sharing: successful copy, denied/missing clipboard, preserved route, draft and modal focus');
    const review=await context.newPage();
    review.on('pageerror',error=>errors.push(error.message));
    await review.goto(root+'/?activity=mecca-day');
    await review.locator('#detail-dialog[open]').waitFor();
    const reviewDate=(await(await context.request.get(root+'/api/clock')).json()).today;
    const reviewNickname='Browser QA '+Date.now();
    const reviewText='<img src=x onerror=window.reviewXss=true> Automated browser validation, not a real venue review.';
    await review.locator('#review-form [name=nickname]').fill(reviewNickname);
    await review.locator('#review-form [name=visited_on]').fill(reviewDate);
    await review.locator('#review-form [name=rating]').selectOption('4');
    await review.locator('#review-form [name=text]').fill(reviewText);
    await review.locator('#review-form [type=submit]').click();
    const ownReview=review.locator('.review').filter({has:review.locator('strong',{hasText:reviewNickname})});
    await ownReview.waitFor();
    assert.equal(await ownReview.locator('p').innerText(),reviewText);
    assert.equal(await ownReview.locator('img').count(),0);
    assert.notEqual(await review.evaluate(()=>window.reviewXss),true);
    await review.locator('#review-form [name=nickname]').fill(reviewNickname);
    await review.locator('#review-form [name=visited_on]').fill(reviewDate);
    await review.locator('#review-form [name=rating]').selectOption('3');
    await review.locator('#review-form [name=text]').fill('Updated automated validation, not a real venue review.');
    await review.locator('#review-form [type=submit]').click();
    await ownReview.locator('p').filter({hasText:'Updated automated validation'}).waitFor();
    assert.equal(await ownReview.count(),1);
    assert.match(await ownReview.locator('.review-meta').innerText(),/3\/5/);
    await reviewResponseChecks(review,context,reviewDate);
    await review.close();
    console.log('PASS reviews: original publish, safe HTML display and same-guest update without duplicate rows');
    await suggestionResponseChecks(context,errors);
    friendContext=await browser.newContext();
    const friend=await friendContext.newPage();
    friend.on('pageerror',error=>errors.push(error.message));
    await friend.goto(invite);
    await friend.locator('#join-plan').click();
    await friend.locator('#join-plan-form [name=nickname]').fill('Browser friend');
    await friend.locator('#join-plan-form [type=submit]').click();
    await friend.waitForFunction(()=>document.querySelectorAll('.person').length===2);
    await friend.locator('[data-vote][data-choice=yes]').click();
    await friend.waitForFunction(()=>document.querySelector('[data-choice=yes]')?.getAttribute('aria-pressed')==='true');
    await page.locator('#refresh-plan').click();
    await page.waitForFunction(()=>document.querySelector('[data-choice=yes]')?.textContent.includes('1'));

    // An older refresh must not overwrite conflict recovery after the host decides.
    let releaseRefresh,refreshStarted;
    const oldRefreshHeld=new Promise(resolve=>refreshStarted=resolve);
    const oldRefreshRelease=new Promise(resolve=>releaseRefresh=resolve);
    let holdNextRead=true;
    await friend.route('**/api/plans/*',async route=>{
      if(route.request().method()!=='GET'||!holdNextRead){await route.continue();return;}
      holdNextRead=false;
      const oldResponse=await route.fetch();
      refreshStarted();await oldRefreshRelease;
      await route.fulfill({response:oldResponse});
    });
    await friend.locator('#refresh-plan').click();
    await oldRefreshHeld;
    await page.locator('[data-pick]').click();
    await page.locator('.decided-label').waitFor();
    await friend.locator('[data-vote][data-choice=maybe]').click();
    await friend.locator('.decided-label').waitFor();
    releaseRefresh();
    await friend.waitForLoadState('networkidle');
    await friend.unroute('**/api/plans/*');
    assert.equal(await friend.locator('[data-vote]:enabled').count(),0);

    await friend.route('**/api/plans/*',route=>route.fulfill({status:503,contentType:'text/html',body:'<h1>Service unavailable</h1>'}));
    await friend.locator('#refresh-plan').click();
    await friend.locator('#plan-refresh-error').waitFor();
    assert.match(await friend.locator('#plan-refresh-error').innerText(),/last loaded votes/);
    assert.equal(await friend.locator('.decided-label').count(),1);
    assert.equal(await friend.locator('[data-vote]:enabled').count(),0);
    await friend.unroute('**/api/plans/*');
    await friend.locator('#refresh-plan').click();
    await friend.locator('#plan-refresh-error').waitFor({state:'detached'});
    await page.setViewportSize({width:1440,height:1000});
    await page.screenshot({path:path.join(reports,'browser-desktop.png')});
    assert.deepEqual(errors,[]);
    console.log('PASS browser: budget, detail, mobile, atomic suggestion, preserved drafts, invite, voting, stale-read/conflict recovery and HTML outage recovery');
  }catch(error){
    await page.screenshot({path:path.join(reports,'browser-failure.png')}).catch(()=>{});
    throw error;
  }finally{
    await context.tracing.stop({path:path.join(reports,'browser-trace.zip')});
    await friendContext?.close();
    await context.close();
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1)});
