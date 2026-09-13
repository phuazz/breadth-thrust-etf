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
            // Contrast, not only size. An inline colour survives a client that
            // strips the stylesheet, which is why it is inline — and that same
            // inline colour ignores the dark-theme rule unless it is overridden.
            // Measuring it here is the only thing that catches the second case.
            const rgb = s => (s.match(/[\d.]+/g)||[]).map(Number);
            const lum = ([r,g,b]) => {
              const f = v => { v/=255; return v<=0.03928 ? v/12.92 : ((v+0.055)/1.055)**2.4; };
              return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b);
            };
            const ground = el => {
              for (let n=el; n; n=n.parentElement) {
                const c = rgb(getComputedStyle(n).backgroundColor);
                if (c.length>=3 && (c[3]===undefined || c[3]>0)) return c;
              }
              return rgb(getComputedStyle(document.body).backgroundColor);
            };
            const texts = visible.filter(e => [...e.childNodes].some(n => n.nodeType===3 && n.textContent.trim()));
            const ratios = texts.map(e => {
              const style = getComputedStyle(e), size = parseFloat(style.fontSize);
              const a = lum(rgb(style.color)) + 0.05, b = lum(ground(e)) + 0.05;
              const large = size>=24 || (size>=18.66 && parseInt(style.fontWeight,10)>=700);
              return {ratio: Math.max(a,b)/Math.min(a,b), floor: large?3:4.5,
                      text: e.textContent.trim().slice(0,40)};
            });
            const failed = ratios.filter(r => r.ratio < r.floor - 0.01);
            return {viewport,scrollWidth:document.documentElement.scrollWidth,overflow,
              minFont:Math.min(...fonts),charsPerLine:Number(chars(document)),
              minContrast:Number(Math.min(...ratios.map(r=>r.ratio)).toFixed(2)),
              contrastFailures:failed.map(r=>`${r.text} (${r.ratio.toFixed(2)}:1)`),
              positions:document.querySelectorAll('.position').length,
              background:getComputedStyle(document.body).backgroundColor};
            function chars(doc){
              const p = doc.querySelector('main p');
              const c = doc.createElement('canvas').getContext('2d');
              c.font = getComputedStyle(p).font;
              const sample = 'The quick brown fox jumps over the lazy dog. ';
              return (p.getBoundingClientRect().width / (c.measureText(sample).width/sample.length)).toFixed(1);
            }
          });
          results.push({file,width,theme,...result});
          if(result.viewport!==width || result.scrollWidth>width+1 || result.overflow || result.minFont<11
             || result.contrastFailures.length)
            throw new Error(JSON.stringify(results.at(-1)));
          const measure = width===390 ? [40,50] : [65,75];
          if(result.charsPerLine<measure[0] || result.charsPerLine>measure[1])
            throw new Error('Reading measure outside target: '+JSON.stringify(results.at(-1)));
          if(width===390) await page.screenshot({path:path.join(folder,file+'.'+theme+'.png'),fullPage:true});
        }
        if(width===390) await page.screenshot({path:path.join(folder,file+'.png'),fullPage:true});
        await context.close();
      }
    }
    fs.writeFileSync(path.join(folder,'mobile-measurements.json'),JSON.stringify(results,null,2));
    console.log(JSON.stringify(results));
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
