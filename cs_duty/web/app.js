"use strict";
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = name => `<i data-lucide="${name}"></i>`;
const labels = {stopped:"未启动",starting:"启动中",running:"接待中",paused:"已暂停",stopping:"停止中",waiting_login:"等待登录",error:"需检查",active:"接待中",collecting:"收集需求",waiting:"等待同事",human:"同事接管",draft:"草稿",ready:"排队中",sending:"发送中",sent:"已发送",stale:"已过期",uncertain:"待核对",cancelled:"已取消",open:"待处理",resolved:"已完成",pending:"未通知",failed:"通知失败",ignored:"无需回复"};
const fields = {product:"产品型号",requirements:"定制内容",quantity:"数量",deadline:"期望交期",contact:"联系方式",company:"公司或称呼"};
const viewNames = {overview:"运行总览",conversations:"客户会话",tasks:"同事待办",knowledge:"业务知识",models:"模型配置",settings:"接待设置",data:"数据管理"};
let state, activeView="overview", selectedCustomer="", initialized=false, toastTimer, historyVersion="", historyGeneration=0;
const edits = new Map(), taskResults = new Map();
function hydrate(){lucide.createIcons({attrs:{"stroke-width":1.7}});}
function setHTML(el, html){if(el.contains(document.activeElement)&&document.activeElement.matches("textarea,input"))return;if(el.innerHTML!==html)el.innerHTML=html;}
function badge(value){return `<span class="badge ${["error","uncertain","failed"].includes(value)?"error":["draft","waiting","open","waiting_login"].includes(value)?"warning":["stopped","cancelled","stale","human"].includes(value)?"neutral":""}">${esc(labels[value]||value)}</span>`;}
function date(value){return new Date(value*1000).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});}
function empty(title, detail, symbol="inbox"){return `<div class="empty">${icon(symbol)}<h3>${esc(title)}</h3><p>${esc(detail)}</p></div>`;}
function toast(message, error=false){clearTimeout(toastTimer);const el=$("#toast");el.textContent=message;el.classList.toggle("error",error);el.hidden=false;toastTimer=setTimeout(()=>el.hidden=true,error?9000:4500);}
async function api(path, body){const response=await fetch('/api/manage/'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-CS-Duty':'1'},body:body===undefined?undefined:JSON.stringify(body)});const value=await response.json();if(!response.ok)throw new Error(value.error||'请求失败');return value;}
function showView(name){if(!viewNames[name])return;activeView=name;$$('.view').forEach(el=>el.hidden=el.id!==`view-${name}`);$$('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.view===name));$('#page-name').textContent=viewNames[name];if(name==='knowledge')loadKnowledge();if(name==='conversations')loadHistory(true);hydrate();}

function render(){
  const runtime=state.runtime;
  $('#connection').innerHTML='<span class="local-dot"></span>本机已连接';
  $('#version').textContent='v'+state.version;
  for(const key of ['conversations','sent','drafts','tasks'])$('#stat-'+key).textContent=state.counts[key];
  $('#nav-count').textContent=state.counts.conversations;
  $('#runtime-title').textContent=({stopped:'服务已就绪',starting:'正在连接工作台',running:'正在接待客户',paused:'接待已暂停',stopping:'正在结束运行',waiting_login:'等待客服网页登录',error:'连接需要检查'})[runtime.state]||'客服接待';
  $('#runtime-badge').outerHTML=badge(runtime.state).replace('<span ','<span id="runtime-badge" ');
  $('#runtime-detail').textContent=runtime.detail;
  $('#runtime-target').textContent=state.config.transport==='mock'?'模拟页面':'真实页面 · 京东京麦';
  $('#runtime-mode').textContent=state.config.mode==='auto'?'自动发送':'填写草稿';
  const active=state.profiles.items.find(p=>p.id===state.profiles.active);
  $('#runtime-model').textContent=active?.model||'尚未配置';
  $('#start').disabled=runtime.running&&runtime.state!=='paused';
  $('#start').innerHTML=icon('play')+(runtime.state==='paused'?'继续接待':'开始接待');
  $('#pause').disabled=!['running','waiting_login'].includes(runtime.state);
  $('#stop').disabled=!runtime.running||runtime.state==='stopping';
  setHTML($('#events'),state.events.length?state.events.map(e=>`<div class="event"><span class="event-dot"></span><div><p>${esc(e.text)}</p><small>${date(e.created)}</small></div></div>`).join(''):empty('运行记录将在这里显示','开始接待后，可以在这里查看异常与恢复信息。','activity'));
  renderReplies();renderConversations();renderTasks();renderProfiles();
  if(!initialized){fillSettings();if(active)fillProfile(active);else fillProfile();initialized=true;}
  renderFeishu();
  if(activeView==='conversations')loadHistory();
  hydrate();
}
function renderReplies(){
  setHTML($('#outbox'),state.outbox.length?state.outbox.map(o=>`<div class="reply-card" data-id="${esc(o.id)}"><div class="card-title"><b>${esc(o.name)}</b>${badge(o.status)}<time>${date(o.created)}</time></div>${o.status==='draft'?`<textarea data-edit="${esc(o.id)}" aria-label="${esc(o.name)}的回复草稿" maxlength="2000">${esc(edits.get(o.id)??o.reply)}</textarea>`:`<p>${esc(o.reply)}</p>`}${o.reason?`<p class="reason">${esc(o.reason)}</p>`:''}<div class="card-actions">${o.status==='draft'?'<button class="button quiet" data-outbox="cancel">取消</button><button class="button primary" data-outbox="approve">发送回复</button>':o.status==='uncertain'?'<button class="button secondary" data-outbox="cancel">核对后取消</button><button class="button primary" data-outbox="confirmed">确认网页已发送</button>':''}</div></div>`).join(''):empty('暂无待处理回复','新消息生成的回复会出现在这里。填写草稿模式不会自动点击网页发送按钮。','file-pen-line'));
}
function renderConversations(){
  if(selectedCustomer&&!state.conversations.some(c=>c.id===selectedCustomer)){
    selectedCustomer='';historyVersion='';historyGeneration++;
    $('#conversation-detail').innerHTML=empty('选择一个客户','客户记录已更新。');
  }
  if(!selectedCustomer&&state.conversations.length)selectedCustomer=state.conversations[0].id;
  setHTML($('#conversation-list'),state.conversations.length?state.conversations.map(c=>`<button class="customer-row ${c.id===selectedCustomer?'active':''}" data-customer="${esc(c.id)}"><b>${esc(c.name)}</b>${badge(c.state)}<small>${esc(c.shop)}</small></button>`).join(''):empty('暂无客户','开始接待后自动同步。'));
}
async function loadHistory(force=false){
  if(!selectedCustomer)return;
  const c=state?.conversations.find(c=>c.id===selectedCustomer);
  if(!c)return;
  const version=c.id+':'+c.latest_id+':'+c.state+':'+c.fields;
  if(!force&&version===historyVersion)return;
  const token=++historyGeneration;
  try{
    const data=await api('history?id='+encodeURIComponent(selectedCustomer));
    if(token!==historyGeneration)return;
    historyVersion=version;
    const d=data.conversation;
    $('#conversation-detail').innerHTML=`<div class="customer-detail-header"><div><h2>${esc(d.name)}</h2><p>${esc(d.shop)} · ${d.platform==='mock'?'模拟环境':'京东京麦'}</p></div><button class="button secondary" data-takeover="${d.state==='human'?'resume':'takeover'}">${d.state==='human'?'恢复接待':'同事接管'}</button><button class="button quiet" data-delete="clear">清空聊天</button><button class="button danger" data-delete="delete">删除客户</button></div><div class="chat-history">${data.messages.map(m=>`<div class="chat-turn ${m.role==='agent'?'agent':''}"><small>${m.role==='agent'?'客服同事':m.role==='customer'?'客户':'系统'} · ${esc(m.timestamp)}</small><p>${esc(m.text)}</p></div>`).join('')}</div>${Object.keys(d.fields).length?`<dl class="customer-fields">${Object.entries(d.fields).map(([k,v])=>`<dt>${esc(fields[k]||k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`:''}`;
    const box=$('.chat-history');box.scrollTop=box.scrollHeight;hydrate();
  }catch(e){toast(e.message,true);}
}
function renderTasks(){
  setHTML($('#task-list'),state.tasks.length?state.tasks.map(t=>{let values={};try{values=JSON.parse(t.fields);}catch{}return `<article class="panel task" data-id="${esc(t.id)}"><div class="card-title"><b>${esc(t.name)}</b>${badge(t.status)}<time>${date(t.created)}</time></div><p class="summary">${esc(t.summary)}</p>${Object.keys(values).length?`<dl>${Object.entries(values).map(([k,v])=>`<dt>${esc(fields[k]||k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`:''}<small>飞书通知：${esc(labels[t.notification]||t.notification)} · 待办 ${esc(t.id.slice(0,8))}</small>${t.status==='open'?`<textarea data-result="${esc(t.id)}" aria-label="${esc(t.name)}的处理结果" placeholder="填写已确认的结果，客服会据此继续回复客户…" maxlength="5000">${esc(taskResults.get(t.id)||'')}</textarea><div class="card-actions"><button class="button primary" data-resolve>提交处理结果</button></div>`:`<p class="summary">${esc(t.result||'')}</p>${t.status==='ready'?'<small>已保存，开始或继续接待后处理。</small>':''}`}</article>`}).join(''):empty('当前没有待办','需要同事确认的问题，以及收集完整的定制需求，会自动汇总到这里。','clipboard-check'));
}
function renderProfiles(){
  setHTML($('#profiles'),state.profiles.items.length?state.profiles.items.map(p=>`<div class="profile" data-id="${esc(p.id)}"><h3>${esc(p.name)}${p.id===state.profiles.active?'<span class="badge">当前使用</span>':''}</h3><p>${esc(p.model)} · ${p.protocol==='anthropic'?'Anthropic':'OpenAI'}</p><p>${esc(p.base_url)}</p><div class="card-actions"><button class="button secondary" data-profile="edit">编辑</button><button class="button secondary" data-profile="test">测试连接</button>${p.id!==state.profiles.active?'<button class="button quiet" data-profile="activate">启用</button>':''}</div></div>`).join(''):empty('添加第一个模型连接','填写 API 地址与密钥，测试通过后即可开始接待。','blocks'));
}
function fillProfile(profile={}){
  const form=$('#profile-form');form.reset();
  for(const key of ['id','name','protocol','base_url','model','timeout','max_tokens'])if(profile[key]!==undefined)form.elements.namedItem(key).value=profile[key];
  if(!profile.id){form.elements.base_url.value='https://api.minimax.cn/anthropic';form.elements.model.value='MiniMax-M3';}
  form.elements.api_key.value='';form.elements.api_key.required=!profile.has_key;
  $('#profile-form-title').textContent=profile.id?'编辑模型连接':'新增模型连接';
}
function fillSettings(){
  const form=$('#settings-form');
  for(const [key,value] of Object.entries(state.config)){
    const input=form.elements.namedItem(key);if(!input)continue;
    if(input.type==='checkbox')input.checked=Boolean(value);else input.value=Array.isArray(value)?value.join('\n'):value;
  }
  form.elements.feishu_webhook.placeholder=state.config.has_feishu_webhook?'已保存，留空保留':'填写机器人 Webhook';
  form.elements.feishu_secret.placeholder=state.config.has_feishu_secret?'已保存，留空保留':'可选：机器人签名密钥';
  form.elements.feishu_app_secret.placeholder=state.config.has_feishu_app_secret?'已保存，留空保留':'填写应用 App Secret';
  updateReception();updateFeishu();
  $('#custom-fields').innerHTML=Object.entries(fields).map(([key,label])=>`<label><input type="checkbox" name="custom_field" value="${key}" ${state.config.custom_fields.includes(key)?'checked':''}>${label}</label>`).join('');
}
async function loadKnowledge(){
  try{
    const kind=$('#knowledge-collection').value;
    const endpoint=kind?'knowledge/materials?kind='+encodeURIComponent(kind)+'&q=':'knowledge?q=';
    const data=await api(endpoint+encodeURIComponent($('#knowledge-search').value));
    $('#knowledge-count').textContent=`启用 ${state?.counts.knowledge??0} 条 · 当前最多显示 100 条`;
    const bundle=state?.knowledge_bundle;
    if(bundle?.batch)$('#knowledge-import-summary').textContent=`已接入 ${bundle.active_documents} 篇官方文档、${bundle.inserted} 个完整章节。${bundle.counts.knowledge_candidates} 条问答候选和 ${bundle.counts.policy_candidates} 条业务规则保留供审核。来源提交 ${bundle.official_commit.slice(0,12)}。`;
    $('#knowledge-list').innerHTML=data.items.length?data.items.map(k=>{
      let sources=[];try{sources=JSON.parse(k.sources);}catch{}
      const provenance=sources.map(s=>esc(s.file||s.document_id||'')+(s.row?' · 第 '+s.row+' 行':'')+(s.section?' · '+esc(s.section):'')+(s.commit?' · '+esc(s.commit.slice(0,12)):'' )).join('；');
      return `<article class="knowledge-item ${k.enabled||k.material?'':'disabled'}"><div><h3>${esc(k.title)}</h3>${k.product?`<span class="badge neutral">${esc(k.product)}</span>`:''}${k.material?`<span class="badge neutral">${esc(({active:'已纳入回复知识',reference:'保留供参考',discarded:'已排除',open:'待核实',partially_resolved:'部分解决',resolved_as_support_level:'已区分支持范围',candidate_lexical_hit:'关键词命中，尚未核实',candidate_no_official_hit:'未找到官方依据',candidate_unchecked:'尚未核实',checked_supported:'有核对依据，保留候选',checked_possible_conflict:'疑似冲突',official_source_as_candidate:'官方主题候选'})[k.material_status]||k.material_status)}</span>`:''}<details><summary>查看内容与依据</summary><p>${esc(k.content)}</p></details><small>${provenance}</small></div>${k.material?'':`<button class="button quiet" data-knowledge="${esc(k.id)}" data-enabled="${k.enabled?'0':'1'}">${k.enabled?'停用':'启用'}</button>`}</article>`;
    }).join(''):empty('没有找到资料','尝试产品全称、关键词，或切换资料类型。','book-open');hydrate();
  }catch(e){toast(e.message,true);}
}
$('#knowledge-collection').addEventListener('change',loadKnowledge);
async function refresh(){state=await api('state');render();}
async function perform(button, work){button.disabled=true;try{await work();await refresh();}catch(e){toast(e.message,true);}finally{if(button.isConnected){button.disabled=false;if(button.dataset.command&&state)render();}}}
document.addEventListener('click',e=>{
  const view=e.target.closest('[data-view]');if(view){showView(view.dataset.view);return;}
  const button=e.target.closest('button');if(!button)return;
  if(button.dataset.command)perform(button,async()=>{await api('runtime/'+button.dataset.command,{});});
  if(button.dataset.customer){selectedCustomer=button.dataset.customer;renderConversations();loadHistory(true);}
  if(button.dataset.takeover)perform(button,async()=>{await api('conversation',{id:selectedCustomer,action:button.dataset.takeover});historyVersion='';});
  if(button.dataset.outbox)perform(button,async()=>{const id=button.closest('[data-id]').dataset.id;await api('outbox',{id,action:button.dataset.outbox,...(edits.has(id)?{reply:edits.get(id)}:{})});edits.delete(id);toast(button.dataset.outbox==='approve'?'已加入发送队列，接待运行时由 RPA 发送。':'已保存处理结果');});
  if(button.hasAttribute('data-resolve'))perform(button,async()=>{const id=button.closest('[data-id]').dataset.id;const result=taskResults.get(id)||'';if(!result.trim())throw new Error('请先填写处理结果');const saved=await api('tasks/resolve',{id,result});taskResults.delete(id);toast(saved.state==='human'?'处理结果已保存。该客户处于“同事接管”，请先在客户会话中点击“恢复接待”。':'处理结果已保存，运行接待时继续处理。');});
  if(button.dataset.profile){const id=button.closest('[data-id]').dataset.id;if(button.dataset.profile==='edit')fillProfile(state.profiles.items.find(p=>p.id===id));else perform(button,async()=>{const result=await api('profiles/'+button.dataset.profile,{id});if(button.dataset.profile==='test')toast(result.ok?`${result.model} 连接正常 · ${result.seconds} 秒`:'接口已返回，但测试内容未符合预期',!result.ok);else toast('已切换当前模型');});}
  if(button.dataset.knowledge)perform(button,async()=>{await api('knowledge/toggle',{id:button.dataset.knowledge,enabled:button.dataset.enabled==='1'});await loadKnowledge();});
});
document.addEventListener('input',e=>{if(e.target.dataset.edit)edits.set(e.target.dataset.edit,e.target.value);if(e.target.dataset.result)taskResults.set(e.target.dataset.result,e.target.value);});
$('#profile-form').addEventListener('submit',e=>{e.preventDefault();perform($('button[type=submit]',e.target),async()=>{const data=Object.fromEntries(new FormData(e.target));const saved=await api('profiles/save',data);e.target.elements.id.value=saved.id;e.target.elements.api_key.value='';e.target.elements.api_key.required=false;toast('模型连接已保存');});});
$('#new-profile').addEventListener('click',()=>fillProfile());
$('#settings-form').addEventListener('submit',e=>{e.preventDefault();perform($('button[type=submit]',e.target),async()=>{const form=new FormData(e.target);const data=Object.fromEntries(form);data.custom_fields=form.getAll('custom_field');data.feishu_enabled=e.target.elements.feishu_enabled.checked;data.feishu_allow_private=e.target.elements.feishu_allow_private.checked;for(const key of ['feishu_allowed_users','feishu_allowed_chats'])data[key]=parseIDs(data[key]);delete data.custom_field;await api('settings',data);for(const key of ['feishu_webhook','feishu_secret','feishu_app_secret'])e.target.elements[key].value='';toast('接待设置已保存，下次连接或启动生效');});});
$('#import-project').addEventListener('click',e=>perform(e.currentTarget,async()=>{const data=await api('knowledge/import-project',{});toast(data.results.map(r=>r.error?`${r.file}：${r.error}`:`${r.file}：新增 ${r.inserted}，重复 ${r.duplicates}`).join('；')||'data/raw/ 中没有 CSV 文件');await loadKnowledge();}));
$('#csv-file').addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;try{if(file.size>4000000)throw new Error('请选择 4 MB 以内的文件');const bytes=await file.arrayBuffer();let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch{text=new TextDecoder('gb18030').decode(bytes);}const data=await api('knowledge/import',{filename:file.name,text});toast(`新增 ${data.inserted} 条，跳过 ${data.duplicates} 条重复记录。`);await refresh();await loadKnowledge();}catch(err){toast(err.message,true);}finally{e.target.value='';}});
let searchTimer;$('#knowledge-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadKnowledge,250);});
$('#add-knowledge').addEventListener('click',()=>$('#knowledge-dialog').showModal());
$('#close-dialog').addEventListener('click',()=>$('#knowledge-dialog').close());
$('#knowledge-form').addEventListener('submit',e=>{e.preventDefault();perform($('button[type=submit]',e.target),async()=>{await api('knowledge/save',Object.fromEntries(new FormData(e.target)));e.target.reset();$('#knowledge-dialog').close();await loadKnowledge();toast('知识已保存');});});
hydrate();
(async function poll(){try{await refresh();}catch{const el=$('#connection');el.textContent='连接中断';}setTimeout(poll,2000);})();

let deletionRequest=null;
document.addEventListener('click',e=>{
  const button=e.target.closest('[data-delete]');if(!button)return;
  if(state.runtime.running||state.feishu?.running){toast('请先停止接待并断开飞书连接，再清理数据。',true);return;}
  const action=button.dataset.delete;
  deletionRequest={action,id:action==='delete_all'?undefined:selectedCustomer};
  const name=state.conversations.find(c=>c.id===selectedCustomer)?.name||'';
  $('#delete-title').textContent=action==='clear'?'清空聊天':action==='delete'?'删除客户':'删除全部客户与聊天';
  $('#delete-description').textContent=action==='delete_all'?'删除全部本地客户和关联记录，保留知识与配置。':
    `将${action==='clear'?'清空':'删除'}「${name}」的聊天、草稿、待办和流程记忆。${action==='clear'?'保留客户。':'同时删除客户。'}模拟页记录会同步清理，京麦服务器记录不受影响。`;
  $('#delete-form').reset();$('#delete-dialog').showModal();
});
$('#close-delete').addEventListener('click',()=>$('#delete-dialog').close());
$('#delete-form').addEventListener('submit',e=>{
  e.preventDefault();const request={...deletionRequest,confirmation:e.target.elements.confirmation.value};
  perform($('button[type=submit]',e.target),async()=>{
    await api('data/delete',request);historyVersion='';historyGeneration++;edits.clear();taskResults.clear();
    $('#delete-dialog').close();toast('本地记录已清理');
  });
});
$('#export-data').addEventListener('click',e=>perform(e.currentTarget,async()=>{
  const response=await fetch('/api/manage/data/export',{method:'POST',headers:{'Content-Type':'application/json','X-CS-Duty':'1'},body:JSON.stringify({include_secrets:$('#export-secrets').checked})});
  if(!response.ok)throw new Error((await response.json()).error||'导出失败');
  const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');
  a.href=url;a.download='cs-duty-workspace-'+new Date().toISOString().replace(/[:.]/g,'-')+'.zip';
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);toast('迁移包已生成，请查看浏览器下载');
}));

function updateReception(){
  const form=$('#settings-form'),real=form.elements.transport.value==='jingmai',auto=form.elements.mode.value==='auto';
  $('#mock-address').hidden=real;$('#real-address').hidden=!real;
  $('#source-description').textContent=real?'打开京麦工作台，登录后读取正在咨询。':'使用本机模拟工作台接收测试咨询。';
  $('#mode-description').textContent=auto?'由 RPA 填入回复、点击发送，并核对发送结果。':'由 RPA 填入网页输入框，保留草稿，不点击发送。';
  $('#reception-summary').textContent=(real?'真实页面 · 京东京麦':'模拟页面')+' / '+(auto?'自动发送':'填写草稿');
}
$('#settings-form').addEventListener('change',updateReception);

function parseIDs(value){return [...new Set(String(value||'').split(/[\s,，]+/).filter(Boolean))];}
function updateFeishu(){
  const app=$('#settings-form').elements.feishu_mode.value==='app';
  $('#feishu-app-fields').hidden=!app;$('#feishu-webhook-fields').hidden=app;
}
function renderFeishu(){
  const info=state.feishu||{running:false,discovered:[]};
  $('#feishu-status').textContent=info.detail||'飞书协作未连接';
  $('[data-feishu="start"]').disabled=info.running;
  $('[data-feishu="stop"]').disabled=!info.running;
  $('[data-feishu="pair"]').disabled=info.state!=='connected';
  setHTML($('#feishu-discovered'),info.discovered.map((d,i)=>`<div class="feishu-identity"><b>已识别${d.chat_type==='p2p'?'私聊':'群聊'}中的员工</b><p>用户：<code>${esc(d.user_id)}</code></p><p>会话：<code>${esc(d.chat_id)}</code></p><button type="button" class="button secondary" data-feishu-adopt="${i}">填入白名单和通知会话</button></div>`).join(''));
  if(info.discovered.length)$('#feishu-pairing').hidden=true;
}
$('#settings-form').addEventListener('change',updateFeishu);
document.addEventListener('click',e=>{
  const button=e.target.closest('button');if(!button)return;
  if(button.dataset.feishu)perform(button,async()=>{
    const action=button.dataset.feishu,result=await api('feishu/'+action,action==='test'?{confirm_send:true}:{});
    if(action==='pair'){
      $('#feishu-pairing').textContent='请在目标飞书会话中发给机器人：'+result.command+'（群内需 @机器人，5 分钟内有效）。识别后核对 ID，再保存白名单。';
      $('#feishu-pairing').hidden=false;
    }else if(action==='test')toast(result.detail);
  }).finally(()=>{if(state)renderFeishu();});
  if(button.hasAttribute('data-feishu-adopt')){
    const d=state.feishu.discovered[Number(button.dataset.feishuAdopt)];if(!d)return;
    const form=$('#settings-form');
    form.elements.feishu_allowed_users.value=[...new Set([...parseIDs(form.elements.feishu_allowed_users.value),d.user_id])].join('\n');
    form.elements.feishu_allowed_chats.value=[...new Set([...parseIDs(form.elements.feishu_allowed_chats.value),d.chat_id])].join('\n');
    form.elements.feishu_chat_id.value=d.chat_id;
    if(d.chat_type==='p2p')form.elements.feishu_allow_private.checked=true;
    toast('已填入。请断开连接，启用待办协作并保存设置，然后重新连接。');
  }
});
