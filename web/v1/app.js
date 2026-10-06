/* ==================================================================
   智账 · PathOrbit AI Ledger · Functional V1 Light UI · Runtime
   UI/IA 事实源：design/functional-v1-master/（master.css / master.js 生成器移植）。
   数据事实源：/api/v1/*（真实 usage.db / usage-board.json）。
   无示例业务数据：Loading / Empty / Partial / Error 全部如实呈现。
   口径：input 不含缓存；total = input + output；cache 单列；
        EFFECTIVE 默认，RAW 保留审计；估算成本 ≠ 实际账单；缺价格 ≠ 免费。
   ================================================================== */
'use strict';
const icons = {
  overview:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  explore:'<path d="M4 5h16M4 12h16M4 19h16M8 3v18"/>',
  settings:'<path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1M5.6 18.4l2.1-2.1m8.6-8.6 2.1 2.1"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="2"/>',
  scan:'<path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',
  shield:'<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6z"/><path d="m8 12 3 3 5-6"/>',
  folder:'<path d="M3 7V5h6l2 2h10v12H3z"/>',
  database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
  arrow:'<path d="M5 12h14m-6-6 6 6-6 6"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  close:'<path d="m6 6 12 12M18 6 6 18"/>',
  link:'<path d="m9 15 6-6M8 13l-2 2a4 4 0 0 0 6 6l3-3M16 11l2-2a4 4 0 0 0-6-6L9 6"/>'
};
const scanOutcome=s=>s==='success'?'success':['partial','partial_failure'].includes(s)?'partial':'error';
const icon=n=>`<span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24">${icons[n]||icons.folder}</svg></span>`;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>n==null?'暂无具体用量':n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':n.toLocaleString('en-US');
const hasUsage=a=>(a.token_records||0)>0;
const exact=n=>n==null?'不可用':n.toLocaleString('en-US');
const money=(n,c)=>`${c==='CNY'?'¥':'$'}${n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}`;
const dateText=t=>new Date(t).toLocaleString('sv-SE',{hour12:false}).replace('T',' ');
const timeText=t=>dateText(t).slice(5,16);
const relative=t=>{if(!t)return '暂无活动';let m=Math.max(0,Math.floor((Date.now()-t)/60000));return m<1?'刚刚':m<60?`${m} 分钟前`:m<1440?`${Math.floor(m/60)} 小时前`:`${Math.floor(m/1440)} 天前`};

/* ---------------- 状态 ---------------- */
const state={
  view:location.pathname==='/'||location.pathname===''?'overview':location.pathname.replace('/','')||'overview',
  range:'30',project:'all',client:'all',model:'all',sort:'tokens',
  chosenProject:null,chosenSession:null,chosenEvent:null,eventPage:0,
  audit:'effective',attributionTab:'alias',moveSession:null,moveTarget:null,
  overlay:null,origin:'overview',settingTab:'common',disclosures:{},
  desktop:null,migration:null,migrationError:null,storagePath:null,
  update:null,whatsNewShown:false,whatsNewData:null,changelogData:null,
  onboarding:'welcome',scan:'idle',data:null,identity:null,health:null,
  discover:null,unassigned:[],replayCount:null,pending:null,moveIds:[],
  error:null,booted:false
};
if(!['overview','explore','settings'].includes(state.view))state.view='overview';
/* RC.3：/settings?tab=about 之类直达（托盘菜单 / What's New 入口使用） */
{const t=new URLSearchParams(location.search).get('tab');if(t&&['common','sources','scan','attribution','privacy','about','advanced'].includes(t))state.settingTab=t;}
/* RC.3：First Run 进度在 backend 重启（选定数据位置）后经 sessionStorage 续步 */
try{const savedOb=sessionStorage.getItem('zhizhang.onboarding');if(savedOb)state.onboarding=savedOb;}catch(e){}

/* ---------------- 数据层（真实 API） ---------------- */
async function api(path,opts){
  let r;
  try{r=await fetch(path,opts);}
  catch(e){const err=new Error('本地服务不可达');err.status=0;err.code='network';throw err;}
  let j={};
  try{j=await r.json();}catch(e){}
  if(!r.ok){const err=new Error((j&&j.message)||('HTTP '+r.status));err.status=r.status;err.code=j&&j.error;err.payload=j;throw err;}
  return j;
}
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
/* RC.3 桌面壳 IPC 桥：仅 Tauri 桌面环境可用；浏览器开发态安全降级 */
const tauriInvoke=(cmd,args)=>{const t=window.__TAURI__;const f=t&&(t.core&&t.core.invoke||t.invoke);if(!f)return Promise.reject(new Error('not-desktop'));return f(cmd,args);};
async function loadDesktop(){
  try{state.desktop=await tauriInvoke('get_desktop_state');}catch(e){state.desktop=null;return;}
  /* What's New：升级用户首启展示一次（Rust 启动时已记录游标；标志领取即清除，§55） */
  if(state.desktop&&state.desktop.show_whats_new&&!state.whatsNewShown){
    state.whatsNewShown=true;
    try{
      const ver=(state.identity&&state.identity.app_version)||state.desktop.last_seen_version||'';
      const cl=await api('/v1/changelog.json');
      const entry=(cl.versions||[]).find(v=>v.version===ver)||{};
      state.whatsNewData={ver,items:entry.highlights||[]};
      state.overlay='whatsnew';render();
    }catch(e){}
  }
}
const switchBtn=(on,action,label)=>`<button class="switch ${on?'on':''}" data-action="${action}" role="switch" aria-checked="${on?'true':'false'}" aria-label="${esc(label)}"><span class="knob"></span></button>`;
const queryURL=over=>{
  const q=new URLSearchParams({range:state.range,project:state.project,client:state.client,
    model:state.model,audit:state.audit,page:String(state.eventPage)});
  if(over&&over.session)q.set('session',over.session);
  if(over&&over.page!=null)q.set('page',String(over.page));
  return '/api/v1/query?'+q.toString();
};
async function loadIdentity(){try{state.identity=await api('/api/v1/identity');}catch(e){state.identity=null;}}
async function loadHealth(){try{state.health=await api('/api/v1/health');}catch(e){state.health=null;}}
async function loadDiscover(force){
  if(state.discover&&!force)return state.discover;
  try{state.discover=await api('/api/v1/discover');}catch(e){state.discover=null;}
  return state.discover;
}
async function loadUnassigned(){
  try{const u=await api('/api/v1/sessions/unassigned');state.unassigned=u.items||[];}
  catch(e){state.unassigned=[];}
}
async function loadReplayCount(){
  try{const q=new URLSearchParams({range:state.range,project:state.project,client:state.client,
    model:state.model,audit:'replay',page:'0'});
    const r=await api('/api/v1/query?'+q.toString());
    state.replayCount=r.total;}
  catch(e){state.replayCount=null;}
}
async function loadQuery(over){
  const r=await api(queryURL(over||{}));
  if(!over){state.data=r;
    // 会话列表随范围过滤；项目/记录同源
    state.sessions=(r.sessions||[]).map(s=>({
      id:s.source+'|'+s.session_id,project:s.project_key||'__unassigned',
      key:s.project_key,title:s.title,source:s.source,agent:s.agent,
      events:s.events,total:s.total,token_records:s.token_records,last_ts:s.last_ts,
      costs:s.estimated_cost_by_currency||{},unpriced:s.unpriced_events||0}));
    state.projects=(r.by_project||[]).map(p=>({
      key:p.project_key||'__unassigned',dbKey:p.project_key,
      name:p.project_key?'(待整理) '.concat(p.project_key.slice(0,8)):'(待整理)',
      ...p}));
  }
  if(!over&&state.view==='explore')await loadExploreRecords();
  return r;
}
async function loadExploreRecords(){
  const ps=projectList();
  if(!ps.some(p=>p.key===state.chosenProject))state.chosenProject=ps[0]?.key||null;
  const ss=(state.sessions||[]).filter(s=>s.project===state.chosenProject);
  if(!ss.some(s=>s.id===state.chosenSession))state.chosenSession=ss[0]?.id||null;
  const q=new URLSearchParams({range:state.range,project:state.chosenProject==='__unassigned'?'unassigned':state.chosenProject||state.project,
    client:state.client,model:state.model,audit:state.audit,page:String(state.eventPage)});
  if(state.chosenSession)q.set('session',state.chosenSession);
  state.recordData=await api('/api/v1/query?'+q.toString());
}
const eventIdentity=e=>JSON.stringify([e.client,e.session_id,e.event_id]);

/* ---------------- 派生视图数据 ---------------- */
function costHTML(costs,hasTokens,unpriced){
  const c=['CNY','USD'].filter(c=>costs&&costs[c]!=null).map(c=>money(costs[c],c));
  return c.length?c.join(' <span class="currency-separator">／</span> '):hasTokens?'暂无价格':'暂不能估算';
}
function projectList(){
  // 我的项目：服务端已按当前筛选聚合；排序在前端（不改变后端口径）
  const arr=(state.data.by_project||[]).map(p=>({
    key:p.project_key||'__unassigned',dbKey:p.project_key,
    name:p.project_key?(p.aliases&&state.renamed&&state.renamed[p.project_key])||p.display_name:'(待整理)',
    kind:'工作目录',mark:(p.display_name||'?').charAt(0).toUpperCase(),
    original:p.original_display||null,aliases:p.aliases||[],
    summary:{events:p.events,sessions:p.sessions,total:p.total,
      hasTokens:hasUsage(p),costs:p.estimated_cost_by_currency||{},
      last:p.last_ts,unpriced:p.unpriced_events>0,
      activity:false,unknown:false}}));
  const val=p=>state.sort==='last'?(p.summary.last||0)
    :state.sort.startsWith('cost')?((p.summary.costs[state.sort.slice(5)]??-1))
    :p.summary.total;
  arr.sort((a,b)=>val(b)-val(a));
  return arr;
}
const chosenProjectEntry=()=>projectList().find(p=>p.key===state.chosenProject)||projectList()[0];
const chosenSessionRow=()=>state.sessions.find(s=>s.id===state.chosenSession)||null;
function sessionCosts(s){return s.costs&&Object.keys(s.costs).length?s.costs:null;}
function badge(text,type='complete'){return `<span class="badge ${type}">${esc(text)}</span>`}
function info(kind='cost'){
  const copy=kind==='cost'?'按公开 API 价格估算，用于比较 AI 使用规模，不代表你的实际账单。人民币与美元分别显示，不换汇。价格未知的部分不会当成免费。':'用量以 Token 计量，可以理解为 AI 处理文字与生成回答的规模。这里只合计输入与输出，缓存内容另列在用量详情中。';
  return `<details class="info"><summary aria-label="${kind==='cost'?'估算成本说明':'用量单位说明'}">i</summary><div class="info-pop">${copy}${kind==='cost'?'<small>Estimated API-equivalent Cost · 当前参考价格</small>':''}</div></details>`;
}
function emptyCard(title,description,action='clear-filters',cta='查看全部历史',kind='folder'){return `<section class="panel empty state-card"><div class="state-icon">${icon(kind)}</div><h2>${title}</h2><p>${description}</p><button class="primary" data-action="${action}">${cta}</button></section>`}
function statusNotice(title,description,action,cta,type='partial',glyph='shield'){return `<div class="state-notice ${type}" role="status">${icon(glyph)}<div><strong>${title}</strong><p>${description}</p></div><button class="text-button" data-action="${action}">${cta} →</button></div>`}
function brand(){return `<div class="brand"><img class="brand-logo" src="/assets/art/brand/zhizhang-128.png" alt="智账"><div><strong>智账</strong><small>PathOrbit AI Ledger</small></div></div>`}
function shell(content){return `<div class="shell"><aside class="sidebar">${brand()}<div class="nav-label">我的 AI 使用</div><nav class="nav" aria-label="主要导航">${[['overview','总览'],['explore','探索'],['settings','设置']].map(([id,name])=>`<button data-nav="${id}" class="${state.view===id?'active':''}">${icon(id)}${name}</button>`).join('')}</nav><div class="sidebar-bottom"><div class="local">${icon('shield')} 数据只保存在本机</div><p>用过的 AI，<br>慢慢成为自己的账本。</p><small>工具清理历史后，<br>已记账的记录依然保留。</small></div></aside><main class="main ${state.view==='settings'?'settings-main':''}">${content}</main></div>`}
function lastUpdateText(){
  const lr=state.health&&(state.health.last_run||{});
  if(state.scan==='done')return '刚刚更新';
  if(lr&&lr.finished_at)return relative(Date.parse(lr.finished_at))+'更新';
  return '尚未更新';
}
function scanButton(label='更新账本'){return `<button class="primary inline" data-action="scan" ${state.scan==='running'?'disabled':''}>${icon('scan')}${state.scan==='running'?'正在更新…':label}</button>`}
function header(title,context,sub){return `<header class="page-header"><div><div class="eyebrow">${context}</div><h1>${title}</h1>${sub?`<p class="subtitle">${sub}</p>`:''}</div><div class="header-actions"><small>${lastUpdateText()}</small>${scanButton()}</div></header>`}
function option(value,label,current){return `<option value="${esc(value)}" ${current===value?'selected':''}>${esc(label)}</option>`}
function toolbar(){
  const ps=projectList();
  return `<div class="toolbar"><div class="inline"><div class="segments" aria-label="时间范围">${[['1','今天'],['7','最近 7 天'],['30','最近 30 天'],['all','全部历史']].map(([id,label])=>`<button data-range="${id}" class="${state.range===id?'active':''}" aria-pressed="${state.range===id}">${label}</button>`).join('')}</div><select aria-label="项目筛选" data-filter="project">${option('all','全部项目',state.project)}${ps.map(p=>option(p.dbKey||'__unassigned',p.name,state.project)).join('')}</select><details class="more-filters" data-disclosure="filters" ${state.disclosures?.filters?'open':''}><summary>${icon('settings')} 筛选工具与模型 ${state.client!=='all'||state.model!=='all'?'<i class="filter-dot"></i>':''}</summary><div class="filter-pop"><label>AI 工具<select aria-label="AI 工具筛选" data-filter="client">${option('all','全部 AI 工具',state.client)}${(state.data.by_client||[]).map(c=>option(c.client,c.label||c.client,state.client)).join('')}</select></label><label>模型<select aria-label="模型筛选" data-filter="model">${option('all','全部模型',state.model)}${(state.data.models||[]).map(m=>option(m.model,m.model,state.model)).join('')}</select></label><button class="text-button" data-action="clear-filters">清除筛选</button></div></details></div><button class="quiet confidence-link" data-action="confidence">${icon('shield')} 这本账完整吗？</button></div>`;
}
function scanNotice(){
  if(state.scan==='running')return statusNotice('正在更新你的账本','正在读取 AI 工具的最新记录，已有账本仍可查看。','continue-reading','查看已有记录','loading','scan');
  const lr=state.health&&state.health.last_run;
  if(lr&&lr.result&&lr.result!=='unknown'&&scanOutcome(lr.result)==='error')return statusNotice('账本暂时没能更新','上次更新没有成功，已保存的账本和历史记录仍在。','scan','重新更新','error','scan');
  if(lr&&scanOutcome(lr.result)==='partial')return statusNotice('账本已更新，部分工具还需要检查','部分工具暂时无法读取；其他工具的记录已更新，历史记录仍保留。','confidence','查看详情','partial');
  const a=state.data.aggregate||{};
  if(a.unknown>0)return statusNotice('有些记录暂时无法识别',`${a.unknown} 条记录无法读取具体内容，未计入使用量或估算成本。`,'confidence','查看原因','partial');
  if(state.unassigned.length>0)return statusNotice('这些记录还没有找到所属项目','选择一个已有项目，把不同工具的使用记录整理到一起。','organize-unassigned','整理记录','partial','folder');
  if(a.unpriced_events>0)return statusNotice('这部分用量暂时无法估算价格','使用量照常保留；没有价格不代表免费。','confidence','了解详情','partial','clock');
  return '';
}
function overview(){
  const a=state.data.aggregate||{},ps=projectList();
  const period=state.range==='all'?'全部历史':state.range==='1'?'今天':`最近 ${state.range} 天`;
  const pending=state.unassigned.reduce((n,s)=>n+(s.events?0:0),0)||state.unassigned.length;
  const top=header('最近 AI 都用在哪？','总览',a.projects?`${period}，你在 ${a.projects} 个项目中进行了 ${a.sessions} 场 AI 会话。`:`${period}，还没有可归入项目的使用记录。`)+toolbar();
  if(state.error)return top+emptyCard('暂时无法打开账本','已有数据不会被覆盖或清除。'+esc(state.error),'retry-ledger','重新读取','database');
  if(!ps.length)return top+emptyCard('这个范围内还没有使用记录','试试更长的时间范围，或更新一下最近的 AI 使用。');
  return top+scanNotice()+`<section class="kpis" aria-label="使用摘要"><div class="kpi"><div class="kpi-label">参与的项目</div><div class="kpi-value num">${a.projects}<span>个</span></div><small>不同工具，汇入同一个项目</small></div><div class="kpi"><div class="kpi-label">AI 会话</div><div class="kpi-value num">${a.sessions}<span>场</span></div><small>${state.unassigned.length?state.unassigned.length+' 场待整理':'全部已归入项目'}</small></div><div class="kpi"><div class="kpi-label">AI 使用量 ${info('usage')}</div><div class="kpi-value num">${hasUsage(a)?fmt(a.total):'暂无具体用量'}</div><small>${hasUsage(a)?'查看项目，了解用在哪里':'只能确认使用过'}</small></div><div class="kpi kpi-money"><div class="kpi-label">估算成本 ${info()}</div><div class="kpi-value kpi-cost num">${(()=>{const rc=a.reliable_costs||{},fc=a.reference_costs||{};const cur=['CNY','USD'].filter(c=>rc[c]!=null||fc[c]!=null);if(!cur.length)return a.costs&&Object.keys(a.costs).length?Object.entries(a.costs).map(([c,v])=>`<span>${money(v,c)}<small>${c}</small></span>`).join('<em>／</em>'):'<span class="unavailable">暂不能估算</span>';return cur.map(c=>{const reliable=rc[c]!=null;const v=rc[c]!=null?rc[c]:fc[c];return `<span>${reliable?'':'~'}${money(v,c)}<small>${c}</small></span>`}).join('<em>／</em>')})()}</div><small>${(()=>{const rc=a.reliable_costs||{},fc=a.reference_costs||{};const ref=['CNY','USD'].filter(c=>fc[c]>0).map(c=>`${money(fc[c],c)}${rc[c]!=null?' 第三方参考':''}`).join('＋');const parts=['API 等价值估算 · 不是实际账单','币种分别显示'];if(ref)parts.push(`其中 ${ref}（仅用于参考估算，不代表当前使用渠道实际价格）`);if(a.unpriced_events)parts.push('部分用量暂不能估算');return parts.join(' · ')})()}</small></div></section><div class="content-grid"><section class="panel project-ledger"><div class="panel-heading"><div><h2>我的项目</h2><p>为了把这些项目做到今天，你用了多少 AI？</p></div><select aria-label="项目排序" id="project-sort">${option('tokens','使用量最多',state.sort)}${option('cost-CNY','人民币估算最多',state.sort)}${option('cost-USD','美元估算最多',state.sort)}${option('last','最近使用',state.sort)}</select></div><div class="project-table-scroll">${projectTable(ps)}</div><div class="panel-footer"><small>${ps.length} 个项目分组，包含待整理记录</small><button class="text-button" data-action="attribution">整理项目 →</button></div></section><aside class="right-rail">${healthPanel()}${trendPanel()}</aside></div><div class="bottom-grid"><section class="panel"><div class="panel-heading"><h2>最近的 AI 会话</h2><button class="text-button" data-nav="explore">查看使用记录 →</button></div><div class="panel-body recent-list">${recentSessions(3).map(recentSession).join('')||'<p class="muted">暂无匹配会话</p>'}</div></section>${modelsPanel()}</div><div class="footer-note">记录保存在本机。即使 AI 工具清理了原始历史，已经入账的使用仍会保留。</div>`;
}
function projectTable(ps){const max=Math.max(...ps.map(p=>p.summary.total),1);return `<table class="ledger-table project-table"><thead><tr><th>项目</th><th class="right">会话</th><th class="right">AI 使用量</th><th class="right">估算成本</th><th class="col-status">账本状态</th></tr></thead><tbody>${ps.map((p,i)=>`<tr class="clickable" tabindex="0" role="button" aria-label="查看项目 ${esc(p.name)}" data-project="${p.key}"><td><div class="project-name"><span class="project-mark ${i===0?'gold':''}">${esc(p.mark)}</span><div><strong>${esc(p.name)}</strong><small>${p.key==='__unassigned'?'等待你确认所属项目':`最近使用 · ${relative(p.summary.last)}`}</small></div></div></td><td class="right num">${p.summary.sessions}<span class="unit"> 场</span></td><td class="right"><span class="project-token num">${p.summary.hasTokens?fmt(p.summary.total):'暂无具体用量'}</span><div class="mini-bar"><i style="width:${p.summary.total/max*100}%"></i></div></td><td class="right cost-cell num">${costHTML(p.summary.costs,p.summary.hasTokens,p.summary.unpriced)}${p.summary.unpriced?'<small>部分用量暂不能估算</small>':''}</td><td class="col-status">${badge(p.summary.hasTokens?'已读取用量':'只能确认使用过',p.summary.hasTokens?'complete':'activity')}</td></tr>`).join('')}</tbody></table>`}
function trendPanel(){
  const daily=(state.data.daily||[]).slice(-14);
  const vals=daily.map(d=>d.total);
  const max=Math.max(...vals,1);
  const pts=vals.map((v,i)=>`${i*20},${100-v/max*78}`).join(' ');
  const label=(d)=>d?d.day.slice(5).replace('-','.'):'';
  return `<section class="panel trend-panel"><div class="panel-heading"><h3>最近的使用变化</h3><small>${daily.length?`近 ${daily.length} 天`:'暂无数据'}</small></div><div class="panel-body"><div class="trend-summary"><strong class="num">${fmt(vals.reduce((n,v)=>n+v,0))}</strong><small>已读取的使用量</small></div><div class="trend"><svg viewBox="0 0 260 115" preserveAspectRatio="none" role="img" aria-label="已读取用量的变化">${vals.length?`<defs><linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#5d9bb0" stop-opacity=".16"/><stop offset="1" stop-color="#5d9bb0" stop-opacity="0"/></linearGradient></defs><path d="M0 35H260M0 105H260" stroke="#e6ecec" stroke-dasharray="3 6"/><polygon points="0,110 ${pts} 260,110" fill="url(#trend-fill)"/><polyline points="${pts}" fill="none" stroke="#548fa5" stroke-width="2" stroke-linejoin="round"/>`:''}</svg><div class="chart-labels"><span>${label(daily[0])}</span><span>${label(daily[Math.floor(daily.length/2)])}</span><span>${label(daily[daily.length-1])}</span></div></div></div></section>`;
}
function pricingTypeText(t){return t==='user_channel'?'用户配置':t==='official'?'官方 API 价':t==='third_party_reference'?'第三方参考价':'暂无可靠价格'}
function modelDistribution(models){
  const sum=models.reduce((n,m)=>n+m.total,0)||1;
  return models.slice(0,6).map((m,i)=>`<div class="model-row"><div class="inline"><span>${esc(m.model||'暂无模型信息')}</span><span class="num muted">${fmt(m.total)}</span></div><div class="track ${i===1?'gold':i===2?'gray':''}"><i style="width:${m.total/sum*100}%"></i></div>${m.priced?`<small class="muted">${pricingTypeText(m.pricing_type)}${m.partial?' · 部分用量暂无价格':''}</small>`:`<small class="muted">暂无可靠价格 · 未计入估算</small>`}</div>`).join('');
}
function modelsPanel(){
  const ms=state.data.models||[];
  return `<details class="panel models-panel" data-disclosure="models" ${state.disclosures?.models?'open':''}><summary><div><h3>想看看用了哪些模型？</h3><p>按使用量查看模型分布</p></div><span>展开 ↓</span></summary><div class="panel-body">${ms.length?modelDistribution(ms):'<p class="muted">暂无模型数据</p>'}</div></details>`;
}
function healthPanel(){
  const a=state.data.aggregate||{},d=state.discover||{};
  const grades=d.grades||{};
  const tokenTools=(d.tools||[]).filter(t=>t.capability==='TOKEN'&&t.data_sources_count>0&&t.status!=='GENERIC_SUPPORTED_UNANCHORED');
  const partial=tokenTools.filter(t=>t.completeness==='PARTIAL').length;
  const complete=(grades.TOKEN||0)-partial,activity=grades.ACTIVITY||0;
  return `<section class="panel health-panel"><div class="panel-heading"><h3>这本账完整吗？</h3>${icon('shield')}</div><div class="panel-body"><strong class="health-conclusion">${hasUsage(a)?(a.unpriced_events?'主要使用量已记录':'主要使用量已记录，价格完整'):(a.events?'已记录使用痕迹，暂无具体用量':'当前范围没有使用记录')}</strong><ul class="health-points"><li><b>${complete}</b> 个工具可读取完整用量</li>${partial?`<li><b>${partial}</b> 个工具可读取用量，但存在历史缺口</li>`:''}<li><b>${activity}</b> 个工具只能确认使用过</li><li>${hasUsage(a)?(a.unpriced_events?'部分用量暂不能估算价格':'当前用量已有参考价格'):'暂无可估算的具体用量'}</li></ul><p class="health-foot">更新账本前的历史是否齐全，还不能确认。</p><button class="text-button" data-action="confidence">查看详情 →</button></div></section>`;
}
function recentSessions(n,pkey){
  let ss=state.sessions;
  if(pkey)ss=ss.filter(s=>s.project===pkey);
  return ss.slice().sort((a,b)=>(b.last_ts||0)-(a.last_ts||0)).slice(0,n);
}
function recentSession(s){
  return `<div class="recent-row"><div><button class="text-button" data-session="${esc(s.id)}">${esc(s.title)}</button><small>${esc(s.project==='__unassigned'?'待整理':(state.data.by_project||[]).find(p=>p.project_key===s.project)?.display_name||'')} · ${esc(s.source)} · ${relative(s.last_ts)}</small></div><span class="num">${hasUsage(s)?fmt(s.total):'只能确认使用过'}</span></div>`;
}
function tokenBreakdown(a){return `<div class="token-breakdown"><div><small>输入 · Input</small><strong class="num">${hasUsage(a)?fmt(a.input):'暂无具体用量'}</strong></div><div><small>输出 · Output</small><strong class="num">${hasUsage(a)?fmt(a.output):'暂无具体用量'}</strong></div><div><small>缓存读取 · Cache</small><strong class="num">${hasUsage(a)?fmt(a.cache):'暂无具体用量'}</strong></div></div><p class="technical-note">输入不含缓存。总用量 = 输入 + 输出；缓存读取单独统计，不加入总用量。</p>`}
function disclosure(key,title,sub,body){return `<details class="disclosure" data-disclosure="${key}" ${state.disclosures?.[key]?'open':''}><summary><div><strong>${title}</strong>${sub?`<small>${sub}</small>`:''}</div><span class="disclosure-arrow">⌄</span></summary><div class="disclosure-body">${body}</div></details>`}
function drawerHead(crumb,title,sub,mark){return `<div class="drawer-head"><div class="inline"><span class="breadcrumb">${crumb}</span><button class="quiet" data-action="close" aria-label="关闭详情">${icon('close')}</button></div><div class="drawer-title">${mark?`<span class="project-mark gold">${esc(mark)}</span>`:''}<h2>${esc(title)}</h2></div><p class="subtitle">${sub}</p></div>`}
function detailSummary(a){return `<div class="detail-summary"><div><small>AI 使用量 ${info('usage')}</small><strong class="num">${hasUsage(a)?fmt(a.total):'暂无具体用量'}</strong><small>${a.sessions} 场会话 · ${a.events} 条记录</small></div><div><small>估算成本 ${info()}</small><strong class="detail-cost">${costHTML(a.costs,hasUsage(a),a.unpriced_events>0)}</strong><small>不是实际账单${a.unpriced_events?' · 部分用量暂不能估算':''}</small></div></div>`}
function sessionCard(s){
  const costs=sessionCosts(s);
  return `<button class="session-card" data-session="${esc(s.id)}"><span><strong>${esc(s.title)}</strong><small>${esc(s.source)} · ${relative(s.last_ts)}</small></span><span class="session-value num">${hasUsage(s)?fmt(s.total):'只能确认使用过'}<small>${hasUsage(s)&&costs?costHTML(costs,true,s.unpriced>0):'暂无具体用量'}</small></span>${icon('arrow')}</button>`;
}
async function openProjectDrawer(p){
  state.chosenProject=p.key;state.origin=state.view;
  // 项目范围查询：会话按用量、模型与工具分布
  try{
    const q=new URLSearchParams({range:state.range,project:p.dbKey||'__unassigned',
      client:state.client,model:state.model,audit:'effective',page:'0'});
    if(p.key==='__unassigned')q.set('project','unassigned');
    const r=await api('/api/v1/query?'+q.toString());
    const a=r.aggregate;
    const src=(r.by_client||[]);
    const sess=(r.sessions||[]).map(s=>({id:s.source+'|'+s.session_id,project:p.key,
      title:s.title,source:s.source,events:s.events,total:s.total,token_records:s.token_records,last_ts:s.last_ts,
      costs:s.estimated_cost_by_currency||{},unpriced:s.unpriced_events||0})).sort((x,y)=>y.total-x.total);
    const detail=tokenBreakdown(a)
      +`<section class="section"><h3>使用了哪些模型</h3>${r.models&&r.models.length?modelDistribution(r.models):'<p class="muted">暂无模型数据</p>'}</section>`
      +`<section class="section"><h3>各 AI 工具的使用量</h3>${src.map(c=>`<div class="distribution-row"><span>${esc(c.label||c.client)}</span><span class="num">${hasUsage(c)?fmt(c.total):`${c.events} 条使用痕迹 · 暂无具体用量`}</span></div>`).join('')||'<p class="muted">暂无数据</p>'}</section>`
      +disclosure('project-technical','高级信息','识别依据与记录身份',`<dl class="technical-grid"><div><dt>原识别名称</dt><dd>${esc(p.original||p.name)}</dd></div><div><dt>Project kind</dt><dd>${esc(p.project_kind||'project')}</dd></div><div><dt>project_key</dt><dd>${esc(p.dbKey||'（待整理 · 尚无项目身份）')}</dd></div><div><dt>路径片段 · 仅本机展示</dt><dd>隐私口径不含完整路径</dd></div></dl>`);
    const d=renderOverlay(`<div class="scrim" data-action="close"></div><aside class="drawer" role="dialog" aria-modal="true" aria-label="项目详情">${drawerHead(state.origin==='explore'?'探索 / 项目':'总览 / 我的项目',p.name,`最近使用 · ${p.summary.last?relative(p.summary.last):'还没有匹配记录'}`,p.mark)}<div class="drawer-content"><div class="detail-toolbar"><span>${a.sessions} 场 AI 会话，来自 ${src.length} 个工具</span><button class="text-button" data-action="alias">修改项目名称</button></div>${detailSummary(a)}<div class="tool-summary"><small>在这些 AI 工具中使用过</small><div class="source-chips">${src.map(c=>`<span>${esc(c.label||c.client)}</span>`).join('')||'<span>暂无</span>'}</div></div><div class="detail-health"><div>${icon('shield')}<span>这本账完整吗？<small>${a.unpriced_events?'部分用量暂不能估算价格。':'查看已读取的记录与可能缺少的信息。'}</small></span></div><button class="text-button" data-action="confidence">查看详情 →</button></div><section class="section project-sessions"><div class="section-heading"><h3>项目会话</h3><small>使用量最多的排在前面</small></div>${sess.map(sessionCard).join('')||'<p class="muted">暂无会话</p>'}</section>${disclosure('project-usage','查看用量详情','输入、输出、缓存，以及模型与工具分布',detail)}</div><div class="drawer-actions"><button data-action="attribution">整理项目</button><button class="primary" data-action="open-explore">查看项目的使用记录</button></div></aside>`);
  }catch(e){toast('项目详情加载失败：'+e.message,true);}
}
async function openSessionDrawer(sid){
  state.chosenSession=sid;
  const s=state.sessions.find(x=>x.id===sid)||chosenSessionRow();
  if(!s){toast('这场会话不在当前筛选范围内');return}
  try{
    const base=new URLSearchParams({range:state.range,client:state.client,model:state.model,audit:'effective',session:sid,page:'0'});
    const r=await api('/api/v1/query?'+base.toString());
    const a=r.aggregate;
    const evs=r.items||[];
    const first=a.first_ts;
    const projName=s.project==='__unassigned'?'待整理':(state.data.by_project||[]).find(p=>p.project_key===s.project)?.display_name||'—';
    const detail=tokenBreakdown(a)+disclosure('session-technical','查看技术来源','模型、助手、身份与原始字段',`<dl class="technical-grid"><div><dt>Source / Client</dt><dd>${esc(s.source)}</dd></div><div><dt>Model / Agent</dt><dd>${esc(evs.find(e=>e.model)?.model||'暂时无法识别')} / ${esc(s.agent||'暂无信息')}</dd></div><div><dt>Session identity</dt><dd>${esc(sid)}</dd></div><div><dt>First seen / Last seen</dt><dd>${first?dateText(first):'暂无记录'} / ${a.last_ts?dateText(a.last_ts):'暂无记录'}</dd></div></dl><p class="technical-note">原始使用记录保存在本地账本，默认已排除确认的重复回放。逐条原始字段可在完整使用记录的高级信息中查看。</p>`);
    renderOverlay(`<div class="scrim" data-action="close"></div><aside class="drawer" role="dialog" aria-modal="true" aria-label="会话详情">${drawerHead(`<button class="text-button" data-action="back-project">${esc(projName)}</button> / 会话`,s.title,'这次 AI 会话用了多少？')}<div class="drawer-content"><div class="session-meta"><div><small>所属项目</small><span>${esc(projName)}</span></div><div><small>发生时间</small><span>${first?dateText(first).slice(0,16):'暂无记录'}<small>最近使用 ${relative(s.last_ts)}</small></span></div><div><small>AI 工具</small><span>${esc(s.source)}</span></div><div><small>模型</small><span>${esc(evs.find(e=>e.model)?.model||'暂无模型信息')}</span></div></div>${detailSummary(a)}${disclosure('session-usage','查看用量详情','输入、输出、缓存与技术来源',detail)}<section class="section"><div class="section-heading"><h3>最近的使用记录</h3><small>最近 ${Math.min(6,a.events)} / ${a.events} 条</small></div><div class="session-records">${evs.slice(0,6).map(e=>`<button class="record-row" data-event-open="${esc(eventIdentity(e))}"><span>${timeText(e.ts_ms)}<small>${esc(e.client)} · ${esc(e.model||'暂无模型信息')}</small></span><span class="num">${e.total==null?'只能确认使用过':fmt(e.total)}</span>${icon('arrow')}</button>`).join('')||'<p class="muted">暂无记录</p>'}</div></section><p class="quiet-explanation">想知道每一条用量从哪里来？打开完整使用记录即可逐条查看。</p></div><div class="drawer-actions"><button data-action="move-session">归到正确项目</button><button class="primary" data-action="open-session-explore">查看完整使用记录</button></div></aside>`);
  }catch(e){toast('会话详情加载失败：'+e.message,true);}
}
function explore(){
  const records=state.recordData||state.data;
  const a=records.aggregate||{},ps=projectList();
  if(!ps.find(p=>p.key===state.chosenProject)&&ps.length)state.chosenProject=ps[0].key;
  const ss=state.sessions.filter(s=>state.chosenProject==='all'?true:s.project===state.chosenProject);
  if(!ss.find(s=>s.id===state.chosenSession)&&ss.length)state.chosenSession=ss[0].id;
  const s=ss.find(x=>x.id===state.chosenSession);
  const p=chosenProjectEntry();
  const rows=records.items||[];
  const sel=rows.find(e=>eventIdentity(e)===state.chosenEvent);
  const pageFrom=state.eventPage*12+1,pageTo=Math.min((state.eventPage+1)*12,records.total);
  return header('探索','我的使用记录','从项目找到会话，再看每一次 AI 使用。')+toolbar()
    +`<div class="explore-breadcrumb"><span>项目 <strong>${esc(p?p.name:'全部')}</strong></span>${icon('arrow')}<span>会话 <strong>${esc(s?s.title:'没有匹配会话')}</strong></span>${icon('arrow')}<span>使用记录 <strong>${records.total} 条</strong></span></div>`
    +`<div class="explore-space"><section class="explore-col explore-projects"><div class="col-title">项目 <small>${ps.length} 个分组</small></div><div class="col-body">${ps.map(x=>`<button class="select-project ${x.key===state.chosenProject?'active':''}" data-explore-project="${x.key}"><strong>${esc(x.name)}</strong><small class="selection-time">${relative(x.summary.last)}使用</small><div class="inline"><span class="num">${x.summary.hasTokens?fmt(x.summary.total):'暂无具体用量'}</span><small>${x.summary.sessions} 场会话</small></div></button>`).join('')||'<p class="muted">暂无匹配项目</p>'}</div></section>`
    +`<section class="explore-col explore-sessions"><div class="col-title">会话 <small>${ss.length} 场</small></div><div class="col-body">${ss.map(x=>`<button class="select-session ${x.id===state.chosenSession?'active':''}" data-explore-session="${x.id}"><strong>${esc(x.title)}</strong><small class="selection-time">${esc(x.source)} · ${relative(x.last_ts)}</small><div class="inline"><span class="num">${hasUsage(x)?fmt(x.total):'只能确认使用过'}</span><small>${hasUsage(x)&&x.costs&&Object.keys(x.costs).length?costHTML(x.costs,true,x.unpriced>0):'暂无具体用量'}</small></div></button>`).join('')||'<p class="muted">暂无匹配会话</p>'}</div></section>`
    +`<section class="explore-col explore-records"><div class="event-heading"><div class="section-heading"><h3>使用记录</h3>${state.audit==='effective'?'<small>点击一条记录，查看用量详情</small>':badge('正在查看高级审计记录','partial')}</div><div class="event-stats"><span>本次用量 <strong class="num">${hasUsage(a)?fmt(a.total):'暂无具体用量'}</strong></span><span>估算成本 <strong class="num">${costHTML(a.costs,hasUsage(a),a.unpriced_events>0)}</strong>${info()}</span></div></div>`
    +`<div class="event-scroll"><table class="ledger-table event-table"><thead><tr><th>时间</th><th>AI 工具</th><th>模型</th><th class="right">使用量</th><th class="right">估算成本</th></tr></thead><tbody>${rows.map(e=>`<tr class="clickable ${eventIdentity(e)===state.chosenEvent?'selected':''} ${e.replay?'replay-row':''}" tabindex="0" role="button" aria-label="查看使用记录" data-explore-event="${esc(eventIdentity(e))}"><td>${timeText(e.ts_ms)}${e.replay?'<small>重复回放</small>':''}</td><td>${esc(e.client)}</td><td class="model-cell">${esc(e.model||'暂无模型信息')}</td><td class="right num" title="${exact(e.total)}">${e.total==null?'暂无具体用量':fmt(e.total)}</td><td class="right num cost-cell">${e.total==null?'暂不能估算':e.cost==null?'暂无价格':money(e.cost,e.currency)}${e.cost_status==='partial'?'<small>部分用量暂不能估算</small>':''}</td></tr>`).join('')}</tbody></table>${records.total?'':emptyCard(state.audit==='replay'?'没有发现重复回放':'没有匹配的使用记录','调整时间、项目或工具筛选后再看看。')}</div>`
    +(sel?eventEvidence(sel):'')
    +`<div class="event-foot"><small>${records.total?pageFrom:0}–${pageTo} / ${records.total} 条使用记录</small><div class="inline"><button class="quiet" data-action="prev-page" ${state.eventPage===0?'disabled':''}>上一页</button><button class="quiet" data-action="next-page" ${(state.eventPage+1)*12>=records.total?'disabled':''}>下一页</button></div></div>`
    +`<details class="disclosure audit" data-disclosure="audit" ${state.audit!=='effective'||state.disclosures?.audit?'open':''}><summary><span>高级：查看原始记录</span><span>⌄</span></summary><div class="disclosure-body"><select aria-label="高级审计口径" id="audit-view">${option('effective','正常记账的使用（EFFECTIVE）',state.audit)}${option('raw','所有原始记录（RAW，含重复回放）',state.audit)}${option('replay','仅重复回放（REPLAY）',state.audit)}</select><p>确认的重复回放不会计入默认账本，原始记录始终保留。此处切换不改变总览的统计方式。</p></div></details></section></div>`;
}
function eventEvidence(e){
  const raw=`<dl class="technical-grid"><div><dt>Source / Session</dt><dd>${esc(e.client)} / ${esc(e.session_id)}</dd></div><div><dt>Event identity</dt><dd>${esc(e.event_id||'')}</dd></div><div><dt>原识别项目</dt><dd>${esc(e.project_key_auto||e.project_key||'待整理')}</dd></div><div><dt>原始时间 ts_ms</dt><dd>${e.ts_ms}</dd></div><div><dt>input_tokens / output_tokens</dt><dd>${exact(e.input)} / ${exact(e.output)}</dd></div><div><dt>cache_read_tokens / total_tokens</dt><dd>${exact(e.cache_read)} / ${exact(e.total)}</dd></div></dl><p>默认账本状态：${e.replay?'重复回放（REPLAY），不计入默认汇总':'有效记录（EFFECTIVE）'}。原始记录保留，可追溯来源。</p>`;
  return `<div class="event-evidence"><div class="section-heading"><h3>这条使用记录的用量详情</h3><button class="quiet" data-action="close-event" aria-label="关闭记录详情">${icon('close')}</button></div><div class="record-breakdown"><span>输入 <strong class="num">${exact(e.input)}</strong></span><span>输出 <strong class="num">${exact(e.output)}</strong></span><span>缓存读取 <strong class="num">${exact(e.cache_read)}</strong></span></div><p class="technical-note">总用量只合计输入与输出，缓存单独列出。${e.total==null?'只能确认使用过，暂无具体用量。':e.cost==null?'暂无价格，不代表免费。':`估算 ${money(e.cost,e.currency)}，不是实际账单。`}</p>${disclosure('event-technical','查看技术详情','原始字段与来源',raw)}</div>`;
}
function settingsRow(title,description,action){return `<div class="settings-row"><div><strong>${title}</strong>${description?`<p>${description}</p>`:''}</div>${action||''}</div>`}
function settingsGroup(title,body){return `<section class="settings-group"><h3>${title}</h3>${body}</section>`}
function toolStatusMeta(t){
  if(t.status==='SUPPORTED'&&t.capability==='TOKEN'){
    if(t.completeness==='PARTIAL')return{p:'完整用量 · 部分较早记录无法计量',b:'已连接 · 部分历史缺口',type:'partial'};
    return{p:'可读取完整使用量',b:'已连接',type:'complete'};
  }
  if(t.status==='SUPPORTED'&&t.capability==='ACTIVITY')return{p:'只能确认使用过，暂无具体用量',b:'已连接',type:'partial'};
  if(t.status==='SUPPORTED')return{p:t.reason||'标准位置未找到数据',b:'未找到',type:'unknown'};
  if(t.status==='GENERIC_SUPPORTED')return{p:'符合通用用量格式，已自动记账',b:'已连接',type:'complete'};
  if(t.status==='GENERIC_SUPPORTED_UNANCHORED')return{p:'发现了可读取的 AI 用量数据，但工具身份待确认，暂未加入账本',b:'可读取 · 身份待确认',type:'partial'};
  if(t.status==='DETECTED_UNSUPPORTED')return{p:t.reason||'确认发现了 AI 数据，暂未支持具体用量',b:'新发现 · 暂未支持',type:'partial'};
  return{p:t.reason||'检测到 AI 数据，待识别',b:'待识别',type:'unknown'};
}
function toolRow(t){
  const m=toolStatusMeta(t);
  return `<div class="settings-tool"><span class="tool-monogram">${esc((t.display_name||t.tool_id||'?').charAt(0).toUpperCase())}</span><div><strong>${esc(t.display_name||t.tool_id)}</strong><p>${esc(m.p)}${t.is_new?' · <b>新发现</b>':''}</p></div>${badge(m.b,m.type)}</div>`;
}
function settingsBody(tab){
  const lr=state.health&&state.health.last_run||{};
  const recent=state.scan==='done'?'刚刚':lr.finished_at?relative(Date.parse(lr.finished_at)):'尚未更新';
  const notEnabled=badge('尚未启用','unknown');
  const d=state.discover||{};
  const found=(d.sources||[]).filter(s=>s.state==='found').length;
  const note=state.scan==='running'?scanNotice():'';
  if(tab==='common'){const dsk=state.desktop;const cb=dsk?dsk.close_behavior:null;const asu=dsk?dsk.autostart_enabled:false;
    return note+settingsGroup('账本更新',settingsRow('读取最新使用记录',`最近更新：${recent}`,settingsUpdateButton())+settingsRow('打开应用时自动更新','当前版本尚未开放，你仍可随时手动更新。',notEnabled))+settingsGroup('后台与启动',dsk?(settingsRow('关闭主窗口时保持后台运行','关闭后智账留在系统托盘，账本更新不中断；可随时从托盘退出。',switchBtn(cb==='background','toggle-close-behavior','关闭主窗口时保持后台运行'))+settingsRow('登录 Windows 后启动智账','开机后自动进入系统托盘，不弹窗打扰。',switchBtn(asu,'toggle-autostart','登录 Windows 后启动智账'))):settingsRow('后台与启动','仅桌面版提供托盘与开机启动设置。',''))+settingsGroup('账本状态',settingsRow(`${found} 个 AI 工具已找到`,'主要使用记录已读取；部分工具只能确认使用过。','<button data-action="confidence">查看详情</button>'))+settingsGroup('数据位置',settingsRow('数据保存在这台电脑','已记入账本的历史会长期保留。','<button data-action="data-folder">打开数据目录</button>'));}
  if(tab==='sources'){
    const tools=d.tools||[];
    const when=d.last_discovery_at?relative(Date.parse(d.last_discovery_at)||0):null;
    return note+settingsGroup('已发现的 AI 工具',`<div class="settings-tools">${tools.map(toolRow).join('')||'<p class="muted">尚未查找 — 首次使用时会自动检查这台电脑上的 AI 工具。</p>'}</div><div class="settings-group-actions"><button data-action="rediscover" ${state.discovering?'disabled':''}>${state.discovering?'正在重新发现…':'重新发现'}</button></div><p class="settings-footnote">${when?`最近自动检查：${when}`:'尚未自动检查'} · 自动发现只读取本机信息，不上传任何数据。</p>`)+`<p class="settings-footnote">重新发现不会清除已记入账本的历史。<button class="text-button" data-action="confidence">查看记录完整性 →</button></p>`;
  }
  if(tab==='scan'){
    const result=state.scan==='running'?'running':state.scan==='done'?state.scanResult:(lr.result&&lr.result!=='unknown'?scanOutcome(lr.result):'never');
    const labels={never:'尚未更新',running:'正在更新',success:'成功',partial:'部分成功',error:'失败'};
    const explanation=result==='never'?'还没有更新过账本。可以使用立即更新读取本机记录。':result==='error'?'本次读取失败，上次保存的账本仍保留。':result==='partial'?'其他工具已更新；部分工具暂时无法读取。':result==='running'?'正在读取最新记录，已有账本仍可查看。':'已读取主要工具的使用记录，部分工具只有使用痕迹。';
    const grades=d.grades||{};
    return note+settingsGroup('最近一次更新',`<dl class="settings-update-facts"><div><dt>最近更新</dt><dd>${state.scan==='done'&&result!=='error'?'刚刚':(lr.finished_at?dateText(Date.parse(lr.finished_at)):'尚未更新')}</dd></div><div><dt>来源</dt><dd>${found} 个 AI 工具</dd></div><div><dt>结果</dt><dd>${badge(labels[result],result==='error'?'error':result==='partial'?'partial':result==='running'?'activity':'complete')}</dd></div></dl><p class="settings-description">${explanation}</p><div class="settings-group-actions">${settingsUpdateButton('立即更新')}</div>`)+settingsGroup('自动更新',settingsRow('打开应用时自动更新','当前版本尚未开放桌面设置。',notEnabled)+settingsRow('自动更新频率','需要最新记录时，请使用手动更新。',notEnabled));
  }
  if(tab==='attribution')return settingsGroup('整理你的项目',settingsRow('修改项目名称','给自动识别出来的项目起一个更好记的名字。','<button data-action="attribution">管理名称</button>')+settingsRow('待整理记录',`${state.unassigned.length} 场会话尚未归入正确项目。`,'<button data-action="organize-unassigned">开始整理</button>')+settingsRow('合并重复项目','把多个自动识别结果归为同一个项目。','<button data-action="organize-merge">管理合并</button>'))+'<p class="settings-footnote">只整理账本中的名称和归属，不修改电脑上的项目文件。</p>';
  if(tab==='privacy'){
    const dsk=state.desktop||{};
    const root=dsk.data_root||(state.identity||{}).data_root||'%LOCALAPPDATA%\\UsageLedger';
    const migrateButtons=dsk.recovering?'':'<button data-action="migrate-pick">更改位置</button>';
    return settingsGroup('账本保存位置',settingsRow('账本保存位置',`<span class="settings-path">${esc(root)}</span><small>智账的使用记录只保存在这台电脑上。</small>`,`<button data-action="data-folder">打开文件夹</button>${migrateButtons}`))+settingsGroup('隐私',`<ul class="settings-privacy-list">${['不上传完整工作路径','无遥测','无云同步'].map(text=>`<li>${icon('shield')}<span>${text}</span></li>`).join('')}</ul>`)+settingsGroup('历史账本','<p class="settings-description">工具中的原始记录被清理后，已经记入账本的历史仍然保留。</p>')+'<button class="text-button" data-setting="advanced" data-open-disclosure="settings-identity">查看高级数据说明 →</button>';
  }
  if(tab==='about'){
    const id=state.identity||{};
    return `<div class="settings-about">${brand()}<p>你的本地 AI 使用账本。看清每个项目、每场对话用了多少 AI。</p></div><dl class="settings-about-facts"><div><dt>版本</dt><dd>${esc(id.app_version||'暂时无法识别')}</dd></div></dl><div class="settings-update-actions"><button class="primary" data-action="check-update" ${state.update&&state.update.phase==='checking'?'disabled':''}>${state.update&&state.update.phase==='checking'?'正在检查…':'检查更新'}</button><button data-action="show-changelog">更新记录</button></div><p class="settings-footnote">检查更新只在你点击时进行；不会自动联网，也不会上传任何账本信息。构建与数据格式信息可在高级中查看。</p>`;
  }
  const id=state.identity||{};
  const pm=(state.data&&state.data.aggregate)||{};
  const dedupView='EFFECTIVE（默认）';
  return `<p class="settings-advanced-intro">这里包含技术口径与诊断信息，普通使用无需修改。</p>`
    +disclosure('settings-pricing','估算价格与计算方式','Pricing source、缓存与记录口径',`<h3>Pricing source</h3><p>人工确认 pricing.json 优先；第三方目录仅作候选。使用当前公开 API 参考价格，不是事件时点的历史账单；无法确认价格时保留 null。CNY / USD 分别显示，不换汇。</p><h3>Cache semantics</h3><p>Input 不含 Cache；total_tokens = input + output；cache_read 单列。WorkBuddy input = max(prompt_tokens − cached_tokens, 0)。</p><h3>RAW / EFFECTIVE / REPLAY</h3><p>默认 ${esc(dedupView)} 排除确认的 REPLAY；RAW 保留原始记录，可在探索高级审计中查看（当前范围 REPLAY ${state.replayCount==null?'—':state.replayCount} 条）。</p>`)
    +disclosure('settings-identity','运行与存储信息','Schema、Runtime、Build ID、Backend 与 Data root',`<dl class="technical-grid"><div><dt>Board Schema</dt><dd>v${esc(id.schema_version||'—')}</dd></div><div><dt>Build ID</dt><dd>${esc(id.build_id||'暂时无法识别')}</dd></div><div><dt>Runtime</dt><dd>${esc(id.runtime_mode||'—')}</dd></div><div><dt>Backend</dt><dd>Python Core</dd></div><div><dt>Data root</dt><dd>${esc(id.data_root_kind||'—')}${id.data_root_kind==='production'?'（%LOCALAPPDATA%\\UsageLedger）':''}</div></div><div><dt>Backend PID</dt><dd>${esc(id.backend_pid||'—')}</dd></div></dl><p>实际位置由 DATA_ROOT 决定，可由环境变量覆盖；开发模式位于仓库。</p>`)
    +disclosure('settings-diagnostics','技术诊断说明','Technical diagnostics · 来源能力与读取限制',`<p>来源健康、最近成功读取、未知记录、价格覆盖与历史边界在数据完整性详情中分别说明。不根据缺少字段推导 0，不把使用痕迹计为 Token。</p><p>当前筛选范围：${state.data.aggregate.events} 条记录、${state.data.aggregate.unknown} 条待识别。</p><button data-action="confidence">查看完整性证据</button>`);
}
function settingsUpdateButton(label='更新账本'){return `<button class="primary inline" data-action="scan" ${state.scan==='running'?'disabled':''}>${icon('scan')}${state.scan==='running'?'正在更新…':label}</button>`}
function settings(){
  const sections=[['common','常用','快速更新账本，查看记录状态与数据位置。'],['sources','AI 工具','看看这台电脑上的哪些 AI 使用可以记入账本。'],['scan','更新账本','从这台电脑上的 AI 工具读取最新使用记录，已记入账本的历史会长期保留。'],['attribution','项目整理','把不同 AI 工具的使用，整理到真正属于它的项目。'],['privacy','数据与隐私','账本属于你，也留在你的电脑上。'],['about','关于',''],['advanced','高级','']];
  const selected=sections.find(s=>s[0]===state.settingTab)||sections[0];state.settingTab=selected[0];
  return `<header class="page-header settings-header"><div><div class="eyebrow">偏好与本机数据</div><h1>设置</h1></div></header><div class="settings-layout"><nav class="settings-nav" aria-label="设置分组">${sections.map(([id,label])=>`<button class="${state.settingTab===id?'active':''} ${id==='sources'||id==='privacy'||id==='about'?'nav-group-start':''}" data-setting="${id}" ${state.settingTab===id?'aria-current="page"':''}>${label}</button>`).join('')}</nav><div class="settings-content" aria-labelledby="settings-page-title"><div class="settings-page-heading"><h2 id="settings-page-title">${selected[1]}</h2>${selected[2]?`<p>${selected[2]}</p>`:''}</div><div id="setting-${selected[0]}">${settingsBody(selected[0])}</div></div></div>`;
}
function attribution(){
  const p=chosenProjectEntry(),tab=state.attributionTab;
  if(!p||p.key==='__unassigned')return `<div class="scrim" data-action="close"></div><section class="modal" role="dialog" aria-label="整理项目"><div class="modal-body"><h2>暂时没有可整理的项目</h2><p>先更新账本，读取可识别项目的使用记录。</p></div><div class="modal-actions"><button data-action="close">关闭</button></div></section>`;
  const others=projectList().filter(x=>x.key!=='__unassigned'&&x.key!==p.key);
  const moveCandidates=state.moveSession?[state.sessions.find(s=>s.id===state.moveSession)].filter(Boolean):state.unassigned.map(s=>({id:(s.source||'')+'|'+(s.session_id||''),title:s.session_id||s.source,source:s.source,project:'__unassigned'}));
  return `<div class="scrim" data-action="close"></div><section class="modal" role="dialog" aria-modal="true" aria-label="整理项目"><div class="drawer-head"><div class="inline"><div><div class="eyebrow">我的项目</div><h2>整理项目</h2></div><button class="quiet" data-action="close" aria-label="关闭项目整理">${icon('close')}</button></div><p class="subtitle">把不同 AI 工具的使用，整理到真正属于它的项目。</p></div><div class="modal-body"><div class="segments attr-tabs">${[['alias','修改项目名称'],['move','归到正确项目'],['merge','合并重复项目']].map(([id,label])=>`<button data-attr-tab="${id}" class="${tab===id?'active':''}">${label}</button>`).join('')}</div>${tab==='alias'?`<div class="project-name current-project"><span class="project-mark gold">${esc(p.mark)}</span><div><small>当前项目</small><strong>${esc(p.name)}</strong></div></div><label class="field">项目显示名称<input id="alias-input" aria-label="项目显示名称" value="${esc(p.name)}" maxlength="60" autocomplete="off"></label><p class="quiet-explanation">只改变账本里的名称，项目的底层身份保持稳定，后续更新账本不会改回自动识别名。</p><div class="preview"><span class="project-mark gold">${esc(p.mark)}</span><div><small>在账本中显示为</small><strong id="alias-preview">${esc(p.name)}</strong></div></div>${disclosure('alias-technical','高级信息','自动识别名称与稳定身份',`<dl class="technical-grid"><div><dt>原识别名称</dt><dd>${esc(p.original||p.name)}</dd></div><div><dt>project_key</dt><dd>${esc(p.dbKey||'（待整理）')}</dd></div><div><dt>Alias</dt><dd>只改变显示名称，底层身份保持稳定；改名后更新账本不会回退。</dd></div></dl>`)}`:tab==='move'?`<h3>${state.moveSession?'选择这场会话的正确项目':'待整理记录'}</h3><p class="subtitle">先选择会话，再选择它应该归属的项目。</p>${moveCandidates.map(s=>`<label class="check-row"><input type="checkbox" name="move-sessions" value="${esc(s.id)}" checked><span><strong>${esc(s.title)}</strong><small>${esc(s.source||'')} · 当前在${s.project==='__unassigned'?'待整理':'其他项目'}</small></span></label>`).join('')||'<p class="quiet-explanation">暂时没有待整理的会话。你也可以从会话详情调整归属。</p>'}<label class="field">归到哪个项目？<select id="move-target" aria-label="目标项目">${projectList().filter(x=>x.key!=='__unassigned').map(x=>option(x.dbKey,x.name,state.moveTarget)).join('')}</select></label><div class="preview"><span>所选会话</span>${icon('arrow')}<strong id="move-preview">${esc((projectList().find(x=>x.dbKey===state.moveTarget)||{}).name||'')}</strong></div><p class="quiet-explanation">只调整账本中的归属，原始 AI 使用记录不会改变。手工归档后，后续更新账本不会覆盖你的决定。</p>`:`<h3>把重复识别的项目合到「${esc(p.name)}」</h3><p class="subtitle">只合并同一个工作的不同识别结果。</p>${others.map(x=>`<label class="check-row"><input type="checkbox" name="merge-projects" value="${esc(x.dbKey)}"><span><strong>${esc(x.name)}</strong><small>自动识别名称：${esc(x.original||x.name)}</small></span></label>`).join('')||'<p class="muted">没有可合并的其他项目</p>'}<div class="preview"><span id="merge-count">0 个项目</span>${icon('arrow')}<strong>${esc(p.name)}</strong></div><p class="quiet-explanation">把多个自动识别结果归为同一个项目，使用记录不会重复计算。合并后，后续更新账本再识别到旧名称也会自动归入这里。</p>${disclosure('merge-technical','高级信息','原始身份与归账关系',`<p>目标 project_key：${esc(p.dbKey||'（待整理）')}。合并保留原始 Project / Session / Source 身份（project_key_auto 留痕），仅改变账本归账关系；已入账记录持久保存。</p>`)}`}</div><div class="modal-actions"><small>只整理账本，不修改项目文件</small><div class="inline"><button data-action="close">取消</button><button class="primary" data-action="apply-attribution">${tab==='alias'?'保存名称':tab==='move'?'确认移动':'确认合并'}</button></div></div></section>`;
}
function confidence(){
  const a=state.data.aggregate||{},d=state.discover||{},grades=d.grades||{};
  const tokenTools=(d.tools||[]).filter(t=>t.capability==='TOKEN'&&t.data_sources_count>0&&t.status!=='GENERIC_SUPPORTED_UNANCHORED');
  const complete=tokenTools.filter(t=>t.completeness!=='PARTIAL');
  const partialHistory=tokenTools.filter(t=>t.completeness==='PARTIAL');
  const activity=(d.sources||[]).filter(s=>s.data_grade==='ACTIVITY'&&s.state==='found');
  const unknown=(d.sources||[]).filter(s=>s.data_grade==='UNKNOWN'&&s.state==='found').concat((d.opaque||[]).filter(s=>s.state==='found'));
  const missing=state.health?.ledger?.source_files_purged;
  const history=missing>0?`${missing} 个原始记录文件已不在工具中`:'首次更新之前的历史是否齐全，还不能确认';
  const lr=state.health&&state.health.last_run||{};
  const cov=hasUsage(a)?Math.round((a.priced_tokens||0)/a.total*100):null;
  const tech=`<p><b>Replay filtering：</b>当前范围确认重复回放 ${state.replayCount==null?'—':state.replayCount} 条已排除；EFFECTIVE 默认不计入，RAW 永久保留，REPLAY 可在探索高级审计中查看。</p><p><b>Unknown：</b>${a.unknown} 条记录暂时无法识别；未知与仅活动不计入 Token 或成本。</p><p><b>Pricing coverage：</b>当前筛选已定价 Token ${exact(a.priced_tokens)} / ${exact(a.total)}（${cov==null?'—':cov}%）；${a.unpriced_events} 条记录存在缺失价格；已定价范围以 Token 覆盖数为准。原 Token 记录数 ${a.token_records||0}。不生成综合可信度百分比。</p><p><b>Provenance：</b>AI 工具、原识别项目、会话身份、记录身份与时间可在探索中逐条追溯；被手工归档的记录保留 adapter 原始归因（project_key_auto）。</p><p><b>Cache semantics：</b>Input excludes cache；total_tokens = input + output；cache_read 单列。</p><p>最近一次更新账本：${lr.finished_at?dateText(Date.parse(lr.finished_at)):'尚未运行'}。</p>`;
  return `<div class="scrim" data-action="close"></div><section class="modal confidence-modal" role="dialog" aria-modal="true" aria-label="数据完整性"><div class="drawer-head"><div class="inline"><div><div class="eyebrow">这本账完整吗？</div><h2>数据完整性</h2></div><button class="quiet" data-action="close" aria-label="关闭数据完整性">${icon('close')}</button></div></div><div class="modal-body"><div class="confidence-conclusion">${icon('shield')}<div><h3>${a.events?'当前范围的使用记录已读取':'当前范围没有使用记录'}</h3><p>${a.events?'已读取当前范围的记录。来源能力与价格覆盖分别列在下方。':'当前范围没有记录，可调整筛选或更新账本。'}</p></div></div><section class="confidence-group"><h3>完整记录</h3>${complete.map(s=>`<div class="confidence-source"><strong>${esc(s.display_name||s.label||s.tool_id)}</strong><span>可读取完整使用量</span>${badge('已读取','complete')}</div>`).join('')||'<p class="muted">暂无</p>'}</section>${partialHistory.length?`<section class="confidence-group"><h3>历史缺口</h3>${partialHistory.map(t=>`<div class="confidence-source"><strong>${esc(t.display_name||t.tool_id)}</strong><span>当前格式可完整读取；部分较早记录无法计量，不会计为用量</span>${badge('历史缺口','partial')}</div>`).join('')}</section>`:''}<section class="confidence-group"><h3>部分记录</h3>${activity.map(s=>`<div class="confidence-source"><strong>${esc(s.label||s.client)}</strong><span>只能确认使用过，暂无具体用量</span>${badge('有使用痕迹','activity')}</div>`).join('')||'<p class="muted">暂无</p>'}<p class="quiet-explanation">${unknown.length} 个已找到的来源暂时无法识别具体用量；${a.unknown} 条使用记录缺少具体用量。它们不会被当成完整用量或免费使用。</p></section><div class="confidence-two"><section class="confidence-group"><h3>成本</h3><strong>${hasUsage(a)?(a.unpriced_events?'部分用量还没有可确认的价格':'当前用量已有参考价格'):'暂无可估算的具体用量'}</strong><p>可估算的用量：${fmt(a.priced_tokens||0)} / ${fmt(a.total)}。没有价格不等于免费，估算不是实际账单。</p></section><section class="confidence-group"><h3>历史</h3><strong>${history}</strong><p>首次更新之前的记录是否齐全，还不能确认。之后已入账的历史会持续保留。</p></section></div>${disclosure('confidence-technical','查看技术详情','需要核对记录与计算方式时再展开',tech)}</div><div class="modal-actions"><small>${lastUpdateText()} · 数据保存在本机</small><div class="inline"><button data-action="confidence-settings">管理 AI 工具</button><button class="primary" data-action="close">明白了</button></div></div></section>`;
}
function firstRun(){
  const step=state.onboarding;
  const stage=step==='welcome'?0:step==='storage'||step==='background'?1:step==='discovered'||step==='no-sources'?2:3;
  const d=state.discover||{};
  const found=(d.tools||[]).filter(t=>t.status==='GENERIC_SUPPORTED'||
    t.status==='DETECTED_UNSUPPORTED'||t.data_sources_count>0);
  const storageHtml=`<div class="welcome-heading"><h1>你的账本保存在哪里？</h1><p>智账的使用记录只保存在这台电脑上。</p></div><div class="storage-options"><button class="storage-option ${state.storagePath==null?'active':''}" data-action="storage-default"><strong>使用系统推荐位置</strong><small>%LOCALAPPDATA%\\UsageLedger（推荐）</small></button><button class="storage-option ${state.storagePath!=null?'active':''}" data-action="storage-pick"><strong>选择其他位置…</strong><small>${state.storagePath?esc(state.storagePath):'例如 D:\\智账数据 · 支持中文路径'}</small></button></div><button class="primary" data-action="storage-continue">继续 ${icon('arrow')}</button><p class="onboarding-note">应用安装位置和账本保存位置相互独立；以后可以在设置中安全迁移。</p>`;
  const backgroundHtml=`<div class="welcome-heading"><h1>你希望智账如何后台运行？</h1><p>两项都由你决定，之后可以随时在设置中更改。</p></div><div class="storage-options"><label class="storage-option"><span><input type="checkbox" id="bg-keep"> <strong>关闭窗口后继续后台运行</strong><small>关闭主窗口后智账留在系统托盘，账本更新不中断；不勾选则首次关闭时会再问你一次。</small></span></label><label class="storage-option"><span><input type="checkbox" id="bg-auto"> <strong>登录 Windows 后启动智账</strong><small>开机后自动进入系统托盘，不弹窗打扰。</small></span></label></div><button class="primary" data-action="background-continue">继续 ${icon('arrow')}</button><p class="onboarding-note">两项完全独立：可以只要托盘、只要开机启动、都选或都不选。</p>`;
  return `<div class="main first-run"><div class="first-head">${brand()}<small>${icon('shield')} 数据只保存在这台电脑</small></div><div class="onboarding"><div class="onboarding-steps">${['欢迎','偏好','找到 AI 工具','建立账本'].map((n,i)=>`<div class="step ${i===stage?'current':''}"><b>${i<stage?'✓':i+1}</b>${n}</div>`).join('')}</div><section class="welcome">${step==='welcome'?`<div class="welcome-heading"><img class="brand-logo" src="/assets/art/brand/zhizhang-128.png" alt="智账"><h1>欢迎使用智账</h1><p>把散落在不同 AI 工具里的使用记录，<br>留在自己手里。</p><p>从不同 AI 工具，汇到你真正做的项目。<br>看清每个项目、每场会话用了多少 AI。</p></div><div class="welcome-principles"><div>${icon('folder')}<h3>自动找到你使用的 AI 工具</h3><p>打开账本时自动检查这台电脑，<br>把分散的使用记录收在一起。</p></div><div>${icon('database')}<h3>留下属于自己的历史</h3><p>入账之后长期保留，<br>工具清理历史也不会丢失。</p></div><div>${icon('shield')}<h3>数据留在你的电脑</h3><p>本地运行，不上传完整路径，<br>没有遥测，也没有云同步。</p></div></div><button class="primary" data-action="onboard-storage">开始 ${icon('arrow')}</button><p class="onboarding-note">只检查标准安装位置的常见数据目录，不会搜索整台电脑，也不会上传任何信息。</p>`:step==='storage'?storageHtml:step==='background'?backgroundHtml:step==='discovered'?`<div class="welcome-heading"><h1>已找到 ${found.length} 个 AI 工具</h1><p>不同工具留下的信息不同，我们会如实记在账本里。</p></div><div class="discovered">${found.map(toolRow).join('')||`<div class="state-icon">${icon('folder')}</div><h1>暂时没有发现可读取的 AI 工具</h1><p class="subtitle">先在支持的工具中使用一次 AI，再回来重新发现。</p>`}</div>${found.length?`<button class="primary" data-action="first-scan">建立我的账本</button><p class="onboarding-note">没有具体用量的工具，也会留下使用过的记录。</p>`:`<button class="primary" data-action="discover">重新发现</button><p class="onboarding-note">也可以稍后在设置的「AI 工具」里手动配置数据源（高级）。</p>`}`:step==='scanning'?`<div class="state-icon">${icon('scan')}</div><h1>正在为你建立账本</h1><p class="subtitle">读取刚刚找到的 AI 工具，把使用记录保存在本机。</p><div class="scan-progress"><i></i></div><p class="onboarding-note">稍等片刻，完成后即可查看你的项目。首次更新账本可能需要几分钟。</p>`:`<div class="state-icon success">${icon('shield')}</div><h1>智账已经建立</h1><p class="subtitle">以后用过的 AI，都有据可查。</p><div class="onboarding-result"><div><strong>${(state.data.by_project||[]).length}</strong><span>个项目</span></div><div><strong>${(state.data.aggregate||{}).sessions||0}</strong><span>场 AI 会话</span></div><div><strong>${found.length}</strong><span>个 AI 工具</span></div></div><p class="quiet-explanation">部分工具只有使用痕迹，部分用量暂无价格。<br>这些差异都会明确说明，不会算成「免费」或「没有使用」。</p><button class="primary" data-nav="overview">查看总览 ${icon('arrow')}</button>`}</section><p class="onboarding-note">智账 · 每一次使用，慢慢沉淀为自己的长期记录。</p></div></div>`;
}
function confirmation(){return `<div class="scrim" data-action="cancel-confirm"></div><section class="modal" role="dialog" aria-modal="true" aria-label="确认整理项目"><div class="drawer-head"><h2>${state.pending.type==='alias'?'确认修改项目名称':state.pending.type==='merge'?'确认合并重复项目':'确认移动这些会话'}</h2><p class="subtitle">先核对目标项目，再完成整理。</p></div><div class="modal-body"><div class="preview"><div><strong>${esc(state.pending.description)}</strong><small>使用记录不会重复计算，原始 AI 工具文件保持不变。</small></div></div><p class="quiet-explanation">${state.pending.type==='alias'?'只改变显示名称，项目身份、会话和用量保持不变。':state.pending.type==='merge'?'整理后，这些自动识别结果会汇入同一个项目，原始来源仍可追溯。':'这些会话及使用记录会归到目标项目，工具、时间和用量都保持不变。手工归档不会被后续更新账本覆盖。'}</p></div><div class="modal-actions"><small>保存到本机账本 · 失败时保留原关系</small><div class="inline"><button data-action="cancel-confirm">返回修改</button><button class="primary" data-action="confirm-attribution">确认${state.pending.type==='alias'?'保存名称':state.pending.type==='merge'?'合并':'移动'}</button></div></div></section>`}
/* RC.3 安全迁移弹层（§29-36） */
function migrationOverlay(){
  const m=state.migration;
  if(!m)return '';
  if(m.phase==='running')return `<div class="scrim"></div><section class="modal" role="dialog" aria-modal="true" aria-label="正在迁移账本"><div class="drawer-head"><h2>正在迁移账本</h2><p class="subtitle">正在把账本安全地搬到新位置。</p></div><div class="modal-body"><div class="scan-progress"><i></i></div><p class="quiet-explanation">正在迁移账本，请稍候…<br>原来的账本会完整保留，直到新位置验证通过。</p></div></section>`;
  const plan=m.plan||{};
  const issues=plan.issues||[];
  const warnings=plan.warnings||[];
  const sizeText=plan.total_bytes?`${(plan.total_bytes/1048576).toFixed(1)} MB`:'少量文件';
  const issueHtml=issues.map(i=>`<div class="danger-note">${esc(i.message)}</div>`).join('');
  const warnHtml=warnings.map(w=>`<div class="state-notice loading"><span>${esc(w)}</span></div>`).join('');
  if(plan.blocked)return `<div class="scrim" data-action="migrate-cancel"></div><section class="modal" role="dialog" aria-modal="true" aria-label="无法迁移到该位置"><div class="drawer-head"><h2>无法迁移到该位置</h2><p class="subtitle">${esc(m.target)}</p></div><div class="modal-body">${issueHtml||'<p class="quiet-explanation">该位置不可用。</p>'}</div><div class="modal-actions"><div class="inline"><button data-action="migrate-cancel">取消</button><button class="primary" data-action="migrate-again">选择其他位置</button></div></div></section>`;
  if(plan.has_ledger)return `<div class="scrim" data-action="migrate-cancel"></div><section class="modal" role="dialog" aria-modal="true" aria-label="目标位置已有账本"><div class="drawer-head"><h2>这里已经有一本账</h2><p class="subtitle">${esc(m.target)}</p></div><div class="modal-body">${issueHtml}<p class="quiet-explanation">RC.3 不支持把两本账合并。你可以改用这里已有的账本（当前账本原样保留在原位置），或另选一个空文件夹。</p></div><div class="modal-actions"><div class="inline"><button data-action="migrate-cancel">取消</button><button data-action="migrate-again">选择其他位置</button><button class="primary" data-action="migrate-adopt">使用这里已有的账本</button></div></div></section>`;
  if(plan.nonempty_dir)return `<div class="scrim" data-action="migrate-cancel"></div><section class="modal" role="dialog" aria-modal="true" aria-label="目标目录包含其它文件"><div class="drawer-head"><h2>请选择空文件夹</h2><p class="subtitle">${esc(m.target)}</p></div><div class="modal-body">${issueHtml}<p class="quiet-explanation">智账不会覆盖目录里的其它文件。你可以另选一个空文件夹，或在该目录下新建一个子文件夹（如「智账数据」）再选择它。</p></div><div class="modal-actions"><div class="inline"><button data-action="migrate-cancel">取消</button><button class="primary" data-action="migrate-again">选择其他位置</button></div></div></section>`;
  return `<div class="scrim" data-action="migrate-cancel"></div><section class="modal" role="dialog" aria-modal="true" aria-label="确认迁移账本"><div class="drawer-head"><h2>确认迁移账本</h2><p class="subtitle">迁移完成后，智账将改用新位置。</p></div><div class="modal-body"><div class="preview"><div><strong>${esc(plan.current_root||'当前位置')} → ${esc(m.target)}</strong><small>${(plan.durable||[]).length} 个文件 · 约 ${sizeText} · 运行状态与日志不搬运</small></div></div>${warnHtml}<p class="quiet-explanation">迁移期间会暂停账本更新；原位置的账本会完整保留，不会自动删除。任何一步失败都会回到原账本。</p></div><div class="modal-actions"><div class="inline"><button data-action="migrate-cancel">取消</button><button class="primary" data-action="migrate-start">开始迁移</button></div></div></section>`;
}
function migrationFailedOverlay(){
  const e=state.migrationError||{};
  return `<div class="scrim" data-action="migrate-cancel"></div><section class="modal" role="dialog" aria-modal="true" aria-label="迁移没有完成"><div class="drawer-head"><h2>迁移没有完成</h2><p class="subtitle">智账仍在使用原来的账本，数据没有丢失。</p></div><div class="modal-body"><p class="quiet-explanation">${esc(e.message||'迁移过程中出现问题，已安全停止。')}</p></div><div class="modal-actions"><div class="inline"><button class="primary" data-action="migrate-cancel">知道了</button></div></div></section>`;
}
/* RC.3 更新器 / 更新记录 / What's New（§40-59） */
function updateOverlay(){
  const u=state.update;if(!u)return '';
  if(u.phase==='failed')return `<div class="scrim" data-action="update-dismiss"></div><section class="modal" role="dialog" aria-modal="true" aria-label="更新失败"><div class="drawer-head"><h2>更新失败</h2><p class="subtitle">当前版本不受影响，可以继续正常使用。</p></div><div class="modal-body"><p class="quiet-explanation">${esc(u.message||'更新过程中出现问题，已安全停止。')}</p>${(u.message||'').includes('签名')?'<p class="quiet-explanation">为保护你的数据，签名验证失败的更新包已被拒绝安装，且不提供跳过验证的选项。</p>':''}</div><div class="modal-actions"><div class="inline"><button class="primary" data-action="update-dismiss">知道了</button></div></div></section>`;
  if(u.phase==='available'||u.phase==='downloading'){
    const p=u.progress;const pct=p&&p.total?Math.min(100,Math.round(p.downloaded/p.total*100)):null;
    const notes=String(u.notes||'').split(/\r?\n/).filter(Boolean).map(l=>`<li>${esc(l)}</li>`).join('');
    return `<div class="scrim" data-action="update-dismiss"></div><section class="modal" role="dialog" aria-modal="true" aria-label="发现新版本"><div class="drawer-head"><h2>发现新版本 ${esc(u.version)}</h2><p class="subtitle">当前版本 ${esc(u.current||'')} · 更新内容：</p></div><div class="modal-body">${notes?`<ul class="whatsnew-list">${notes}</ul>`:''}${u.phase==='downloading'?`<div class="scan-progress"><i></i></div><p class="quiet-explanation">${pct!=null?`已下载 ${pct}%（签名验证将在下载完成后自动进行）`:'正在连接更新源…'}</p>`:'<p class="quiet-explanation">下载完成后会自动验证数字签名；验证通过才会安装，验证失败将拒绝安装。</p>'}</div><div class="modal-actions"><div class="inline"><button data-action="update-dismiss" ${u.phase==='downloading'?'disabled':''}>以后再说</button>${u.phase==='available'?`<button class="primary" data-action="download-update">下载更新</button>`:''}</div></div></section>`;
  }
  return '';
}
function changelogOverlay(cl){
  const versions=(cl&&cl.versions)||[];
  return `<div class="scrim" data-action="update-dismiss"></div><section class="modal" role="dialog" aria-modal="true" aria-label="更新记录"><div class="drawer-head"><h2>更新记录</h2><p class="subtitle">智账的版本变化一览。</p></div><div class="modal-body">${versions.map(v=>`<div class="changelog-entry"><h3>${esc(v.version)} <small>${esc(v.date||'')}</small></h3><ul class="whatsnew-list">${(v.highlights||[]).map(h=>`<li>${esc(h)}</li>`).join('')}</ul></div>`).join('')||'<p class="quiet-explanation">暂无更新记录。</p>'}</div><div class="modal-actions"><div class="inline"><button class="primary" data-action="update-dismiss">关闭</button></div></div></section>`;
}
function whatsNewOverlay(version,highlights){
  return `<div class="scrim"></div><section class="modal" role="dialog" aria-modal="true" aria-label="这次更新了什么"><div class="drawer-head"><h2>智账已更新到 ${esc(version)}</h2><p class="subtitle">这次更新了什么</p></div><div class="modal-body"><ul class="whatsnew-list">${(highlights||[]).slice(0,5).map(h=>`<li>${esc(h)}</li>`).join('')}</ul></div><div class="modal-actions"><div class="inline"><button data-action="show-changelog">查看完整更新记录</button><button class="primary" data-action="update-dismiss">开始使用</button></div></div></section>`;
}
function render(){
  const mainScroll=document.querySelector('.main')?.scrollTop||0,drawerScroll=document.querySelector('.drawer-content')?.scrollTop||0;
  let content;
  if(state.view==='first-run')content=firstRun();
  else if(state.booted===false)content=`<div class="page-header"><div><div class="eyebrow">总览</div><h1>正在打开账本…</h1></div></div><div class="state-loading" style="padding:60px 0;text-align:center;color:var(--ink-3)">正在读取本机保存的记录。</div>`;
  else if(state.fatal)content=header('暂时无法打开账本','总览','已有数据不会被覆盖或清除。')+emptyCard('账本还在，稍后再试一次',esc(state.fatal),'retry-ledger','重新读取','database');
  else if(state.view==='settings')content=settings();
  else if(state.view==='explore')content=state.data?explore():loadingBlock();
  else content=state.data?overview():loadingBlock();
  document.getElementById('app').innerHTML=state.view==='first-run'?content:shell(content);
  document.getElementById('overlay-root').innerHTML=state.overlay==='project'?'':state.overlay==='session'?'':state.overlay==='attribution'?attribution():state.overlay==='confidence'?confidence():state.overlay==='confirmation'?confirmation():state.overlay==='whatsnew'?whatsNewOverlay(state.whatsNewData&&state.whatsNewData.ver,state.whatsNewData&&state.whatsNewData.items):state.overlay==='update'?updateOverlay():state.overlay==='changelog'?changelogOverlay(state.changelogData):state.overlay==='migration'?migrationOverlay():state.overlay==='migration-failed'?migrationFailedOverlay():'';
  document.querySelector('.main')?.toggleAttribute('inert',!!state.overlay);document.querySelector('.sidebar')?.toggleAttribute('inert',!!state.overlay);
  if(document.querySelector('.main'))document.querySelector('.main').scrollTop=mainScroll;if(document.querySelector('.drawer-content'))document.querySelector('.drawer-content').scrollTop=drawerScroll;
  if(state.overlay)queueMicrotask(()=>document.querySelector('[role="dialog"] input:not([type=checkbox]),[role="dialog"] button')?.focus({preventScroll:true}));
}
function loadingBlock(){return '<div style="padding:60px 0;text-align:center;color:var(--ink-3)">正在读取…</div>'}
let overlayEl=null;
function renderOverlay(html){
  if(overlayEl)overlayEl.remove();
  overlayEl=document.createElement('div');
  overlayEl.innerHTML=html;
  document.getElementById('overlay-root').replaceChildren(overlayEl);
  document.querySelector('.main')?.setAttribute('inert','');
  document.querySelector('.sidebar')?.setAttribute('inert','');
  queueMicrotask(()=>overlayEl.querySelector('[role="dialog"] input:not([type=checkbox]),[role="dialog"] button')?.focus({preventScroll:true}));
  return overlayEl;
}
function closeOverlay(){
  if(overlayEl){overlayEl.remove();overlayEl=null;}
  document.querySelector('.main')?.removeAttribute('inert');
  document.querySelector('.sidebar')?.removeAttribute('inert');
  document.getElementById('overlay-root').replaceChildren();
  state.overlay=null;state.moveSession=null;
}
let toastTimer;
function toast(message){clearTimeout(toastTimer);const el=document.getElementById('toast');el.textContent=message;el.className='show';toastTimer=setTimeout(()=>el.className='',3800)}
function nav(view,resetEvent=true){
  state.view=view;closeOverlay();state.eventPage=0;if(resetEvent)state.chosenEvent=null;
  if(view!=='explore')state.audit='effective';
  history.replaceState({},'','/'+(view==='overview'?'':view));
  // 立即反馈：先渲染目标页壳（active 态 + 已有数据/空态），数据随后异步刷新
  render();
  refreshAndRender();
}
async function scan(){
  if(state.scan==='running')return;
  state.scan='running';render();
  try{
    const result=await post('/api/v1/scan',{});
    state.scan='done';state.scanResult=scanOutcome(result.status);
    await loadHealth();await loadUnassigned();await loadQuery();
    render();toast(state.scanResult==='success'?'账本已更新':state.scanResult==='partial'?'账本已更新，部分工具需要检查':'暂时没能更新，已有账本仍保留');
  }catch(e){
    if(e.status===409){state.scan='idle';await loadHealth();render();toast('已有更新正在进行，请稍后再试');return;}
    state.scan='done';state.scanResult='error';await loadHealth();render();
    toast('暂时没能更新，已有账本仍保留');
  }
}
let refreshGen=0;
let refreshPending=0;
async function refreshAndRender(){
  const gen=++refreshGen;
  refreshPending++;
  try{
    await loadQuery();
    if(gen!==refreshGen)return;   // 已有更新的交互：本次结果过期，不渲染
    state.error=null;state.booted=true;
  }catch(e){
    if(gen!==refreshGen)return;
    state.booted=true;
    if(e.status===0||e.status===404){state.fatal='本地服务不可达（'+e.message+'）';}
    else state.fatal=e.message;
  }finally{
    refreshPending--;
  }
  render();
}
/* ---------------- 事件 ---------------- */
document.addEventListener('click',async e=>{
  const b=e.target.closest('button,[data-action],tr[data-project],tr[data-event-open],tr[data-explore-event]');if(!b||b.disabled)return;
  if(b.dataset.nav){nav(b.dataset.nav);return}
  if(b.dataset.range){state.range=b.dataset.range;state.eventPage=0;render();refreshAndRender();return}
  if(b.dataset.project){
    const p=projectList().find(x=>x.key===b.dataset.project);
    if(p){if(p.key==='__unassigned'){state.attributionTab='move';state.moveSession=null;state.origin='overview';renderOverlay(attribution());state.overlay='attribution';}
      else await openProjectDrawer(p);}
    return}
  if(b.dataset.session){await openSessionDrawer(b.dataset.session);return}
  if(b.dataset.exploreProject){
    state.chosenProject=b.dataset.exploreProject==='__unassigned'?'__unassigned':b.dataset.exploreProject;
    // 下钻保留项目候选列表，仅筛选右侧会话与记录。
    state.eventPage=0;state.chosenEvent=null;state.chosenSession=null;
    render();refreshAndRender();return}
  if(b.dataset.exploreSession){state.chosenSession=b.dataset.exploreSession;state.eventPage=0;state.chosenEvent=null;render();refreshAndRender();return}
  if(b.dataset.exploreEvent){state.chosenEvent=b.dataset.exploreEvent;render();return}
  if(b.dataset.eventOpen){state.chosenEvent=b.dataset.eventOpen;nav('explore',false);return}
  if(b.dataset.attrTab){state.attributionTab=b.dataset.attrTab;state.moveSession=null;renderOverlay(attribution());return}
  if(b.dataset.setting){state.settingTab=b.dataset.setting;if(b.dataset.openDisclosure)state.disclosures[b.dataset.openDisclosure]=true;history.replaceState({},'','/settings');render();document.querySelector('.main').scrollTop=0;return}
  const act=b.dataset.action;
  if(act==='close'){closeOverlay();}
  else if(act==='scan')scan();
  else if(act==='confidence'){state.overlay='confidence';renderOverlay(confidence());}
  else if(act==='continue-reading'){document.querySelector('.project-ledger')?.scrollIntoView({block:'nearest'});}
  else if(act==='organize-unassigned'){state.attributionTab='move';state.moveSession=null;renderOverlay(attribution());state.overlay='attribution';}
  else if(act==='organize-merge'){state.attributionTab='merge';state.moveSession=null;renderOverlay(attribution());state.overlay='attribution';}
  else if(act==='attribution'||act==='alias'){state.attributionTab='alias';state.moveSession=null;renderOverlay(attribution());state.overlay='attribution';}
  else if(act==='move-session'){closeOverlay();state.attributionTab='move';state.moveSession=state.chosenSession;renderOverlay(attribution());state.overlay='attribution';}
  else if(act==='back-project'){const p=chosenProjectEntry();if(p&&p.key!=='__unassigned')openProjectDrawer(p);else closeOverlay();}
  else if(act==='open-explore'||act==='open-session-explore'){closeOverlay();nav('explore');}
  else if(act==='clear-filters'){state.range='all';state.project='all';state.client='all';state.model='all';state.eventPage=0;render();refreshAndRender();}
  else if(act==='next-page'){state.eventPage++;render();refreshAndRender();}
  else if(act==='prev-page'){state.eventPage=Math.max(0,state.eventPage-1);render();refreshAndRender();}
  else if(act==='close-event'){state.chosenEvent=null;render();}
  else if(act==='confidence-settings'){state.settingTab='sources';nav('settings');}
  else if(act==='discover'){state.onboarding='discovered';await loadDiscover(true);render();}
  else if(act==='first-scan'){
    if(state.onboarding==='scanning')return;
    state.onboarding='scanning';render();
    try{
      const result=await post('/api/v1/scan',{});
      await loadHealth();await loadDiscover(true);await loadUnassigned();await loadQuery();
      if((scanOutcome(result.status)==='success'||scanOutcome(result.status)==='partial')&&state.data.ledger_state?.has_records){
        state.onboarding='complete';
        try{sessionStorage.removeItem('zhizhang.onboarding');}catch(e){}
        /* §58：First Run 完成即记录当前版本，不再弹 What's New */
        tauriInvoke('complete_first_run').catch(()=>{});
      }else{state.onboarding='discovered';toast('暂时没能建立账本，请检查来源后重试');}
    }catch(err){state.onboarding='discovered';toast(err.status===409?'已有更新正在进行，请稍后重试':'暂时没能建立账本，请重试');}
    render();
  }
  else if(act==='rediscover'){
    if(state.discovering)return;
    state.discovering=true;render();
    try{state.discover=await post('/api/v1/discover/refresh',{});}
    catch(e){if(e.status!==409)toast('重新发现失败，请稍后再试');}
    state.discovering=false;render();
    const d=state.discover||{};
    toast(`已发现 ${d.found!=null?d.found:(d.tools||[]).length} 个 AI 工具`);}
  else if(act==='data-folder'){
    if(window.__TAURI__&&window.__TAURI__.core){try{await window.__TAURI__.core.invoke('open_data_root');}catch(err){toast('打开数据目录失败');}}
    else toast('桌面版中可在此打开数据目录');}
  else if(act==='toggle-close-behavior'){
    const on=!b.classList.contains('on');
    try{await tauriInvoke('set_close_behavior',{behavior:on?'background':'exit'});await loadDesktop();render();
      toast(on?'已开启：关闭窗口后智账保持后台运行':'已关闭：关闭窗口将完全退出智账');}
    catch(err){toast('设置未保存，请重试');}}
  else if(act==='toggle-autostart'){
    const on=!b.classList.contains('on');
    try{const real=await tauriInvoke('set_autostart',{enable:on});await loadDesktop();render();
      toast(real?'已开启：登录 Windows 后智账自动启动':'已关闭开机启动');}
    catch(err){toast('设置未保存，请重试');}}
  /* ---- RC.3 First Run：数据位置 / 后台方式 ---- */
  else if(act==='onboard-storage'){state.onboarding='storage';render();}
  else if(act==='storage-default'){state.storagePath=null;render();}
  else if(act==='storage-pick'){
    try{const picked=await tauriInvoke('pick_folder');
      if(picked){state.storagePath=picked;render();}}
    catch(err){toast('无法打开文件夹选择器');}}
  else if(act==='storage-continue'){
    state.onboarding='background';
    try{sessionStorage.setItem('zhizhang.onboarding','background');}catch(e){}
    if(state.storagePath){
      render();toast('正在应用新的账本位置…');
      try{await tauriInvoke('set_data_root',{path:state.storagePath});
        // backend 已在新位置重启；页面将被导航，sessionStorage 续步
      }catch(err){toast('该位置不可用：'+(err.message||err));state.onboarding='storage';render();}
    }else{render();await loadDiscover(true);}}
  else if(act==='background-continue'){
    try{
      if(document.getElementById('bg-keep')?.checked)await tauriInvoke('set_close_behavior',{behavior:'background'});
      if(document.getElementById('bg-auto')?.checked){
        const real=await tauriInvoke('set_autostart',{enable:true});
        if(!real)toast('开机启动注册未生效，可稍后在设置中重试');
      }
    }catch(err){toast('部分偏好未保存，可稍后在设置中更改');}
    state.onboarding='discovered';
    try{sessionStorage.setItem('zhizhang.onboarding','discovered');}catch(e){}
    await loadDiscover(true);render();}
  /* ---- RC.3 安全迁移（设置 → 数据与隐私 → 更改位置） ---- */
  else if(act==='migrate-pick'){
    try{
      const picked=await tauriInvoke('pick_folder');
      if(!picked)return;
      const plan=await tauriInvoke('migrate_data_root_plan',{target:picked});
      state.migration={phase:'confirm',target:picked,plan};
      state.overlay='migration';render();
    }catch(err){toast(err&&err.message?err.message:'无法使用该位置');}}
  else if(act==='migrate-again'){
    state.migration=null;renderOverlay('');
    document.querySelector('[data-action="migrate-pick"]')?.click();}
  else if(act==='migrate-cancel'){state.migration=null;state.migrationError=null;closeOverlay();render();}
  else if(act==='migrate-adopt'){
    const target=state.migration&&state.migration.target;if(!target)return;
    state.migration={phase:'running',target};state.overlay='migration';render();
    try{await tauriInvoke('set_data_root',{path:target});
      /* backend 重启后导航回账本 */}
    catch(err){state.migration=null;state.migrationError={message:err.message||String(err)};state.overlay='migration-failed';render();}}
  else if(act==='migrate-start'){
    const target=state.migration&&state.migration.target;if(!target)return;
    state.migration={phase:'running',target};state.overlay='migration';render();
    try{await tauriInvoke('migrate_data_root_execute',{target:target});
      /* 成功：Rust 切换 bootstrap、重启 backend 并导航回设置页 */}
    catch(err){
      state.migration=null;state.migrationError={message:err&&err.message?err.message:String(err)};
      state.overlay='migration-failed';render();}}
  /* ---- RC.3 更新器（仅用户显式触发，§41） ---- */
  else if(act==='check-update'){
    if(!window.__TAURI__){toast('桌面版中可以使用检查更新');return;}
    state.update={phase:'checking'};render();
    try{
      const r=await tauriInvoke('check_for_update');
      if(r.status==='not_configured'){state.update=null;render();
        toast('更新源未配置：本版本暂未接入在线更新通道，功能不受影响');}
      else if(r.status==='up_to_date'){state.update=null;render();toast('当前已是最新版本');}
      else if(r.status==='available'){
        state.update={phase:'available',version:r.version,current:r.current,notes:r.notes||''};
        state.overlay='update';render();}
    }catch(err){
      state.update={phase:'failed',message:err&&err.message?err.message:String(err)};
      state.overlay='update';render();}}
  else if(act==='download-update'){
    state.update={...state.update,phase:'downloading',progress:null};
    state.overlay='update';render();
    try{await tauriInvoke('download_and_install_update');
      /* 成功路径：安装器启动后应用退出 */}
    catch(err){
      state.update={phase:'failed',message:err&&err.message?err.message:String(err)};
      render();}}
  else if(act==='update-dismiss'){state.update=null;closeOverlay();render();}
  else if(act==='show-changelog'){
    try{const cl=await api('/v1/changelog.json');state.changelogData=cl;state.overlay='changelog';render();}
    catch(err){toast('暂时无法读取更新记录');}}
  else if(act==='retry-ledger'){state.fatal=null;await refreshAndRender();toast('正在重新读取');}
  else if(act==='cancel-confirm'){state.pending=null;renderOverlay(attribution());state.overlay='attribution';}
  else if(act==='apply-attribution')applyAttribution();
  else if(act==='confirm-attribution')confirmAttribution();
});
document.addEventListener('change',async e=>{
  if(e.target.dataset.filter){state[e.target.dataset.filter]=e.target.value;state.eventPage=0;await refreshAndRender();}
  else if(e.target.id==='project-sort'){state.sort=e.target.value;render()}
  else if(e.target.id==='audit-view'){state.audit=e.target.value;state.eventPage=0;state.chosenEvent=null;await refreshAndRender();}
  else if(e.target.id==='move-target'){state.moveTarget=e.target.value;const x=projectList().find(p=>p.dbKey===e.target.value);document.getElementById('move-preview').textContent=x?x.name:''}
  else if(e.target.name==='merge-projects'){document.getElementById('merge-count').textContent=`${document.querySelectorAll('[name="merge-projects"]:checked').length} 个项目`}
});
document.addEventListener('input',e=>{if(e.target.id==='alias-input')document.getElementById('alias-preview').textContent=e.target.value||'请输入项目名称'});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&overlayEl){e.preventDefault();closeOverlay();}
  if((e.key==='Enter'||e.key===' ')&&e.target.matches('tr[role=button]')){e.preventDefault();e.target.click()}
  if(e.key==='Tab'&&overlayEl){const list=[...overlayEl.querySelectorAll('[role=dialog] button:not(:disabled),[role=dialog] input,[role=dialog] select,[role=dialog] summary')].filter(x=>x.getClientRects().length);const first=list[0],last=list[list.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}}});
document.addEventListener('toggle',e=>{if(e.target.isConnected&&e.target.dataset?.disclosure)state.disclosures[e.target.dataset.disclosure]=e.target.open},true);
/* ---------------- 整理项目（真实写入） ---------------- */
function applyAttribution(){
  const p=chosenProjectEntry();
  if(state.attributionTab==='alias'){
    const value=document.getElementById('alias-input').value.trim();
    if(!value){toast('请输入显示名称');return}
    state.pending={type:'alias',name:value,description:`「${p.name}」改名为「${value}」`};
  }else if(state.attributionTab==='move'){
    const ids=[...document.querySelectorAll('[name="move-sessions"]:checked')].map(x=>x.value);
    if(!ids.length){toast('请至少选择一场会话');return}
    const target=document.getElementById('move-target').value;
    state.pending={type:'move',ids,target,description:`${ids.length} 场会话 → ${(projectList().find(x=>x.dbKey===target)||{}).name||target}`};
  }else{
    const ids=[...document.querySelectorAll('[name="merge-projects"]:checked')].map(x=>x.value);
    if(!ids.length){toast('请选择要合入的项目');return}
    state.pending={type:'merge',ids,target:p.dbKey,description:`${ids.map(id=>{const x=projectList().find(y=>y.dbKey===id);return x?x.name:id}).join('、')} → ${p.name}`};
  }
  renderOverlay(confirmation());state.overlay='confirmation';
}
async function confirmAttribution(){
  const p=state.pending;
  try{
    if(p.type==='alias'){
      await post('/api/v1/projects/alias',{alias:p.name,project_key:chosenProjectEntry()?.dbKey,display_name:p.name});
      state.renamed=state.renamed||{};state.renamed[state.chosenProject]=p.name;
      toast('项目名称已保存，电脑上的文件保持不变');
    }else if(p.type==='move'){
      const sessions=p.ids.map(id=>{const [source,...rest]=id.split('|');return {source,session_id:rest.join('|')}});
      const r=await post('/api/v1/projects/assign',{project_key:p.target,sessions});
      toast(`已归档 ${r.events_updated} 条使用记录 · 后续更新账本不会覆盖`);
    }else{
      let moved=0;
      for(const from of p.ids){
        const r=await post('/api/v1/projects/merge',{from,to:p.target});
        moved+=r.events_moved;
      }
      toast(`已合并 ${moved} 条使用记录到目标项目`);
    }
    state.pending=null;closeOverlay();
    await Promise.all([loadUnassigned(),loadHealth(),refreshAndRender()]);
  }catch(e){
    toast('保存失败：'+e.message+'（原关系保持不变）',true);
    state.pending=null;closeOverlay();
  }
}
/* ---------------- 启动 ---------------- */
/* RC.3：迁移期间的关闭/退出请求 → Rust 发事件 → 显示迁移中提示（§36） */
try{const ev=window.__TAURI__&&window.__TAURI__.event;
  if(ev&&ev.listen){
    ev.listen('migration-in-progress',()=>{
      if(!state.migration){state.migration={phase:'running',target:''};state.overlay='migration';render();}
      else toast('正在迁移账本，请稍候…');});
    /* 下载进度（§49） */
    ev.listen('update-download-progress',p=>{
      if(state.update&&state.update.phase==='downloading'){
        state.update.progress=p;render();}});}
}catch(e){}
(async function boot(){
  render();
  await Promise.all([loadIdentity(),loadHealth(),loadDiscover(),loadDesktop()]);  // 桌面壳状态失败 = 浏览器开发态
  await loadUnassigned();
  await loadReplayCount();
  // 空账本（首次使用）：overview 无数据且无任何会话 → First Run
  try{
    await loadQuery();
    state.booted=true;state.error=null;
    const a=(state.data.aggregate||{});
    if(!state.data.ledger_state?.has_records&&state.view==='overview'){state.view='first-run';}
  }catch(e){
    state.booted=true;
    if(e.status===0||e.status===404)state.fatal='本地服务不可达：'+e.message;
    else state.fatal=e.message;
  }
  render();
})();
