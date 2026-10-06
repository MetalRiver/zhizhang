/* Artifact-level acceptance only. Does not load or mutate formal Runtime or production data. */
const {chromium}=require(process.env.LEDGER_PLAYWRIGHT||'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('node:fs');const path=require('node:path');const {pathToFileURL}=require('node:url');const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.LEDGER_BROWSER||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const checks=[];const errors=[];const add=(name,evidence)=>checks.push({name,status:'PASS',evidence});
 const context=await browser.newContext({viewport:{width:1440,height:900},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 const go=async(view='overview',query='')=>page.goto(pathToFileURL(path.join(__dirname,'master.html')).href+'?view='+view+query);
 try{
  for(const [width,height] of [[1920,1080],[1440,900],[1366,768],[1180,720]]){
   await page.setViewportSize({width,height});
   for(const view of ['first-run','overview','project','session','explore','settings','attribution','confidence']){
    await go(view);
    const layout=await page.evaluate(()=>{
     const selectors=['.main','.project-table-scroll','.drawer-content','.modal-body','.event-scroll'];
     const overflow=selectors.flatMap(sel=>[...document.querySelectorAll(sel)].filter(x=>x.scrollWidth>x.clientWidth+2).map(x=>({selector:sel,width:x.clientWidth,scroll:x.scrollWidth})));
     const foot=document.querySelector('.drawer-actions,.modal-actions');const r=foot?.getBoundingClientRect();
     const primary=[...document.querySelectorAll('.right-rail .panel:first-child .text-button')].map(x=>{const b=x.getBoundingClientRect(),p=x.closest('.panel').getBoundingClientRect();return b.bottom<=p.bottom+1});
     return{pageOverflow:document.documentElement.scrollWidth>innerWidth,overflow,footerVisible:!r||(r.bottom<=innerHeight&&r.top>=34),confidenceCTA:primary.every(Boolean)};
    });
    assert.equal(layout.pageOverflow,false,`${view} ${width} document overflow`);assert.deepEqual(layout.overflow,[],`${view} ${width} inner overflow`);assert(layout.footerVisible,`${view} ${width} fixed actions clipped`);assert(layout.confidenceCTA,`${view} ${width} confidence CTA clipped`);
   }
   add(`8 页面布局 / ${width}×${height}`,'无页面或内容横向截断；详情固定操作可见；可信度入口完整');
  }
  await page.setViewportSize({width:1440,height:900});await go('first-run');
  await page.getByRole('button',{name:'开始查找'}).click();assert(await page.getByRole('heading',{name:'已找到 5 个 AI 工具'}).isVisible());
  await page.getByRole('button',{name:'建立我的账本'}).click();await page.getByRole('heading',{name:'你的 AI 使用账本已经建立'}).waitFor();await page.getByRole('button',{name:'查看总览'}).click();assert(await page.getByRole('heading',{name:'最近 AI 都用在哪？',exact:true}).isVisible());add('First Run 闭环','欢迎 → 发现 → 首次扫描 → 完成 → 总览');
  const baseline=await page.evaluate(()=>aggregate(effective()));
  await page.locator('[data-disclosure="filters"] > summary').click();await page.getByLabel('AI 工具筛选').selectOption('CatPaw');const activity=await page.evaluate(()=>aggregate(effective()));assert.equal(activity.hasTokens,false);assert(activity.activity>0);assert(await page.locator('.kpis').innerText().then(t=>t.includes('暂无具体用量')));add('活动与 Token 隔离',`${activity.activity} 条活动；Token 与成本不可用`);
  await go();await page.getByLabel('时间范围').getByRole('button',{name:'今天',exact:true}).click();const today=await page.evaluate(()=>aggregate(effective()));assert(today.total<baseline.total);await page.getByLabel('项目筛选').selectOption('p_fox');const filtered=await page.evaluate(()=>aggregate(effective()));assert.equal(filtered.projectCount,1);await page.locator('[data-disclosure="filters"] > summary').click();await page.getByLabel('模型筛选').selectOption('glm-4.7');const model=await page.evaluate(()=>[...new Set(effective().map(e=>e.model))]);assert.deepEqual(model,['glm-4.7']);add('时间 / 项目 / 客户端 / 模型筛选','摘要、项目账单、趋势及模型同步过滤');
  await go();await page.locator('[data-project="p_fox"]').click();assert(await page.getByRole('dialog',{name:'项目详情'}).isVisible());
  await page.locator('.session-card').first().click();assert(await page.getByRole('dialog',{name:'会话详情'}).isVisible());await page.getByRole('button',{name:'查看完整使用记录'}).click();await page.locator('[data-explore-event]').first().click();assert(await page.getByRole('heading',{name:'这条使用记录的用量详情'}).isVisible());add('Project → Session → Event','项目最耗会话首项 → 会话详情 → 探索事件 → 来源与精确值');
  await go('explore');await page.locator('[data-explore-session="s_0_1"]').click();await page.locator('.audit summary').click();await page.getByLabel('高级审计口径').selectOption('raw');const raw=await page.locator('.event-foot').innerText();assert(raw.includes('/ 30 条'));await page.getByLabel('高级审计口径').selectOption('replay');assert((await page.locator('.event-foot').innerText()).includes('/ 14 条'));add('有效 / 原始 / 回放审计','16 有效 + 14 回放 = 30 原始；原始仅在高级审计出现');
  await go('attribution');const identity=await page.evaluate(()=>selectedProject().key);await page.getByLabel('项目显示名称').fill('狐写 · 个人项目');await page.getByRole('button',{name:'保存名称'}).click();assert.equal(await page.evaluate(()=>selectedProject().key),identity);assert((await page.locator('.project-table-scroll').innerText()).includes('狐写 · 个人项目'));add('Alias 稳定身份','改显示名，project_key 保持不变');
  await go('attribution','&tab=move');const beforeMove=await page.evaluate(()=>aggregate(effective()));await page.getByRole('button',{name:'预览移动'}).click();assert(await page.getByRole('dialog',{name:'确认整理项目'}).isVisible());await page.getByRole('button',{name:'确认移动'}).click();assert.equal(await page.evaluate(()=>sessions.filter(s=>s.project==='p_unassigned').length),0);assert.equal(await page.evaluate(()=>aggregate(effective()).total),beforeMove.total);add('待归项目 → 指定项目','选择 → 目标项目 → 预览 → 确认；全局 Token 不变');
  await go('attribution','&tab=merge');const beforeMerge=await page.evaluate(()=>aggregate(effective()));await page.locator('[name="merge-projects"]').first().check();await page.getByRole('button',{name:'预览合并'}).click();await page.getByRole('button',{name:'确认合并'}).click();const afterMerge=await page.evaluate(()=>aggregate(effective()));assert.equal(afterMerge.total,beforeMerge.total);assert.equal(afterMerge.projectCount,beforeMerge.projectCount-1);assert.equal(afterMerge.sessions,beforeMerge.sessions);add('合并不重复计量','总 Token、会话数保持；项目数量减少；原始来源身份保留');
  await go('confidence');await page.locator('[data-disclosure="confidence-technical"] > summary').click();assert((await page.locator('[role=dialog]').innerText()).includes('input + output'));assert(!(await page.locator('[role=dialog]').innerText()).includes('可信度 97%'));add('可信度解释','来源能力、定价分母、历史边界、重复回放与技术口径完整');
  for(const [scenario,text] of [['empty','这个范围内还没有使用记录'],['scan-error','暂时没能更新'],['partial-scan','部分工具还需要检查'],['ledger-unavailable','暂时无法打开账本'],['unknown-pricing','暂无价格'],['activity-only','暂无具体用量']]){await go('overview','&scenario='+scenario);assert((await page.locator('.main').innerText()).includes(text),scenario)}
  await go('first-run','&scenario=no-sources');assert(await page.getByRole('heading',{name:'还没找到支持的 AI 工具'}).isVisible());await go('confidence','&scenario=history-gap');assert((await page.locator('[role=dialog]').innerText()).includes('有历史记录缺口'));add('8 个边界状态','空记录、扫描错误、部分扫描、账本不可用、未定价、仅活动、未发现来源、历史缺口');
  await go();const invariants=await page.evaluate(()=>({totalConsistent:records.every(e=>e.total==null||e.total===e.input+e.output),activityNull:records.filter(e=>e.activity).every(e=>e.total===null&&e.cost===null),currencyKeys:Object.keys(aggregate(effective()).costs),costNull:records.some(e=>e.total!=null&&e.cost===null),originalIdentity:records.filter(e=>!e.unknown).every(e=>e.originalProject)}));assert(invariants.totalConsistent&&invariants.activityNull&&invariants.costNull&&invariants.originalIdentity);assert.deepEqual(invariants.currencyKeys,['CNY','USD']);add('冻结数据语义',invariants);

  await go('project');assert.equal(await page.locator('.token-breakdown').isVisible(),false);await page.locator('[data-disclosure="project-usage"] > summary').click();assert(await page.locator('.token-breakdown').isVisible());await page.locator('[data-disclosure="project-technical"] > summary').click();assert((await page.locator('[data-disclosure="project-technical"]').innerText()).includes('p_fox'));add('项目技术信息逐层展开','默认会话与工具优先；用量组成、模型分布、原识别身份均可追溯');
  await go('session');assert.equal(await page.locator('.token-breakdown').isVisible(),false);await page.locator('[data-disclosure="session-usage"] > summary').click();assert(await page.locator('.token-breakdown').isVisible());await page.locator('[data-disclosure="session-technical"] > summary').click();assert((await page.locator('[data-disclosure="session-technical"]').innerText()).includes('Session identity'));add('会话技术信息保留','输入、输出、缓存与助手、首尾时间、来源身份完整');
  await go('explore');assert.deepEqual(await page.locator('.event-table th').allTextContents(),['时间','AI 工具','模型','使用量','估算成本']);assert.equal(await page.getByLabel('高级审计口径').isVisible(),false);await page.locator('[data-explore-event]').first().click();await page.locator('[data-disclosure="event-technical"] > summary').click();assert((await page.locator('[data-disclosure="event-technical"]').innerText()).includes('cache_read_tokens'));add('探索轻量默认 + 完整审计','5 列默认记录，逐条展开精确值与原始字段；RAW/REPLAY 在高级层保留');
  await go();await page.locator('.kpi-money .info > summary').click();assert((await page.locator('.kpi-money .info-pop').innerText()).includes('不代表你的实际账单'));add('估算语义清楚','成本说明可鼠标与键盘打开，明确公开 API 等价估算与币种不换汇');
  await go('settings');assert.equal(await page.locator('[data-disclosure="settings-identity"] .disclosure-body').isVisible(),false);await page.locator('[data-setting="advanced"]').click();await page.locator('[data-disclosure="settings-identity"] > summary').click();assert((await page.locator('[data-disclosure="settings-identity"]').innerText()).includes('Build ID'));add('设置工程信息下沉','默认常用；Schema、Runtime、Build ID、SQLite 在高级展开中');
  for(const [scenario,title] of [['loading','正在打开你的账本'],['scanning','正在更新你的账本'],['success','账本已更新'],['unknown','有些记录暂时无法识别'],['unassigned','这些记录还没有找到所属项目'],['disabled','自动更新尚未启用']]){await go('overview','&scenario='+scenario);assert((await page.locator('.main').innerText()).includes(title));const card=page.locator('.state-card,.state-notice').first();assert(await card.locator('.icon').count());assert(await card.locator('button:not(:disabled)').count());assert(await card.locator('p').count());}add('状态有图标、解释与下一步','Loading、Scanning、Success、Unknown、Unassigned、Disabled 均可明确继续；加上既有 Empty/No source/Partial/Error/No pricing');
  await go();const locked=await page.evaluate(()=>({nav:[...document.querySelectorAll('.nav button')].map(b=>b.textContent.trim()),total:aggregate(effective()).total,currencies:aggregate(effective()).costs,mode:state.audit}));assert.deepEqual(locked.nav,['总览','探索','设置']);assert.equal(locked.total,29252192);assert.equal(locked.mode,'effective');add('锁定结构与用量守恒',locked);
  await go('attribution');assert.equal(await page.locator('[data-disclosure="alias-technical"] .disclosure-body').isVisible(),false);add('普通界面不暴露 project_key','高级信息可展开，不删除项目稳定身份');

  // 暗色归档（archive/dark-v1）已确认淘汰并移除，对应一致性检查随之删除。
  const stateEvidence=[];
  for(const scenario of ['loading','scanning','success','empty','no-sources','partial-scan','scan-error','no-pricing','unknown','unassigned','disabled']){await go('overview','&scenario='+scenario);const card=page.locator(scenario==='no-sources'?'.welcome':'.state-card,.state-notice').first();assert(await card.locator('.icon').count(),scenario+' icon');assert(await card.locator('h1,h2,h3,strong').count(),scenario+' title');assert(await card.locator('p').count(),scenario+' explanation');assert(await card.locator('button:not(:disabled)').count(),scenario+' next action');stateEvidence.push(scenario)}add('11 类状态逐项检查',stateEvidence);
  await go();const costSummary=page.locator('.kpi-money .info > summary');await costSummary.focus();await page.keyboard.press('Enter');assert(await page.locator('.kpi-money .info-pop').isVisible());add('成本说明键盘可达','信息按钮获得焦点后 Enter 展开说明');
  await page.setViewportSize({width:1180,height:720});await go('explore');const columns=await page.locator('.explore-space').evaluate(el=>getComputedStyle(el).gridTemplateColumns.split(' ').length);assert.equal(columns,2);add('1180 探索结构','顶部项目选择，下方会话与记录两列，保留当前路径');
  const settingIds=['common','sources','scan','attribution','privacy','about','advanced'];
  const settingLabels=['常用','AI 工具','更新账本','项目整理','数据与隐私','关于','高级'];
  for(const [width,height] of [[1920,1080],[1440,900],[1366,768],[1180,720]]){
   await page.setViewportSize({width,height});await go('settings');
   assert.deepEqual(await page.locator('.settings-nav button').allTextContents(),settingLabels);
   for(const [index,id] of settingIds.entries()){
    await page.locator(`.settings-nav [data-setting="${id}"]`).click();
    assert.equal(await page.locator('#settings-page-title').textContent(),settingLabels[index]);
    assert.equal(await page.locator('.settings-nav [aria-current="page"]').count(),1);
    assert.equal(await page.locator('.settings-content > [id^="setting-"]').count(),1);
    const body=await page.locator('.settings-content').innerText();
    assert(!body.includes('扫描'),id+' still says scan');
    if(id!=='advanced')assert(!/RAW|EFFECTIVE|REPLAY|Schema|Runtime|Build ID|Backend|Data root|parser|adapter/.test(body),id+' exposes technical vocabulary');
    const layout=await page.locator('.settings-content').evaluate(el=>({horizontalOverflow:el.scrollWidth>el.clientWidth+2,width:el.clientWidth,border:getComputedStyle(el).borderTopWidth,radius:getComputedStyle(el).borderTopLeftRadius,shadow:getComputedStyle(el).boxShadow,font:[...el.querySelectorAll('p')].filter(x=>x.getClientRects().length).map(x=>parseFloat(getComputedStyle(x).fontSize)),pageOverflow:document.documentElement.scrollWidth>innerWidth}));
    assert(!layout.horizontalOverflow&&!layout.pageOverflow,id+' '+width+' overflow');assert.equal(layout.border,'0px');assert.equal(layout.radius,'0px');assert.equal(layout.shadow,'none');assert(layout.font.every(n=>n>=13),id+' small descriptions');if(width===1180)assert(layout.width>=780,id+' narrow content');
    assert((await page.locator('.settings-content button.primary').count())<=1,id+' multiple primary CTA');
    if(id==='common'){assert.equal(await page.locator('.settings-tool').count(),0);assert.equal(await page.locator('button.toggle').count(),0);assert(body.includes('尚未启用'));}
    if(id==='sources'){assert.equal(await page.locator('.settings-tool').count(),5);assert.equal(await page.locator('.settings-tool .badge.partial').count(),2);}
    if(id==='scan'){assert(body.includes('今天 14:28'));assert(body.includes('5 个 AI 工具'));assert(body.includes('成功'));assert((body.match(/尚未启用/g)||[]).length===2);}
    if(id==='attribution'){assert(body.includes('6 场会话'));assert(!body.includes('2 组重复'));}
    if(id==='privacy'){assert(body.includes('%LOCALAPPDATA%'));assert(body.includes('不上传完整工作路径'));assert(body.includes('无遥测'));assert(body.includes('无云同步'));assert(body.includes('历史仍然保留'));}
    if(id==='advanced'){assert.equal(await page.locator('.settings-content details[open]').count(),0);}
   }
   add(`设置 7 分组 / ${width}×${height}`,'单个独立内容面；无巨型卡片；轻量导航；描述≥13px；最多一个主动作；最小窗口内容宽度≥780px');
  }
  await page.setViewportSize({width:1440,height:900});await go('settings','&setting=privacy');await page.getByRole('button',{name:'查看高级数据说明'}).click();assert.equal(await page.locator('#settings-page-title').textContent(),'高级');assert(await page.locator('[data-disclosure="settings-identity"] .disclosure-body').isVisible());add('隐私 → 高级数据说明','打开对应高级折叠，不跳到无关配置');
  await go('settings','&setting=attribution');await page.getByRole('button',{name:'管理名称'}).click();assert(await page.getByLabel('项目显示名称').isVisible());await page.getByRole('button',{name:'取消',exact:true}).click();await page.getByRole('button',{name:'开始整理'}).click();assert.equal(await page.locator('[name="move-sessions"]').count(),6);await page.getByRole('button',{name:'取消',exact:true}).click();await page.getByRole('button',{name:'管理合并'}).click();assert(await page.locator('[name="merge-projects"]').count());add('设置项目整理的三个具体动作','名称、待整理会话、合并均进入既有流程，不编造重复项目发现能力');
  await go();await page.getByLabel('项目筛选').selectOption('p_fox');await page.locator('[data-nav="settings"]').click();await page.locator('[data-setting="attribution"]').click();assert((await page.locator('.settings-content').innerText()).includes('6 场会话'));await page.getByRole('button',{name:'开始整理'}).click();assert.equal(await page.locator('[name="move-sessions"]').count(),6);add('设置整理数量与操作一致','整理全账本的6场待整理会话，不受首页项目筛选误裁剪');
  for(const [scenario,title] of [['success','成功'],['partial-scan','部分成功'],['scan-error','失败']]){await go('settings','&setting=scan&scenario='+scenario);assert((await page.locator('.settings-update-facts').innerText()).includes(title));await page.getByRole('button',{name:'立即更新',exact:true}).click();assert(await page.getByRole('button',{name:'正在更新…',exact:true}).isDisabled());await page.getByRole('button',{name:'立即更新',exact:true}).waitFor();assert((await page.locator('.settings-update-facts').innerText()).includes(title));}add('更新账本结果与防并发','成功、部分成功、失败保留准确结果；更新中按钮禁用，历史数据不变');
  await go('settings');const countBefore=await page.evaluate(()=>records.length);await page.getByRole('button',{name:'打开数据目录',exact:true}).click();assert((await page.locator('#toast').innerText()).includes('设计参考'));assert.equal(await page.evaluate(()=>records.length),countBefore);add('数据目录仍为设计演示','只显示正式桌面接口说明，不操作本机目录');
  await go('confidence');await page.getByRole('button',{name:'管理 AI 工具'}).click();assert.equal(await page.locator('#settings-page-title').textContent(),'AI 工具');add('完整性管理入口定位准确','进入 AI 工具页，完整性页面内容与能力未重构');

  assert.deepEqual(errors,[]);add('浏览器脚本错误',0);
  fs.writeFileSync(path.join(__dirname,'qa-report.json'),JSON.stringify({scope:'Settings Local Rebuild + Global Minor Polish; offline design fixtures only, not Runtime tests',checks,errors},null,2));console.log(JSON.stringify({checks,errors},null,2));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
