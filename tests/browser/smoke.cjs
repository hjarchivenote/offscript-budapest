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

(async()=>{
  const browser=await chromium.launch({headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await context.tracing.start({screenshots:true,snapshots:true});
  let friendContext;
  try{
    const info=await(await context.request.get(root+'/api/activities/overture-64ec40bf-c520-4656-9152-5f78d059c904-place')).json();
    const currentPrice=info.provider_freshness.price.state==='current';
    await page.goto(root);await settled(page);
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
