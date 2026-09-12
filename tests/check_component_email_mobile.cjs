/* Local rendered publication check. No external page or email is contacted. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');

(async () => {
  const folder = path.resolve(__dirname, '../.component-mail');
  const files = fs.readdirSync(folder).filter(x => /^rehearsal-.*\.html$/.test(x));
  const server = http.createServer((req, res) => {
    const name = decodeURIComponent(req.url.slice(1));
    if (!files.includes(name)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(fs.readFileSync(path.join(folder, name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  const results = [];
  try {
    for (const file of files) {
      for (const width of [390, 844, 768, 1280]) {
        const context = await browser.newContext({viewport:{width, height:width===844?390:844},
          isMobile:width===390 || width===844, deviceScaleFactor:1});
        const page = await context.newPage();
        await page.goto(`http://127.0.0.1:${server.address().port}/${file}`);
        for (const theme of ['light', 'dark']) {
          await page.evaluate(t => document.documentElement.dataset.theme=t, theme);
          const result = await page.evaluate(() => {
            const viewport = document.documentElement.clientWidth;
            const visible = [...document.querySelectorAll('main *')].filter(e => e.checkVisibility());
            const overflow = visible.filter(e => e.getBoundingClientRect().right>viewport+1 || e.getBoundingClientRect().left<0).length;
            const fonts = visible.filter(e=>e.textContent.trim()).map(e=>parseFloat(getComputedStyle(e).fontSize));
            const p = document.querySelector('main p');
            const c = document.createElement('canvas').getContext('2d');
            c.font = getComputedStyle(p).font;
            const sample = 'The quick brown fox jumps over the lazy dog. ';
            const chars = p.getBoundingClientRect().width / (c.measureText(sample).width/sample.length);
            return {viewport,scrollWidth:document.documentElement.scrollWidth,overflow,
              minFont:Math.min(...fonts),charsPerLine:Number(chars.toFixed(1)),
              positions:document.querySelectorAll('.position').length,
              background:getComputedStyle(document.body).backgroundColor};
          });
          results.push({file,width,theme,...result});
          if(result.viewport!==width || result.scrollWidth>width+1 || result.overflow || result.minFont<11)
            throw new Error(JSON.stringify(results.at(-1)));
        }
        if(width===390) await page.screenshot({path:path.join(folder,file+'.png'),fullPage:true});
        await context.close();
      }
    }
    fs.writeFileSync(path.join(folder,'mobile-measurements.json'),JSON.stringify(results,null,2));
    console.log(JSON.stringify(results));
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
