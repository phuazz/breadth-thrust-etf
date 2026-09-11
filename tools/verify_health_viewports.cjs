// Standalone browser verification against the locally served dashboard.
const {chromium} = require('playwright');
const fs=require('fs');
(async () => {
  fs.mkdirSync('logs',{recursive:true});
  const browser = await chromium.launch({headless:true,channel:'msedge'});
  const page = await browser.newPage();
  const runtimeErrors=[];
  page.on('pageerror',error=>runtimeErrors.push(error.message));
  page.on('requestfailed',request=>console.error('REQUEST FAILED',request.url(),request.failure()?.errorText));
  await page.goto('http://127.0.0.1:8765/docs/index.html');
  await page.waitForFunction(()=>typeof window.Plotly !== 'undefined');
  await page.locator('#health-tab-btn').waitFor();
  const tabs = await page.locator('[data-tab]').evaluateAll(es => es.map(e=>e.dataset.tab));
  const results=[];
  for(const width of [390,844,768,1280]) {
    await page.setViewportSize({width,height:width===844?390:844});
    for(const tab of tabs) {
      await page.locator(`[data-tab="${tab}"]`).click();
      if(tab==='data')await page.locator('#data-audit-body').waitFor({state:'visible'});
      // The page debounces viewport reflow by 150 ms; Plotly then resizes asynchronously.
      await page.waitForTimeout(400);
      const row=await page.evaluate(tab=>{
        const root=document.querySelector(`#tab-${tab}`);
        const visible=[...root.querySelectorAll('*')].filter(e=>e.checkVisibility() && e.textContent.trim() && !e.children.length);
        const overflow=visible.filter(e=>{
          if(e.getBoundingClientRect().right<=document.documentElement.clientWidth+1)return false;
          for(let p=e.parentElement;p&&p!==document.body;p=p.parentElement){
            if(/auto|scroll|hidden/.test(getComputedStyle(p).overflowX))return false;
          }
          return true;
        });
        const p=root.querySelector('p');
        const canvas=document.createElement('canvas');const ctx=canvas.getContext('2d');
        if(p)ctx.font=getComputedStyle(p).font;
        return {tab,viewport:innerWidth,client:document.documentElement.clientWidth,
          scroll:document.documentElement.scrollWidth,
          minFont:Math.min(...visible.map(e=>parseFloat(getComputedStyle(e).fontSize))),
          uncontainedOverflow:overflow.length,
          proseChars:p?Math.round(p.getBoundingClientRect().width/ctx.measureText('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ').width*52):null};
      },tab);
      results.push(row);
      if(tab==='health' && [390,1280].includes(width)) {
        await page.locator('#tab-health').scrollIntoViewIfNeeded();
        await page.screenshot({path:`logs/capture-health-${width}.png`});
      }
    }
  }
  fs.writeFileSync('logs/capture-viewports.json',JSON.stringify(results,null,2));
  console.log(JSON.stringify({measurements:results.length,
    health:results.filter(r=>r.tab==='health'),runtimeErrors,
    failures:results.filter(r=>r.client!==r.viewport || r.scroll>r.client+1 || r.minFont<11 || r.uncontainedOverflow>0)},null,2));
  if(runtimeErrors.length || results.some(r=>r.client!==r.viewport || r.scroll>r.client+1 || r.minFont<11 || r.uncontainedOverflow>0))process.exitCode=1;
  await browser.close();
})().catch(e=>{console.error(e);process.exit(1);});
