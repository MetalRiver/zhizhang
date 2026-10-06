const {chromium}=require(process.env.AUDIT_PLAYWRIGHT_PATH||'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const m=JSON.parse(fs.readFileSync(process.argv[2]));
const output=m.output;fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.AUDIT_EDGE_PATH});
 const ctx=await browser.newContext({viewport:{width:1180,height:720},timezoneId:'Asia/Shanghai'});
 const page=await ctx.newPage(),errors=[],checks=[];page.on('pageerror',e=>errors.push(e.message));
 const ready=async()=>page.waitForFunction(()=>state.booted);
 const json=async(url)=>ctx.request.get(url).then(r=>r.json());
 try{
  await page.goto(m.cache.base+'/');await ready();
  await page.getByRole('button',{name:'开始',exact:true}).click();
  await page.getByRole('button',{name:'继续',exact:true}).nth(0).click();   /* 数据位置：默认推荐 */
  await page.getByRole('button',{name:'继续',exact:true}).nth(0).click();   /* 后台方式：默认全不勾 */
  await page.getByRole('button',{name:'建立我的账本'}).click();
  await page.getByRole('button',{name:'查看总览',exact:true}).waitFor({timeout:60000});await page.getByRole('button',{name:'查看总览',exact:true}).click();
  await page.getByRole('heading',{name:'我的项目',exact:true}).waitFor();
  const q=await page.evaluate(()=>state.data);assert.equal(q.aggregate.total,0);assert.equal(q.aggregate.token_records,1);assert.equal(q.aggregate.costs.USD,.002);
  assert.equal(await page.locator('.project-token').first().innerText(),'0');assert((await page.locator('.project-table').textContent()).includes('已读取用量'));
  await page.locator('tr[data-project]').first().click();await page.getByRole('dialog',{name:'项目详情'}).waitFor();
  await page.locator('[data-disclosure="project-usage"] > summary').click();assert((await page.locator('.token-breakdown').innerText()).includes('1.0K'));
  await page.locator('.session-card').first().click();await page.getByRole('dialog',{name:'会话详情'}).waitFor();assert.equal(await page.locator('.detail-summary strong').first().innerText(),'0');
  await page.getByRole('button',{name:'查看完整使用记录',exact:true}).click();await page.waitForFunction(()=>state.view==='explore'&&state.recordData?.items.length===1);
  await page.locator('tr[data-explore-event]').first().click();assert(!(await page.locator('.event-evidence').innerText()).includes('只能确认使用过'));
  checks.push({name:'cache-only-overview-project-session-event',status:'PASS',total:0,cache:1000,cost:.002});
  await page.goto(m.writes.base+'/');await ready();await page.getByRole('button',{name:'全部历史',exact:true}).click();await page.waitForFunction(()=>state.data.filter.range==='all');
  const before=await json(m.writes.base+'/api/v1/query?range=all');
  await page.locator('tr[data-project]').first().click();await page.getByRole('dialog',{name:'项目详情'}).waitFor();
  await page.locator('.session-card').first().click();await page.getByRole('dialog',{name:'会话详情'}).waitFor();
  const sid=await page.evaluate(()=>state.chosenSession),oldKey=await page.evaluate(()=>state.sessions.find(s=>s.id===state.chosenSession).key);
  const target=before.by_project.find(p=>p.project_key&&p.project_key!==oldKey).project_key;
  await page.getByRole('button',{name:'归到正确项目',exact:true}).click();await page.getByLabel('目标项目').selectOption(target);
  await page.getByRole('button',{name:'确认移动',exact:true}).click();await page.getByRole('dialog',{name:'确认整理项目'}).waitFor();await page.locator('[data-action="confirm-attribution"]').click();
  await page.getByRole('dialog').waitFor({state:'hidden'});await page.waitForFunction(([s,k])=>state.sessions.some(x=>x.id===s&&x.key===k),[sid,target]);
  const assigned=await json(m.writes.base+'/api/v1/query?range=all&session='+encodeURIComponent(sid));assert(assigned.items.every(e=>e.project_key===target));
  checks.push({name:'assign-ui-real-http',status:'PASS',session:sid,target});
  // Select a real target, merge two projects in one UI action, verify all three writes completed.
  const candidates=await page.evaluate(()=>state.data.by_project.filter(p=>p.project_key));
  const mergeTarget=candidates[0].project_key,from=candidates.slice(1,3).map(p=>p.project_key);
  await page.locator(`tr[data-project="${mergeTarget}"]`).click();await page.getByRole('dialog',{name:'项目详情'}).waitFor();await page.getByRole('button',{name:'整理项目',exact:true}).click();await page.getByRole('button',{name:'合并重复项目',exact:true}).click();
  for(const key of from)await page.locator(`input[name="merge-projects"][value="${key}"]`).check();
  await page.getByRole('button',{name:'确认合并',exact:true}).click();await page.getByRole('dialog',{name:'确认整理项目'}).waitFor();await page.locator('[data-action="confirm-attribution"]').click();
  await page.getByRole('dialog').waitFor({state:'hidden'});await page.waitForFunction(keys=>keys.every(k=>!state.data.by_project.some(p=>p.project_key===k)),from);
  const after=await json(m.writes.base+'/api/v1/query?range=all');assert.equal(after.aggregate.events,before.aggregate.events);assert.equal(after.aggregate.total,before.aggregate.total);
  await page.reload();await ready();assert(from.every(k=>!after.by_project.some(p=>p.project_key===k)));
  checks.push({name:'multi-project-merge-ui-real-http',status:'PASS',sources:from,target:mergeTarget,events:after.aggregate.events,total:after.aggregate.total});
  assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({status:'PASS',checks,errors},null,2));console.log(JSON.stringify({status:'PASS',checks}));
 }catch(e){fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({status:'FAIL',checks,error:e.stack,errors},null,2));await page.screenshot({path:path.join(output,'failure.png')});throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
