/* DESIGN REFERENCE ONLY. Reproduce offscreen artifact screenshots; does not connect to Runtime. */
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const playwrightPath = process.env.LEDGER_PLAYWRIGHT || 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright';
const { chromium } = require(playwrightPath);
const targets = [
 ['01-first-run.png','first-run',1440,900],
 ['02-overview.png','overview',1920,1080],
 ['03-project-detail.png','project',1440,900],
 ['04-session-detail.png','session',1440,900],
 ['05-explore.png','explore',1920,1080],
 ['06-settings.png','settings',1440,900],
 ['07-project-attribution.png','attribution',1440,900],
 ['08-data-confidence.png','confidence',1440,900],
 ['09-overview-1366.png','overview',1366,768],
 ['10-overview-1180.png','overview',1180,720]
];
const requested=process.argv.slice(2).filter(x=>x!=='--only');
if(requested.some(n=>!targets.some(t=>t[0]===n)))throw Error('Unknown screenshot target');
const captureTargets=requested.length?targets.filter(t=>requested.includes(t[0])):targets;
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.LEDGER_BROWSER||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const results=[];const errors=[];
 try {
  const context=await browser.newContext({deviceScaleFactor:1,locale:'zh-CN',timezoneId:'Asia/Shanghai',reducedMotion:'reduce'});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  for(const [name,view,width,height] of captureTargets){
   await page.setViewportSize({width,height});
   await page.goto(pathToFileURL(path.join(__dirname,'master.html')).href+'?view='+view);
   await page.evaluate(()=>document.fonts.ready);
   await page.screenshot({path:path.join(__dirname,name),fullPage:false});
   results.push({name,view,width,height,...await page.evaluate(()=>({horizontalOverflow:document.documentElement.scrollWidth>innerWidth,mainWidth:document.querySelector('.main')?.clientWidth,dialogs:document.querySelectorAll('[role=dialog]').length,buttons:document.querySelectorAll('button').length}))});
  }
  const previous=requested.length&&fs.existsSync(path.join(__dirname,'capture-report.json'))?JSON.parse(fs.readFileSync(path.join(__dirname,'capture-report.json'),'utf8')):{results:[]};
  const previousRefreshed=previous.pass==='Settings Local Rebuild + Global Minor Polish'?(previous.refreshed||[]):[];
  const refreshed=[...new Set([...previousRefreshed,...results.map(r=>r.name)])];
  const merged=targets.map(t=>results.find(r=>r.name===t[0])||previous.results.find(r=>r.name===t[0])).filter(Boolean);
  fs.writeFileSync(path.join(__dirname,'capture-report.json'),JSON.stringify({pass:'Settings Local Rebuild + Global Minor Polish',refreshed,retained:merged.filter(r=>!refreshed.includes(r.name)).map(r=>r.name),renderer:'Microsoft Edge · isolated headless artifact rendering',deviceScaleFactor:1,errors,results:merged},null,2));
  console.log(JSON.stringify({errors,results},null,2));
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
