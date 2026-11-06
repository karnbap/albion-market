'use strict';
const $=id=>document.getElementById(id);
const n=(v,d=0)=>v===null||v===undefined?'—':Number(v).toLocaleString('ko-KR',{maximumFractionDigits:d});
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const storage={get(key,fallback){try{return JSON.parse(localStorage.getItem('albion.'+key))??fallback}catch{return fallback}},set(key,value){try{localStorage.setItem('albion.'+key,JSON.stringify(value))}catch{}}};
const savedView=storage.get('view',{});
let state,token,selectedItem=savedView.item||'',selectedCity=savedView.city||'1002',ready=false,loading=false,refreshQueued=false,viewVersion=0;
let formVersion=0,saving=false,saveQueued=false,saveTimer,scrollTimer,restorePending=true;
let draft=storage.get('settingsDraft',null);
const percentKeys=['sales_tax','setup_fee','minimum_margin','near_high_fraction','depth_price_band','cycle_share'];
const numberKeys=['trade_budget','inventory_heavy_days','weekday_cycles','fresh_minutes','transport_per_unit','small_quantity_max','large_quantity_min','wall_quantity_min','wall_multiple'];
const boolKeys=['fees_confirmed','transport_confirmed'];
const fragmentCache=new Map(),innerScroll=storage.get('tableScroll',{});
const formatters=new Map();
const date=v=>{if(!v)return '미조회';const zone=state?.timezone||'Europe/Paris';if(!formatters.has(zone))formatters.set(zone,new Intl.DateTimeFormat('ko-KR',{timeZone:zone,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZoneName:'shortOffset'}));return formatters.get(zone).format(new Date(v))};
const ago=v=>{const minutes=Math.max(0,Math.round((Date.now()-Date.parse(v))/60000));return minutes<1?'방금':minutes<60?`${minutes}분 전`:minutes<2880?`${Math.round(minutes/60)}시간 전`:date(v)};
const riskText={ok:'무난',caution:'조금 위험',danger:'위험',unknown:'판매량 미확인'};
const guideFor=(item,city)=>state.volume_guides?.[item+'/'+city];
const guideLine=g=>!g||g.per_day==null?'판매량 이력 부족 · 몇 개까지 괜찮은지 모름':g.per_day<1?'최근 하루 1개도 안 팔림 · 되팔 목적이면 위험':`하루 ${n(g.per_day)}개 팔림 · 내 몫 ${n(g.share*100)}% 기준 ${n(g.caution_above)}개 넘게 사면 조금 위험 · ${n(g.danger_above)}개 넘으면 위험`;
const shortGuide=g=>!g||g.per_day==null?'파는 곳 판매량 미확인':`파는 곳 하루 ${n(g.per_day)}개 팔림 · 내 몫 ${n(g.share*100)}% ${n(g.caution_above)}개까지 무난`;
const WEEK=['월','화','수','목','금','토','일'];
const pct=v=>(v>0?'+':'')+n(v*100,0)+'%';
const timingText=t=>{if(!t)return '';const wd=WEEK[t.weekday],parts=[];
 if(t.sales_weekday!=null)parts.push(`${wd}요일 판매량 평소의 ${n(t.sales_weekday,2)}배`);
 if(t.sales_hour!=null)parts.push(`${t.hour}시대 판매량 평소의 ${n(t.sales_hour,2)}배`);
 if(t.bid_vs_usual!=null)parts.push(`매수가 평소 ${wd}요일보다 ${pct(t.bid_vs_usual)}`);
 if(t.ask_vs_usual!=null)parts.push(`매도가 평소 ${wd}요일보다 ${pct(t.ask_vs_usual)}`);
 return parts.length?`파는 곳 지금(${wd} ${t.hour}시): ${parts.join(' · ')}`:`파는 곳 ${wd}요일·${t.hour}시대 기록이 아직 부족`};
const outlookText=o=>`파는 곳 앞으로 6시간 예상 판매 ${n(o.next6)}개(평소 ${n(o.usual6)}개) · 24시간 ${n(o.next24)}개${o.low_window?' · 수요 적은 시간대':''}`;
const hourRanges=hours=>{const out=[];for(const h of hours){const last=out[out.length-1];if(last&&(last[1]+1)%24===h)last[1]=h;else out.push([h,h])}return out.map(([a,b])=>a===b?`${String(a).padStart(2,'0')}시`:`${String(a).padStart(2,'0')}~${String(b).padStart(2,'0')}시`).join(', ')};
const settingsWarnings=()=>[!state.settings.fees_confirmed&&'수수료 설정 미확인',!state.settings.transport_confirmed&&'운송비 설정 미확인'].filter(Boolean);
const empty=text=>`<div class="empty">${esc(text)}</div>`;
const editing=()=>!!document.activeElement?.closest('#my-order-form,#purchase-form,#bank-form,.bank-city-form,.buy-target-form,.sell-target-form')&&document.activeElement.matches('select,input');
function table(headers,rows,key){return `<div class="table-wrap" data-scroll-key="${esc(key)}" tabindex="0" aria-label="${esc(key)} 표 스크롤"><table><thead><tr>${headers.map(h=>`<th>${h?.html??esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
function rememberView(){storage.set('view',{item:selectedItem,city:selectedCity,period:$('period').value,bookFilter:$('book-filter').value,scrollY:window.scrollY})}
function updateHtml(id,html){
 if(fragmentCache.get(id)===html)return;
 const root=$(id);
 for(const wrap of root.querySelectorAll('[data-scroll-key]'))innerScroll[wrap.dataset.scrollKey]={top:wrap.scrollTop,left:wrap.scrollLeft};
 root.innerHTML=html;fragmentCache.set(id,html);
 for(const wrap of root.querySelectorAll('[data-scroll-key]')){
  const key=wrap.dataset.scrollKey,old=innerScroll[key];if(old){wrap.scrollTop=old.top;wrap.scrollLeft=old.left}
  wrap.addEventListener('scroll',()=>{innerScroll[key]={top:wrap.scrollTop,left:wrap.scrollLeft};storage.set('tableScroll',innerScroll)},{passive:true});
 }
}
function preserveViewport(work){
 const oldY=window.scrollY;
 const anchor=[...document.querySelectorAll('main > section:not([hidden]), .tabs, .tab-panel:not([hidden]) > section, .tab-panel:not([hidden]) > .toolbar')].find(el=>{const r=el.getBoundingClientRect();return r.top<=64&&r.bottom>64});
 const oldTop=anchor?.getBoundingClientRect().top;
 work();
 if(!restorePending){const delta=anchor?anchor.getBoundingClientRect().top-oldTop:0;window.scrollTo({top:Math.max(0,oldY+delta),left:window.scrollX,behavior:'instant'})}
}
async function refresh(){
 if(loading){refreshQueued=true;return}loading=true;
 const revision=viewVersion;
 try{
  const r=await fetch(`/api/state?item=${encodeURIComponent(selectedItem)}&city=${encodeURIComponent(selectedCity)}`);if(!r.ok)throw Error(r.status);
  const next=await r.json();token=next.token;
  // While a hand-entry field is in use, neither reload nor redraw: either can close an open
  // dropdown or reset a half-filled form. Leaving the field refreshes again.
  if(ready&&editing())return;
  if(ready&&state?.ui_revision&&next.ui_revision&&state.ui_revision!==next.ui_revision){rememberView();if(draft)storage.set('settingsDraft',readDraft());window.location.reload();return}
  const legacy=next.build!=='20261105.2';
  $('upgrade').hidden=!legacy;
  for(const input of $('settings').querySelectorAll('input,button'))input.disabled=legacy;
  if(legacy)$('saved').textContent='업데이트 적용 후 자동 저장을 사용할 수 있습니다.';
  if(revision!==viewVersion){refreshQueued=true;return}state=next;
  if(!ready){
   $('item').innerHTML=Object.entries(state.items).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');
   $('city').innerHTML=Object.entries(state.cities).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');
   selectedItem=state.selected_item;selectedCity=state.selected_city;$('item').value=selectedItem;$('city').value=selectedCity;
   $('period').value=['0','1','2'].includes(savedView.period)?savedView.period:'0';
   $('book-filter').value=['all','small','large','meaningful'].includes(savedView.bookFilter)?savedView.bookFilter:'all';
   fillSettings();numberHints();ready=true;
  }
  preserveViewport(()=>{
   const packetAge=state.last_packet?.received?Math.max(0,(Date.now()-Date.parse(state.last_packet.received))/60000):null;
   const watch=state.game_watch,capture=state.capture?.running;
   // Game closed with the watcher alive is the normal idle state, not a warning.
   let warning=null;
   if(capture===false&&!watch)warning=['시장 수집기가 실행 중이 아닙니다.','이 Windows 컴퓨터에서 알비온 게임을 실행하면 수집기가 자동으로 켜집니다. 휴대폰·아이패드 자료는 자동 수집되지 않습니다.'];
   else if(capture===false&&!watch.active)warning=['자동 감시가 꺼져 있습니다.','게임을 켤 때 자동으로 수집하려면 이 폴더의 enable-game-watch.ps1을 한 번 실행해 주세요. 지금은 지난 자료만 표시합니다. 휴대폰·아이패드 자료는 자동 수집되지 않습니다.'];
   else if(capture===false&&watch.game_running)warning=['게임은 켜져 있지만 지금은 수집하지 않습니다.',`자동 감시 상태: ${watch.status||'확인 중'}. 잠시 후 자동으로 다시 시도합니다. 계속되면 data 폴더의 game-watch.log를 확인해 주세요.`];
   $('capture-warning').hidden=!warning;if(warning){$('capture-title').textContent=warning[0];$('capture-detail').textContent=warning[1]}
   $('connection').textContent=capture===false?(watch?.active&&!watch.game_running?'자동 감시 켜짐 · 게임을 켜면 수집 시작':'수집기 꺼짐 · 지난 자료만 표시'):!state.packets?'저장 프로그램 연결됨 · 첫 자료 대기':packetAge<=state.settings.fresh_minutes?'저장 프로그램 연결됨 · 최근 자료 수신':`저장 프로그램 연결됨 · ${n(packetAge)}분간 새 자료 없음`;
   $('last').textContent=`마지막 자료 ${date(state.last_packet?.received)} · ${n(state.packets)}회 수신`;
   $('auto-update').textContent=state.auto_update?.enabled?state.auto_update.status+(state.auto_update.note?' · '+state.auto_update.note:''):'자동 반영 준비 · 분석 프로그램을 한 번 다시 실행해 주세요.';
   $('welcome').hidden=state.packets>0;$('zone').textContent=state.timezone;render();
  });
  if(restorePending){restorePending=false;window.scrollTo({top:Math.max(0,Number(savedView.scrollY)||0),behavior:'instant'});if(draft)scheduleSave()}
 }catch(e){$('connection').textContent='저장 프로그램 연결 끊김';$('last').textContent='마지막 화면과 입력을 유지합니다. 재연결을 기다리는 중입니다.'}
 finally{loading=false;if(refreshQueued){refreshQueued=false;queueMicrotask(refresh)}}
}
function quote(book){return (book.fresh_levels?.length?book.fresh_levels:book.levels)[0]}
function quoteText(book,side){const r=quote(book),label=`<span class="quote-label ${side}">${book.single_only?(side==='buy'?'매수 관측 · 최고가 미확인':'매도 관측 · 최저가 미확인'):(side==='buy'?'최고 매수':'최저 매도')}</span>`;if(!r)return label+'<small class="quote-time">미조회</small>';const seen=r.seen||book.seen;return `${label}<span class="quote-value"><b>${n(r.price)}</b><span class="quote-quantity">${n(r.amount)}개</span></span><small class="quote-time ${r.stale||book.stale?'old':''}" title="${esc(date(seen))}">${r.stale||book.stale?'지난 관측 · ':''}${ago(seen)}</small>`}
// In-game colours: tiers I-VIII grey, tan, green, blue, red, orange, yellow, white; enchantments .1-.4 green, blue, purple, yellow.
const tierSpan=(t,e)=>`<span class="tier-tag t${t}">T${t}</span>${e!=null&&e!==''?`<span class="ench-tag e${e}">.${e}</span>`:''}`;
const tierLabel=text=>esc(text).replace(/\bT([1-8])(?:\.([0-4]))?\b/g,(m,t,e)=>tierSpan(t,e));
const qualityNames=['','보통','좋음','뛰어남','훌륭함','걸작'];
const qualityLabel=q=>`<span class="q-tag q${q}">품질 ${q} ${qualityNames[q]||''}</span>`;
const itemName=i=>(state.items[i]||i).replace(' · 테스트·보류','');
const itemHead=i=>({html:`${esc(itemName(i))}${(state.trial_items||[]).includes(i)?'<small class="trial-tag">테스트·보류</small>':''}`});
function extremeMark(item,city,side){const e=state.city_extremes?.[item]?.[side];return e?.observations?.some(r=>r.city===city)?`<span class="extreme-badge ${side}" title="${side==='buy'?'도시 중 최고 매수':'도시 중 최저 매도'}${e.fresh?'':' · 지난 자료'}">${side==='buy'?'최고가 도시':'최저가 도시'}${e.fresh?'':' · 지난'}</span>`:''}
function personalMark(item,city){const sale=state.personal_sales?.by_market?.[item+'/'+city];return sale?`<span class="personal-mark">내 판매 메일 · 누적 최고 ${n(sale.highest_price)} / ${n(sale.highest_price_amount)}개<small>체결 시각 미확인 · 최근 메일 ${date(sale.first_read)}</small></span>`:''}
function renderPersonal(){
 const sales=state.personal_sales;
 if(!sales){updateHtml('personal-sales',empty('거래 메일 기능을 적용하려면 분석 프로그램을 다시 실행해 주세요.'));return}
 $('personal-context').textContent=`전체 ${n(sales.mail_count)}개 메일`;
 const notices=(sales.conflicts?`<p class="warn">내용이 달라진 메일 ${n(sales.conflicts)}건은 합계에서 제외했습니다.</p>`:'')+(sales.unknown_identity?'<p class="help">캐릭터 식별 정보가 없는 메일이 있습니다. 여러 캐릭터 사용 시 중복 구분에 한계가 있습니다.</p>':'');
 if(!sales.mail_count){updateHtml('personal-sales',empty('저장된 판매 완료 메일이 아직 없습니다. 아래 메일 수집 진단에서 원인을 확인해 주세요.')+`<p class="help">${esc(sales.note)}</p>`+notices);return}
 const markets=Object.entries(sales.by_market).sort((a,b)=>b[1].mail_count-a[1].mail_count);
 const summary=table(['품목 · 도시','메일','판매 수량','최고 단가 (그 단가 수량)','최근 메일'],markets.map(([key,m])=>{const [item,city]=key.split('/');return `<tr><td>${tierLabel(state.items[item]||item)} · ${esc(state.cities[city]||city)}</td><td>${n(m.mail_count)}</td><td>${n(m.total_amount)}개</td><td>${n(m.highest_price)} (${n(m.highest_price_amount)}개)</td><td>${date(m.first_read)}</td></tr>`}),'personal-markets');
 const recent=table(['처음 확인한 때','메일 받은 때','품목 · 도시','단가','수량'],(sales.recent||[]).map(r=>`<tr><td>${date(r.first_seen)}</td><td>${r.mailed?date(r.mailed):'—'}</td><td>${tierLabel(state.items[r.item]||r.item)} · ${esc(state.cities[r.city]||r.city)}</td><td>${n(r.price)}</td><td>${n(r.amount)}개</td></tr>`),'personal-recent');
 updateHtml('personal-sales',`<h3 class="sub-head">시장별 합계</h3>${summary}<h3 class="sub-head">최근 메일 ${n((sales.recent||[]).length)}개</h3>${recent}<p class="help">${esc(sales.note)}</p>`+notices);
}
function renderPersonalMails(){
 const m=state.personal_mails;
 if(!m){updateHtml('personal-mails',empty('구매·만료 메일을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const kind={buy:'구매 주문 완료',buy_expired:'구매 주문 만료',sell_expired:'판매 주문 만료'};
 $('personal-mails-context').textContent=Object.entries(kind).map(([k,v])=>`${v} ${n(m.counts[k]||0)}개`).join(' · ');
 const traded=r=>r.kind==='buy'?`${n(r.amount)}개 구매`:`${n(r.amount)} / ${n(r.order_amount)}개 ${r.kind==='buy_expired'?'구매':'판매'}`;
 const silver=r=>`${n(r.silver)} ${r.kind==='buy'?'지출':r.kind==='buy_expired'?'환불':'수입'}`;
 const rows=m.recent.map(r=>`<tr><td>${date(r.mailed||r.first_seen)}</td><td>${kind[r.kind]||esc(r.kind)}</td><td>${tierLabel(state.items[r.item]||r.item)} · ${esc(state.cities[r.city]||r.city)}</td><td>${n(r.price)}</td><td>${traded(r)}</td><td>${silver(r)}</td></tr>`);
 updateHtml('personal-mails',(rows.length?table(['메일 받은 때','종류','품목 · 도시','단가','수량','실버'],rows,'personal-mails'):empty('저장된 구매 주문·만료 메일이 아직 없습니다. 게임 우편함에서 구매 주문 완료 메일을 열면 여기에 쌓입니다.'))+`<p class="help">${esc(m.note)}</p>`+(m.conflicts?`<p class="warn">내용이 달라진 메일 ${n(m.conflicts)}건은 제외했습니다.</p>`:''));
}
function renderMailDiagnosis(){
 const d=state.mail_diagnostics;
 if(!d){updateHtml('mail-diagnosis',empty('진단 기능을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const topics=Object.fromEntries(d.private_topics.map(t=>[t.topic,t])),mail=topics.marketnotifications,orders=topics['marketorders.ingest'],mine=topics['marketorders.mine'];
 const c=d.counts,outcome={saved:'저장',duplicate:'이미 저장한 메일',conflict:'내용 충돌',ignored:'무시',rejected:'형식 오류'};
 const dbg=state.client_debug,fork=dbg?.fork;
 let verdict;
 if(!d.private_topics.length)verdict=['warn','수집기의 개인 자료 통로(메일 전용)로 들어온 자료가 한 번도 없습니다. 수집기가 이 프로그램이 아닌 다른 방법으로 켜졌거나 오래된 실행 설정일 수 있습니다. 게임을 끄고 10초 뒤 다시 켜서 자동 수집으로 시작해 주세요.'];
 else if(!mail&&fork)verdict=['warn','전용 수집기가 켜져 있지만 메일은 아직 한 번도 오지 않았습니다. 게임 우편함을 열고 판매 완료나 구매 주문 완료 메일을 하나 열어 주세요. 그래도 안 오면 아래 수집기 기록을 캡처해 보내 주세요.'];
 else if(!mail)verdict=['warn',`개인 자료 통로는 연결돼 있지만(시장 자료 ${n(orders?.count||0)}회) 판매 메일은 한 번도 오지 않았습니다. 수집기는 켜진 뒤 받은 우편함 목록으로만 메일을 읽습니다. 지역을 한 번 이동한 다음 우편함을 닫았다 다시 열고 판매 완료 메일을 열어 주세요. 아래 수집기 기록에 ‘Mail Info is not valid’가 지역을 옮긴 뒤에도 계속 보이면, 수집기가 우편함 목록 자체를 읽지 못하는 문제입니다. 맨 아래 원본 기록을 캡처해 보내 주세요.`];
 else if(!c.saved)verdict=['warn','판매 메일은 들어왔지만 저장된 것이 없습니다. 아래 기록의 무시 이유를 확인해 주세요.'];
 else verdict=['ok',`판매 메일이 정상적으로 들어오고 있습니다 · 저장 ${n(c.saved)}건`];
 const events=d.events.length?table(['받은 때','종류','품목','도시 코드','결과','이유'],d.events.map(e=>`<tr><td>${date(e.received)}</td><td>${esc(e.kind||'—')}</td><td>${esc(state.items[e.item]||e.item||'—')}</td><td>${esc(e.location||'—')}</td><td>${esc(outcome[e.outcome]||e.outcome)}</td><td>${esc(e.detail||'')}</td></tr>`),'mail-events'):'';
 const opName={94:'내 주문 목록',174:'우편함 목록',175:'메일 개수',176:'메일 열기'};
 const raw=!dbg?'':!dbg.enabled?'<p class="help">수집기가 디버그 기록 없이 켜져 있어 원본 기록이 없습니다. 최신 버전을 받은 뒤 게임을 끄고 10초 뒤 다시 켜 주세요.</p>':dbg.lines.length?`<p class="help">메일이나 내 주문이 안 들어올 때 이 부분을 캡처해 보내 주시면 원인을 찾을 수 있습니다.</p><pre class="client-log">${dbg.lines.map(l=>`${date(l.logged)} ${l.op?'['+(opName[l.op]||l.op)+'] ':''}${esc(l.text.length>600?l.text.slice(0,600)+'…':l.text)}`).join('\n')}</pre>`:'<p class="help">디버그 기록은 켜져 있지만 우편함·메일·내 주문 기록이 아직 없습니다. 게임에서 우편함과 내 주문 탭을 열어 주세요.</p>';
 const log=d.client_log.lines.length?`<pre class="client-log">${d.client_log.lines.map(esc).join('\n')}</pre>`:`<p class="help">${d.client_log.file?'수집기 기록에 메일 관련 줄이 없습니다.':'수집기 기록 파일을 찾지 못했습니다.'}</p>`;
 updateHtml('mail-diagnosis',`<p class="${verdict[0]==='ok'?'positive':'warn'} diagnosis">${esc(verdict[1])}</p><div class="diag-grid"><div><small>개인 통로 · 메일</small><b>${mail?n(mail.count)+'회':'없음'}</b><span>${mail?'마지막 '+date(mail.last):''}</span></div><div><small>개인 통로 · 시장 자료</small><b>${orders?n(orders.count)+'회':'없음'}</b><span>${orders?'마지막 '+date(orders.last):''}</span></div><div><small>개인 통로 · 내 주문 목록</small><b>${mine?n(mine.count)+'회':'없음'}</b><span>${mine?'마지막 '+date(mine.last):fork?'':'전용 수집기만 보냄'}</span></div><div><small>메일 처리 결과</small><b>${Object.entries(c).map(([k,v])=>`${outcome[k]||k} ${n(v)}`).join(' · ')||'없음'}</b></div></div>${events}<h3 class="sub-head">수집기 기록 (메일 관련 최근 줄)</h3>${log}<h3 class="sub-head">수집기 원본 기록 (우편함·메일·내 주문)</h3>${raw}<p class="help">${fork?'전용 수집기를 쓰는 중입니다. 판매 완료·판매 만료·구매 주문 완료·구매 주문 만료 메일과 내 주문 목록을 이 컴퓨터의 분석 프로그램으로만 보냅니다.':'공식 수집기를 쓰는 중입니다. 공식 수집기는 판매 주문 완료 메일만 보내고, 구매 주문 완료·만료 메일은 보내지 않습니다.'}</p>`);
}
function renderPickHistory(){
 const rows=state.pick_history;
 if(!rows){updateHtml('pick-history',empty('기록 기능을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const kind={instant:'바로 사고 바로 팔기',instant_list:'바로 사고 매도 주문',best:'양쪽 주문',buyout:'싹쓸이'};
 updateHtml('pick-history',rows.length?table(['시각','순위','거래','수량','투자','예상 이익','방식'],rows.flatMap(entry=>entry.picks.map((p,i)=>`<tr class="${i?'':'log-first'}"><td>${i?'':`${date(entry.created)}<small>${ago(entry.created)}</small>`}</td><td>${i+1}</td><td>${tierLabel(state.items[p.item]||p.item)} · ${esc(state.cities[p.buy_city]||p.buy_city)}${p.kind==='buyout'?' 싹쓸이':' → '+esc(state.cities[p.sell_city]||p.sell_city)}</td><td>${n(p.units)}개</td><td>${n(p.cost)}</td><td class="positive">+${n(p.profit)}</td><td>${esc(kind[p.kind]||p.kind)}</td></tr>`)),'pick-history'):empty('아직 저장된 추천이 없습니다. 게임에서 시장을 열면 추천이 나올 때마다 여기에 쌓입니다.'));
}
function renderBookPatterns(){
 const bp=state.book_patterns,t=state.timing;
 if(!bp){updateHtml('book-weekdays','');updateHtml('book-hours','');return}
 $('book-pattern-context').textContent=`${state.cities[selectedCity]} · ${state.items[selectedItem]} · 지금 ${WEEK[t.weekday]}요일 ${t.hour}시 (${state.timezone})`;
 const head=['','평균 최저 매도가','매도 물량','평균 최고 매수가','매수 물량','관측'];
 const rows=(scale,count,label,current)=>Array.from({length:count},(_,k)=>{const s=bp.sell[scale].find(r=>r.key===k),b=bp.buy[scale].find(r=>r.key===k);return s||b||k===current?`<tr class="${k===current?'now-row':''}"><td>${label(k)}</td><td>${s?n(s.best):'—'}</td><td>${s?n(s.amount):'—'}</td><td>${b?n(b.best):'—'}</td><td>${b?n(b.amount):'—'}</td><td>${n((s?.samples||0)+(b?.samples||0))}</td></tr>`:''});
 const anything=bp.sell.weekdays.length||bp.buy.weekdays.length;
 updateHtml('book-weekdays',anything?table(['요일',...head.slice(1)],rows('weekdays',7,k=>WEEK[k],t.weekday),'book-weekdays'):empty('아직 쌓인 호가 기록이 없습니다. 이 시장을 여러 요일·시간에 열면 채워집니다.'));
 updateHtml('book-hours',anything?table(['시간',...head.slice(1)],rows('hourly',24,k=>String(k).padStart(2,'0')+'시',t.hour),'book-hours'):'');
}
function renderPortfolio(){
 const pf=state.portfolio;
 if(!pf){updateHtml('portfolio',empty('매매 계획을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const city=c=>esc(state.cities[c]||c),share=v=>v==null?'—':n(v*100,0)+'%',item=i=>esc(state.items[i]||i),how={order:'매수 주문',instant:'바로 사기'};
 const hours=h=>!h?'—':!h.enough?`<small>자료 부족 · 같은 시각 ${n(h.days,0)}일치 (3일치부터)</small>`:`${h.clear?'':'뚜렷한 차이 없음 · '}${h.hours.map(x=>`${String(x.hour).padStart(2,'0')}시 <small>${pct(x.vs_average)}</small>`).join(' · ')}`;
 const short=(silver,need)=>silver<need-0.5?`<small class="warn">돈이 모자라 ${n(silver)}만</small>`:'';
 const tiles=`<div class="diag-grid capital-grid"><div><small>지금 총 투자금</small><b>${n(pf.total)}</b><span>보유 원가 + 매수 주문</span></div><div><small>평일 순환에 쓸 돈</small><b>${n(pf.weekday_silver)} · ${share(pf.weekday_share)}</b><span>품목마다 한 번 돌릴 만큼 · 하루 ${n(pf.turns)}번 돌림</span></div><div><small>주말 핫타임에 쓸 돈</small><b>${n(pf.weekend_silver)} · ${share(pf.weekend_share)}</b><span>금 저녁~일 싹쓸이·매도 · 필요 ${n(pf.weekend_need)}</span></div><div><small>내 몫</small><b>시장 하루 판매량의 ${n(pf.cycle_share*100,0)}%</b><span>설정 탭에서 바꿈 · ${pf.cities.map(city).join(', ')}만 계산</span></div></div>`;
 const stock=w=>w.stock_turns>=1?`<span class="warn">가진 것 먼저 팔기</span><small>${n(w.stock_turns,1)}번 돌릴 만큼 있음</small>`:`<span class="positive">한 번에 ${n(w.to_buy)}개 더 사기</span><small>가진 것 ${n(w.turn_units-w.to_buy)}개 + 새로 ${n(w.to_buy)}개</small>`;
 const weekday=pf.rows.map(r=>{const w=r.weekday,o=r.weekday_other;if(!w)return `<tr><td>${item(r.item)}</td><td style="text-align:left" colspan="6"><small>지금 호가로는 이익 나는 순환이 없습니다</small></td></tr>`;
  return `<tr><td>${item(r.item)}<small>${how[w.method]}</small></td><td>${city(w.buy_city)}<small>${how[w.method]} ${n(w.buy)} · ${esc(w.buy_basis)}</small></td><td>${w.legs.map(l=>`${city(l.sell_city)} ${n(Math.ceil(l.units/pf.turns))}개<small>매도 주문 ${n(l.sell)} · ${pct(l.margin/l.cost)}</small>`).join('')}</td><td><b>${n(w.turn_units)}개</b><small>${n(w.turn_silver)} · 투자금의 ${share(w.share)}</small>${short(w.silver,w.turn_silver)}</td><td><span class="positive">${pct(w.roi)}</span> <small>한 번 돌릴 때</small><small>하루 약 +${n(w.profit)} · 월~목 +${n(w.profit_week)}</small>${o?`<small>${how[o.method]}(${city(o.buy_city)}에서)로는 ${pct(o.roi)} · 하루 +${n(o.profit)}</small>`:''}</td><td>${n(r.held)}개<br>${stock(w)}</td><td>${r.buy_hours&&r.sell_hours&&!r.buy_hours.enough&&!r.sell_hours.enough?hours(r.sell_hours):`<small>사기</small> ${hours(r.buy_hours)}<br><small>팔기</small> ${hours(r.sell_hours)}`}</td></tr>`});
 const weekend=pf.rows.map(r=>{const k=r.weekend;if(!k)return `<tr><td>${item(r.item)}</td><td style="text-align:left" colspan="5"><small>지금 호가로는 주말에 이익 나는 싹쓸이가 없습니다</small></td></tr>`;
  return `<tr><td>${item(r.item)}</td><td>${city(k.buy_city)}<small>바로 사기 ${n(k.buy)} (평일 최저 매도가)</small></td><td>${k.legs.map(l=>`${city(l.sell_city)} ${n(Math.floor(l.units))}개<small>매도 주문 ${n(l.sell)} (금~일) · ${pct(l.margin/l.cost)} · ${esc(l.sell_basis)}</small>`).join('')}</td><td><b>${n(k.units)}개</b><small>${n(k.need)} · 투자금의 ${share(k.share)}</small>${short(k.silver,k.need)}</td><td><span class="positive">${pct(k.roi)}</span><small>약 +${n(k.profit)}${r.swing==null?'':` · 가격 흔들림 ±${n(r.swing*100,1)}%`}</small></td><td>${hours(r.weekend_hours)}</td></tr>`});
 updateHtml('portfolio',tiles+trialNote('계획')
  +`<h3 class="sub-head">평일 순환 (월~목 · 하루 ${n(pf.turns)}번)</h3>`+table(['품목','살 곳','팔 곳 (한 번에 나눠 팔 양)','한 번에','이익','지금 가진 것','좋은 시간'],weekday,'plan-weekday')
  +`<h3 class="sub-head">주말 핫타임 (금 저녁~일)</h3>`+table(['품목','살 곳','팔 곳 (나눠 팔 양)','합계 (어림)','예상 수익률','팔기 좋은 시간'],weekend,'plan-weekend')
  +`<p class="help"><b>평일 순환</b>은 적게 사서 그날 팔고 다시 사는 것을 하루 ${n(pf.turns)}번 하는 계획입니다. 사는 방법은 두 가지를 모두 계산해 하루 이익이 큰 쪽을 보여 줍니다: 살 곳 최고 매수가에 <b>매수 주문</b>(수수료 포함)을 걸거나, 다른 도시의 최저 매도가로 <b>바로 사기</b>. 파는 것은 최저 매도가의 매도 주문이고, 한 도시에만 팔면 매도량이 쌓이므로 이익이 나는 여러 도시에 나눠 팝니다: 이익이 큰 도시부터 그 도시가 받아 줄 양(아래 내 몫)까지 채웁니다. 다른 도시에서 팔면 설정의 개당 운송비를 뺍니다. 도시마다 받아 줄 양 = 그 도시 하루 판매량 가운데 매도 주문을 채워 주는 몫(즉시 구매 비율 · 아직 측정 전이면 절반)의 ${n(pf.cycle_share*100,0)}%, 매수 주문으로 살 양은 살 곳 판매량의 나머지 몫의 ${n(pf.cycle_share*100,0)}%까지이고, 한 번에 = 그 양 ÷ ${n(pf.turns)}번입니다. 이미 가진 것이 있으면 먼저 팔고, 모자란 만큼만 삽니다.</p>`
  +`<p class="help"><b>주말 핫타임</b>은 금요일 저녁부터 일요일까지(${n(pf.hot_days,2)}일) 싼 매물을 바로 사서 비싸게 매도 주문으로 파는 계획입니다. 살 값은 평일 최저 매도가, 팔 값은 금~일 최저 매도가로 잡았습니다. 실제로 살 매물은 <b>싹쓸이</b> 탭에서 지금 호가로 확인하세요. 팔 양 = 금~일 즉시 구매 수요의 ${n(pf.cycle_share*100,0)}% × ${n(pf.hot_days,2)}일.</p>`
  +`<p class="help">평일 돈과 주말 돈은 같은 투자금을 다시 쓰는 것이라 따로 비율을 냅니다. 돈이 모자라면 돈 대비 이익이 큰 품목부터 채웁니다. 가격은 그 요일들의 호가 기록이 있으면 그것을, 없으면 다른 요일 호가나 지난 호가를 요일별 체결가 흐름으로 보정한 값을 씁니다(칸마다 근거 표시). 시간대는 같은 시각을 3일 이상 본 1시간 이력이 있어야 나오며, 시장 상세에서 24시간 이력을 여러 날 열면 쌓입니다(${esc(state.timezone)} 기준).</p>`);
}
function renderStudies(){
 const rows=state.studies;
 if(!rows){updateHtml('studies',empty('전략 연구를 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const money=v=>v==null?'—':n(v,0);
 updateHtml('studies',`<p class="help">아래는 시장(도시)마다 따로 본 기초 자료입니다. 다른 도시에 나눠 파는 계획은 위 매매 계획에 있습니다.</p>`+table(['품목 · 도시','평균 체결가 평일 → 주말','하루 판매량 평일 / 주말','즉시 구매 비율','체결 관찰 (28일)','최저가 깎임','같은 도시 안에서 평일 매수 주문 → 주말 매도 주문 (수수료 후)'],rows.map(r=>{const a=r.plan_a;return `<tr><td>${tierLabel(state.items[r.item])}<br><small>${esc(state.cities[r.city])}</small></td><td>${money(r.weekday?.price)} → ${money(r.weekend?.price)}${r.price_gap==null?'':`<small class="${r.price_gap>0?'positive':'warn'}">${pct(r.price_gap)}</small>`}</td><td>${r.weekday?n(r.weekday.per_day)+'개':'—'} / ${r.weekend?n(r.weekend.per_day)+'개':'—'}</td><td>${r.instant_buy_share==null?'—':n(r.instant_buy_share*100)+'%'}<small>근거 ${n(r.share_samples)}구간</small></td><td><small>매도 주문 ${n(r.fills_sell)}개 팔림<br>매수 주문 ${n(r.fills_buy)}개 채워짐</small></td><td>${r.undercut_hours==null?'—':'평균 '+n(r.undercut_hours,1)+'시간마다'}<small>관찰 ${n(r.watched_hours,1)}시간 · ${n(r.undercuts)}번</small></td><td>${a?`<span class="${a.margin>0?'positive':'warn'}">${a.margin>0?'+':''}${n(a.margin)} · ${n(a.roi*100,1)}%</span><small>${n(a.buy)} → ${n(a.sell)} · ${esc(a.basis)} 기준</small>`:'—'}</td></tr>`}),'studies'));
}
const MY_ORDERS_PAGE=30;let showAllMyOrders=false;
function renderMyOrders(){
 const rows=state.my_orders;
 if(!rows){updateHtml('my-orders',empty('내 매물 기능을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 if(!$('my-item').options.length){$('my-item').innerHTML=Object.entries(state.items).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');$('my-city').innerHTML=Object.entries(state.cities).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('')}
 const auto=state.client_debug;
 $('my-auto-status').className=auto?.enabled||auto?.fork?'help':'warn';
 $('my-auto-status').textContent=!auto?'':auto.fork?(auto.last_direct?`자동 인식 켜짐 · 전용 수집기가 내 주문 목록을 직접 보냅니다 · 마지막으로 받은 때 ${ago(auto.last_direct)}`:'자동 인식 켜짐 · 전용 수집기 사용 중. 게임 시장 창에서 내 주문 탭(매도·매수)을 한 번 열어 주세요.'):auto.enabled?(auto.last_my_orders?`자동 인식 켜짐 · 마지막으로 게임의 내 주문 목록을 읽은 때 ${ago(auto.last_my_orders)}`:'자동 인식 켜짐 · 게임 시장 창에서 내 주문 탭을 한 번 열어 주세요.'):'자동 인식 꺼짐: 수집기가 디버그 기록 없이 켜져 있습니다. 최신 버전을 받은 뒤 게임을 끄고 10초 뒤 다시 켜면 켜집니다.';
 const sideName={sell:'매도',buy:'매수'},filt=myOrderFilter(rows);
 // A row passes every filter but the one being counted, so each chip shows what choosing it would list.
 const match=(r,skip)=>['item','city','side'].every(k=>k===skip||filt[k]==='all'||r[k]===filt[k]);
 const chips=(key,label,values,name)=>`<div class="filter-row"><span>${label}</span><div class="segmented">${['all',...values].map(v=>{const on=filt[key]===v;return `<button type="button" data-key="${key}" data-value="${esc(v)}" class="${on?'active':''}" aria-pressed="${on}">${v==='all'?'전체':esc(name(v))} ${n(rows.filter(r=>match(r,key)&&(v==='all'||r[key]===v)).length)}</button>`}).join('')}</div></div>`;
 const present=(key,order)=>order.filter(v=>rows.some(r=>r[key]===v));
 updateHtml('my-orders-filters',rows.length?chips('item','품목',present('item',Object.keys(state.items)),v=>state.items[v])+chips('city','도시',present('city',Object.keys(state.cities)),v=>state.cities[v])+chips('side','종류',['sell','buy'],v=>sideName[v]):'');
 const shown=rows.filter(r=>match(r));
 updateHtml('my-orders',!rows.length?empty('등록한 내 매물이 없습니다. 게임 시장 창에서 내 주문 탭(매도·매수)을 열면 자동으로 들어옵니다.'):!shown.length?empty('고른 조건에 맞는 내 주문이 없습니다.'):table(['품목 · 도시','종류','내 가격','처음 수량 → 남은 수량','팔림/채워짐','내 앞 물량','상태',''],shown.slice(0,showAllMyOrders?shown.length:MY_ORDERS_PAGE).map(r=>`<tr><td>${tierLabel(state.items[r.item])}${r.source==='auto'?'':' <span class="pill">직접</span>'}<br><small>${esc(state.cities[r.city])}</small></td><td class="side-${r.side}">${sideName[r.side]}</td><td>${n(r.price)}</td><td>${n(r.amount)} → <b>${n(r.remaining)}</b></td><td>${n(r.filled)}개</td><td>${r.order_id?n(r.ahead)+'개':'—'}<small>${r.best==null?'':(r.side==='sell'?'최저 매도 ':'최고 매수 ')+n(r.best)}</small></td><td>${!r.order_id?'<span class="warn">아직 연결 안 됨 · 게임에서 내 주문 탭이나 이 시장을 열어 주세요</span>':r.is_best?'<span class="positive">맨 앞</span>':`<span class="quiet">${r.side==='sell'?'더 싼 매물 있음':'더 비싼 매수 있음'}</span>`}<small>${r.seen?'마지막 확인 '+ago(r.seen):'등록 '+ago(r.created)}</small></td><td><button type="button" class="icon-button" data-close="${r.id}" title="목록에서 빼기" aria-label="${esc(state.items[r.item])} ${esc(state.cities[r.city])} ${n(r.price)} 목록에서 빼기">×</button></td></tr>`),'my-orders')+(shown.length>MY_ORDERS_PAGE?`<p class="more-row"><button type="button" class="secondary small-button" data-more>${showAllMyOrders?'처음 '+MY_ORDERS_PAGE+'개만 보기':'나머지 '+n(shown.length-MY_ORDERS_PAGE)+'개 더 보기'}</button></p>`:''));
}
// The my-listings filter (item, city, side), remembered; a choice no longer in the list falls back to all.
function myOrderFilter(rows){
 const saved={item:'all',city:'all',side:storage.get('myOrdersView','all'),...storage.get('myOrdersFilter',{})};
 for(const k of ['item','city','side'])if(saved[k]!=='all'&&!rows.some(r=>r[k]===saved[k]))saved[k]='all';
 return saved;
}
function renderStale(){
 const rows=state.stale_listings;
 if(!rows){updateHtml('stale-listings',empty('이 기능을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 $('stale-context').textContent=`3일 넘게 안 팔림 ${n(rows.length)}건 · 판매세 ${n(state.settings.sales_tax*100,1)}% · 주문 수수료 ${n(state.settings.setup_fee*100,1)}%`;
 const days=d=>d==null?'판매량 미확인':d===0?'바로':d<1?'하루 안':`약 ${n(d,1)}일`;
 const margin=o=>o.margin==null?'원가 미확인':`<em class="${o.margin>=0?'positive':'warn'}">원가 대비 ${o.margin>=0?'+':''}${n(o.margin)}/개</em>`;
 const tile=(title,o,detail,missing,pick)=>`<div class="${pick?'pick':''}"><small>${title}${pick?' · 추천':''}</small>${o?`<b>개당 ${n(o.net)}</b><span>${detail}</span><span>${margin(o)}</span><span>${days(o.days)}</span>`:`<b>—</b><span>${missing}</span>`}</div>`;
 const pace=r=>r.per_day==null?'판매량 미확인':r.instant_buy_share==null?`하루 ${n(r.per_day,1)}개 팔림 (매수 주문 체결 포함)`:`매도 주문 하루 ${n(r.per_day,1)}개 팔림 (판매량의 ${n(r.instant_buy_share*100)}%)`;
 const advice=r=>{const w=r.wait,l=r.relist;
  if(r.advice==='relist')return ['positive','가격 고치기',`${n(l.price)}로 다시 올리면 ${days(l.days)}, 그대로면 ${days(w.days)} · 개당 ${n(w.net-l.net)} 덜 받는 대신 ${days(w.days-l.days)} 빨리`];
  if(r.advice==='cheapest')return ['','기다리기',`이미 최저가라 고쳐도 앞당겨지지 않습니다 · ${pace(r)}`];
  if(r.advice==='soon')return ['','기다리기',`${days(w.days)} 뒤 내 차례`];
  if(r.advice==='below_cost')return ['','기다리기',l?`고치면 원가보다 개당 ${n(-l.margin)} 손해`:'고칠 가격이 없음'];
  return ['warn','판단 보류','이 시장 판매량 이력이 부족합니다 · 게임에서 24시간 시장 이력을 열어 주세요']};
 updateHtml('stale-listings',rows.length?rows.map(r=>{const [tone,title,why]=advice(r),relist=r.advice==='relist',wait=['cheapest','soon','below_cost'].includes(r.advice);
  return `<article class="stale-card"><div class="stale-head"><b>${tierLabel(state.items[r.item]||r.item)} · ${esc(state.cities[r.city]||r.city)}</b><span class="pill">${n(r.idle_days,1)}일째 안 팔림</span></div><p class="help">내 가격 ${n(r.price)} · 남은 ${n(r.remaining)}개 · ${r.sold_before?'마지막 판매 후':'처음 본 뒤 판매 없음'} · ${r.cost==null?'원가 미확인 (매수 기록을 넣으면 손익 계산)':'원가 '+n(r.cost)} · 내 앞 ${n(r.ahead)}개 (최저가 ${n(r.best)}, 호가 ${r.book_seen?ago(r.book_seen):'미조회'}) · ${pace(r)}</p><p class="stale-advice">추천: <b class="${tone}">${title}</b> — ${why}</p><div class="diag-grid">${tile('그대로 기다리기',r.wait,`${n(r.wait.price)} 그대로`,'',wait)}${tile('가격 고치기',r.relist,r.relist?`${n(r.relist.price)}로 다시 올림 · 수수료 ${n(r.relist.fee)}`:'','이미 최저가',relist)}${tile('매수 주문에 바로 팔기',r.instant,r.instant?`${r.instant.units<r.remaining?`${n(r.instant.units)} / ${n(r.remaining)}개만`:`${n(r.instant.units)}개`} · 평균 ${n(r.instant.price)}`:'','매수 주문 없음',false)}</div></article>`}).join(''):empty('3일 넘게 안 팔린 내 매도 주문이 없습니다.'));
}
function renderCapital(){
 const c=state.capital;
 if(!c){updateHtml('capital',empty('투자금 추적을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const items=Object.entries(c.items);
 $('capital-context').textContent=`재고 경고 기준 ${n(c.heavy_days,1)}일치`;
 const change=ch=>ch.delta==null?`<b>—</b><span>${ch.days}일 전 기록 없음 · 쌓는 중</span>`:`<b>${ch.delta>0?'▲ +':ch.delta<0?'▼ ':''}${n(ch.delta)}</b><span>${ch.ratio==null?'':`${ch.ratio>0?'+':''}${n(ch.ratio*100,1)}% · `}${date(ch.at)} ${n(ch.total)}에서</span>`;
 const tiles=`<div class="diag-grid capital-grid"><div><small>지금 총 투자금</small><b>${n(c.total)}</b><span>보유 원가 ${n(c.holdings)} + 매수 주문 ${n(c.orders)}${c.unpriced?` · 원가 미확인 ${n(c.unpriced)}개 제외`:''}</span></div>${c.changes.map(ch=>`<div><small>${ch.days===1?'하루':ch.days+'일'} 전 대비</small>${change(ch)}</div>`).join('')}</div>`;
 const rows=items.map(([item,it])=>`<tr><td>${tierLabel(state.items[item]||item)}</td><td>${n(it.held)}개</td><td>${n(it.buying)}개</td><td>${it.cost==null?'원가 미확인':n(it.cost)}</td><td>${n(it.value+it.orders_silver)}${it.unpriced?`<small>원가 미확인 ${n(it.unpriced)}개 제외</small>`:''}</td><td>${it.per_day==null?'판매량 미확인':n(it.per_day)+'개'}</td><td>${it.days==null?'—':n(it.days,1)+'일'}</td><td>${it.heavy?'<span class="warn">재고 많음 · 재고 쌓는 매수 추천 줄임</span>':it.days==null?'<small>판매량 이력이 없어 판단 못 함</small>':'<span class="positive">괜찮음</span>'}</td></tr>`);
 const daily=[...c.daily].reverse().slice(0,14).map((d,i,arr)=>{const prev=arr[i+1];const diff=prev?d.total-prev.total:null;return `<tr><td>${esc(d.date)}</td><td>${n(d.total)}</td><td>${n(d.holdings)}</td><td>${n(d.orders)}</td><td>${diff==null?'—':`${diff>0?'▲ +':diff<0?'▼ ':''}${n(diff)}`}</td></tr>`});
 updateHtml('capital',tiles+(rows.length?`<h3 class="sub-head">품목별</h3>${table(['품목','보유','매수 주문 대기','원가','투자금','시장 하루 판매','재고 일수','상태'],rows,'capital-items')}`:empty('지금 묶인 투자금이 없습니다.'))+(daily.length?`<h3 class="sub-head">날짜별 (그날 마지막 기록)</h3>${table(['날짜','총 투자금','보유 원가','매수 주문','전날 대비'],daily,'capital-daily')}`:''));
}
function renderHoldings(){
 const h=state.holdings;
 if(!h){updateHtml('holdings',empty('보유량을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 if(!$('bank-item').options.length){$('bank-item').innerHTML=Object.entries(state.items).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');$('bank-city').innerHTML=Object.entries(state.cities).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('')}
 const byCity={};for(const r of h.rows)(byCity[r.city]??=[]).push(r);
 const valued=h.rows.filter(r=>r.value!=null),unpriced=h.rows.length-valued.length,place=city=>city==='carried'?'들고 있는 것':state.cities[city]||city;
 const bankCell=r=>r.city==='carried'?'—':`${n(r.bank)}개${r.bank_source==='auto'?`<small>자동 · ${ago(r.bank_updated)} 연 은행</small>`:r.bank_source==='manual'?`<small>${ago(r.bank_updated)} 적음</small>`:''}`;
 $('holdings-context').textContent=h.rows.length?`${n(Object.keys(byCity).filter(c=>c!=='carried').length)}개 도시 · 원가 기준 ${n(valued.reduce((s,r)=>s+r.value,0))}${unpriced?` (원가 미확인 ${n(unpriced)}줄 제외)`:''}`:'';
 const rows=Object.entries(byCity).flatMap(([city,list])=>list.map((r,i)=>`<tr class="${i?'':'log-first'}"><td>${i?'':esc(place(city))}</td><td>${tierLabel(state.items[r.item]||r.item)}</td><td>${r.city==='carried'?'—':n(r.listed)+'개'}</td><td>${bankCell(r)}</td><td>${r.city==='carried'?'—':`${n(r.finished)}개${r.finished_seen?`<small>${ago(r.finished_seen)} 확인</small>`:''}`}</td><td>${r.city==='carried'?`${n(r.carried)}개<small>${ago(r.bank_updated)} 확인</small>`:'—'}</td><td><b>${n(r.total)}개</b></td><td>${r.value==null?'원가 미확인':n(r.value)}</td><td>${r.city==='carried'?'—':r.value_now==null?'사는 사람 없음':`${n(Math.round(r.value_now))}<small>단가 ${n(Math.round(r.net_now))} · 잔량 ${n(r.sell_now_amount)}개</small>`}</td></tr>`));
 const finished=h.finished.length?`<h3 class="sub-head">거래 완료 탭 (시장별 마지막으로 받은 목록)</h3>${table(['시장','항목','추적 품목','내가 사서 안 찾은 것','받은 때'],h.finished.map(f=>`<tr><td>${esc(state.cities[f.city]||f.location)}</td><td>${n(f.entries)}건</td><td>${n(f.tracked)}건</td><td>${f.identity_known?n(f.waiting)+'개':'<span class="warn">캐릭터 확인 불가</span>'}</td><td>${ago(f.received)}</td></tr>`),'finished-lists')}`:'<p class="help">거래 완료 탭 목록은 아직 받지 않았습니다. 전용 수집기 local.3부터 게임 시장 창에서 거래 완료 탭을 열면 받아 둡니다.</p>';
 const unmapped=(h.unmapped_banks||[]).length?`<div><p class="warn">어느 도시 은행인지 모르는 은행이 있습니다. 빠른 이동 도착지처럼 도시가 아닌 곳에서 은행에 들어가면 이렇게 됩니다. 도시를 한 번 골라 주면 기억합니다.</p>${h.unmapped_banks.map(b=>`<form class="bank-city-form my-order-form" data-location="${esc(b.location)}"><span>은행 구역 ${esc(b.location)} · ${Object.entries(b.items).map(([k,v])=>`${esc(state.items[k]||k)} ${n(v)}개`).join(', ')||'추적 품목 없음'} · ${ago(b.seen)} 연 은행</span><label>도시<select name="city">${Object.entries(state.cities).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('')}</select></label><button type="submit">이 도시로 정하기</button></form>`).join('')}<p class="help" id="bank-city-status"></p></div>`:'';
 // 품목별 한 줄 요약: 얼마에 사서 몇 개 들고 있고, 지금 팔면 얼마가 되는가.
 const byItem={};
 for(const r of h.rows){const g=byItem[r.item]??={total:0,cost_silver:0,costed:0,value_now:0,priced:0,unpriced:0,instant:0,instant_units:0,cities:{}};
  g.total+=r.total; if(r.total){const c=g.cities[r.city]??={total:0,bank:0,listed:0,finished:0};c.total+=r.total;c.bank+=r.bank;c.listed+=r.listed;c.finished+=r.finished}
  if(r.cost!=null&&r.total){g.cost_silver+=r.total*r.cost; g.costed+=r.total}
  if(r.list_net!=null&&r.total){g.value_now+=r.total*r.list_net; g.priced+=r.total} else if(r.total) g.unpriced+=r.total;
  if(r.net_now!=null&&r.total){g.instant+=r.total*r.net_now; g.instant_units+=r.total}}
 const items=Object.entries(byItem).filter(([,g])=>g.total);
 const sum=items.reduce((a,[,g])=>({buy:a.buy+g.cost_silver,now:a.now+g.value_now}),{buy:0,now:0});
 const money=v=>v==null?'—':n(Math.round(v));
 const summary=items.length?table(
   ['품목','총 갯수','평균 매수가','총 매수금액','제일 비싸게 팔 곳','팔면 단가','총금액','총수익'],
   items.map(([item,g])=>{
     const avg=g.costed?g.cost_silver/g.costed:null, unit=g.priced?g.value_now/g.priced:null;
     const profit=(avg!=null&&unit!=null)?g.value_now-(g.priced*avg):null;
     const warn=g.unpriced?`<small class="warn">${n(g.unpriced)}개는 팔 곳 호가 없음</small>`:'',b=h.best_listing?.[item];
     const s=b?.sweep,how=!b?'':s?(s.units?`앞 매물 ${n(s.units)}개(${n(s.levels)}개 가격대 · ${n(s.cost)} 실버)를 사들이고 다음 매도가보다 1 싸게 · 산 것도 같이 팔기`:'내 매도 주문을 다음 매도가보다 1 싸게 다시 올림')+` · 그냥 걸면 ${n(s.plain_price)}`:b.own?'내 주문이 최저가 · 같은 값':'최저 매도가보다 1 싸게';
     const where=b?`${esc(state.cities[b.city]||b.city)} <b>${n(b.price)}</b><small>${how}</small>${b.sell_days!=null?`<small class="${b.sell_days>3?'warn':''}">내 몫(하루 판매량 ${n(state.settings.cycle_share*100)}%)으로 다 팔려면 약 ${n(b.sell_days,1)}일</small>`:''}<small>${b.fresh?'유효':'지난 자료'} · ${ago(b.seen)}</small>`:'—';
     const parts=c=>[['은행',c.bank],['시장',c.listed],['완료 탭',c.finished]].filter(([,v])=>v),banks=Object.entries(g.cities).map(([city,c])=>{const p=parts(c);return `<small>${esc(place(city))} ${n(c.total)}개${p.length>1||(p.length===1&&city!=='carried')?` (${p.map(([k,v])=>`${k} ${n(v)}`).join(' · ')})`:''}</small>`}).join('');
     const instant=g.instant_units?`<small>바로 팔면 ${money(g.instant/g.instant_units)}</small>`:'';
     return `<tr><td>${tierLabel(state.items[item]||item)}${banks}</td>`
       +`<td><b>${n(g.total)}개</b></td><td>${money(avg)}</td><td>${money(g.cost_silver||null)}</td>`
       +`<td>${where}</td><td>${money(unit)}${instant}${warn}</td><td>${money(g.value_now||null)}</td>`
       +`<td class="${profit==null?'':profit>=0?'positive':'warn'}">${money(profit)}</td></tr>`}),
   'holdings-by-item')
   +`<details class="more-help"><summary>계산 방법 보기</summary><p class="help"><b>제일 비싸게 팔 곳</b> = 거래 분석 도시 중 매도 주문을 가장 비싸게 걸 수 있는 곳입니다. 마지막으로 연 매도 목록의 최저 매도가보다 1 싸게 걸고, 최저가가 내 주문이면 같은 값으로 봅니다. 앞에 다른 판매자 매물이 적게 깔려 있으면 그것을 먼저 사들이고(본 매도 목록 안, 가격대 수 제한 없이 한 거래 최대 실버까지) 다음 매도가보다 1 싸게 거는 것도 계산해, 산 물량 값까지 넣어 총액이 더 크면 그 방법으로 보여 줍니다. 그만큼 팔 물량이 늘어납니다. <b>팔면 단가</b> = 그 값에서 판매세 ${n(state.settings.sales_tax*100,1)}%와 설치 수수료 ${n(state.settings.setup_fee*100,1)}%를 빼고, 다른 도시에 있는 물량은 개당 운송비(설정)를 뺀 값입니다. 그 값에 다 팔린다는 보장은 없습니다. 사들이는 경우 사는 데 쓴 실버를 빼고 내 보유 수량으로 나눈 값입니다. 작은 글씨 '바로 팔면'은 매수 요청서에 즉시 팔 때(최고 매수가 − 판매세)입니다. 총수익은 원가를 아는 수량만 셉니다.</p></details>`
   +`<p class="help">합계 — 매수 ${money(sum.buy||null)} → 제일 비싸게 팔면 ${money(sum.now||null)} (차액 <b class="${sum.now-sum.buy>=0?'positive':'warn'}">${money(sum.now-sum.buy)}</b>)</p>`:'';
 updateHtml('holdings',(items.length?`<h3 class="sub-head">품목별 — 사서 들고 있는 것과 지금 가치</h3>${summary}`:'')
   +(rows.length?`<h3 class="sub-head">도시별 자세히</h3>`+table(['도시','품목','시장에 올린 것','은행','거래 완료 탭','인벤토리','합계','원가 기준 금액','바로 팔면 (최고 매수가)'],rows,'holdings'):empty('보유량이 아직 없습니다. 게임에서 내 주문 탭이나 거래 완료 탭, 은행을 열거나, 위에 은행 수량을 적어 주세요.'))+unmapped+finished);
}
function renderPurchases(){
 const p=state.purchases;
 if(!p){updateHtml('purchases',empty('매수 기록을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 if(!$('buy-item').options.length){$('buy-item').innerHTML=Object.entries(state.items).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');$('buy-city').innerHTML=Object.entries(state.cities).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('')}
 $('purchases-context').textContent=`원가 = 매수 주문 수수료 ${n(p.setup_fee*100,1)}% 포함${state.settings.fees_confirmed?'':' · 수수료 설정 미확인'}`;
 const source={instant_buy:'자동 · 바로 사기 (게임에서 읽음)',mail:'구매 주문 완료 메일',expired_mail:'구매 주문 만료 메일 (산 만큼)',open_order:'진행 중 매수 주문 (채워진 만큼)'};
 const how=r=>r.source==='manual'?(r.kind==='instant'?'직접 입력 · 바로 사기':'직접 입력 · 매수 주문'):source[r.source]||esc(r.source);
 const summary=p.summary.length?table(['품목',`최근 ${p.window_days}일 수량`,'평균 매수가','원가 (수수료 포함)','전체 수량','전체 평균 매수가','전체 원가'],p.summary.map(s=>`<tr><td>${tierLabel(state.items[s.item]||s.item)}</td><td>${n(s.recent.units)}개</td><td>${n(s.recent.average)}</td><td>${n(s.recent.cost_average)}</td><td>${n(s.all.units)}개</td><td>${n(s.all.average)}</td><td>${n(s.all.cost_average)}</td></tr>`),'purchase-summary'):'';
 const rows=p.recent.map(r=>`<tr><td>${date(r.at)}</td><td>${tierLabel(state.items[r.item]||r.item)} · ${esc(state.cities[r.city]||r.city)}</td><td>${how(r)}</td><td>${n(r.unit)}${r.order_price!=null&&Math.round(r.order_price)!==Math.round(r.unit)?`<small>주문 단가 ${n(r.order_price)}</small>`:''}</td><td>${n(r.amount)}개</td><td>${n(r.silver)}</td><td>${r.id!=null?`<button type="button" class="secondary small-button" data-remove-purchase="${r.id}">삭제</button>`:''}</td></tr>`);
 updateHtml('purchases',rows.length?`<h3 class="sub-head">품목별 평균</h3>${summary}<h3 class="sub-head">최근 기록 ${n(rows.length)}개</h3>${table(['산 때','품목 · 도시','어디서','단가','수량','지출',''],rows,'purchases')}`:empty('매수 기록이 아직 없습니다. 구매 주문 완료 메일을 열거나, 바로 산 것을 위에 적으면 쌓입니다.'));
}
// Trial items (테스트·보류) are tracked and shown but left out of picks and the plan.
function trialNote(where){const t=(state.trial_items||[]).map(i=>esc(state.items[i]||i));return t.length?`<p class="help">${t.join(', ')}: 지켜보는 중이라 ${where}에서 뺐습니다. 시장 상세·매매 후보에서는 볼 수 있습니다.</p>`:''}
function renderNow(){
 const picks=state.now;
 if(!picks){updateHtml('now-instant',empty('추천을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const fresh=Object.values(state.books).flatMap(c=>Object.values(c)).filter(p=>p.sell.fresh||p.buy.fresh).length,total=Object.keys(state.items).length*Object.keys(state.cities).length;
 const clock=new Date();$('now-context').textContent=`내 컴퓨터 ${WEEK[(clock.getDay()+6)%7]}요일 ${clock.toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'})} · 최근 ${n(state.settings.fresh_minutes)}분 안에 연 시장 ${n(fresh)}/${n(total)}곳 · 한 거래 최대 ${n(state.settings.trade_budget)} 실버${(state.trial_items||[]).length?` · 추천 제외: ${state.trial_items.map(i=>state.items[i]||i).join(", ")}`:''}`;
 const hidden=settingsWarnings();
 $('now-note').textContent=(hidden.length?hidden.join(' · ')+' — 설정 탭에서 게임 값과 맞춰 주세요. ':'')+'예상 이익은 본 호가와 최근 판매량으로 계산한 값이며 체결을 보장하지 않습니다.';
 const range=(a,b)=>a===b||b==null?n(a):`${n(a)}~${n(b)}`;
 const city=c=>esc(state.cities[c]);
 const steps=p=>{const item=esc(state.items[p.item]),q=n(p.units)+'개';switch(p.kind){
  case 'instant':return [`${city(p.buy_city)} 매도 요청서에서 ${item} ${q} 바로 구매 · ${range(p.buy_low,p.buy_high)}`,`${city(p.sell_city)}로 운반`,`${city(p.sell_city)} 매수 요청서에 바로 판매 · ${range(p.sell_high,p.sell_low)}`];
  case 'instant_list':return [`${city(p.buy_city)} 매도 요청서에서 ${item} ${q} 바로 구매 · ${range(p.buy_low,p.buy_high)}`,`${city(p.sell_city)}로 운반`,`${city(p.sell_city)}에 ${n(p.sell_high)}로 매도 주문 등록 · 하루 약 ${n(p.sell_per_day)}개 팔림`];
  case 'best':return [`${city(p.buy_city)}에 ${item} 매수 주문 ${q} · ${n(p.buy_low)}`,`체결되면 ${city(p.sell_city)}로 운반`,`${city(p.sell_city)}에 ${n(p.sell_high)}로 매도 주문`];
  default:return [`${city(p.buy_city)} 매도 요청서의 ${item} ${q} 싹쓸이 · ${range(p.buy_low,p.buy_high)}`,`최저가가 ${n(p.floor_after)}로 오름`,`${n(p.sell_high)}로 다시 매도 주문 · 하루 약 ${n(p.sell_per_day)}개 팔림`]}};
 const title=p=>p.kind==='buyout'?`${city(p.buy_city)} ${tierLabel(state.items[p.item])} 매물 싹쓸이`:`${tierLabel(state.items[p.item])} · ${city(p.buy_city)} → ${city(p.sell_city)}`;
 const history=state.pick_history?.[0],last=!picks.length&&history;
 const groupOf=p=>p.group||(p.kind==='instant'?'instant':'wait');
 const groups=last?{instant:last.picks.filter(p=>groupOf(p)==='instant'),wait:last.picks.filter(p=>groupOf(p)==='wait')}:(state.now_groups||{instant:picks.filter(p=>groupOf(p)==='instant'),wait:picks.filter(p=>groupOf(p)==='wait')});
 if(last)$('now-note').textContent=`지금 계산할 최근 호가가 없어 ${ago(last.created)}(${date(last.created)}) 마지막 추천을 보여 줍니다. 가격이 바뀌었을 수 있으니 게임에서 시장을 다시 열어 확인하세요.`;
 $('now').classList.toggle('stale-picks',Boolean(last));
 const card=(p,i)=>`<li class="now-card ${p.kind}"><div class="now-rank">${i+1}</div><div class="now-body"><div class="now-title">${last?'<span class="pill past-pill">지난 추천</span>':''}<b>${title(p)}</b><span class="pill ${p.kind==='instant'?'instant-badge':''}">${esc(p.certainty)}</span></div><ol class="now-steps">${steps(p).map(x=>`<li>${x}</li>`).join('')}</ol>${p.timing?`<p class="timing">${esc(timingText(p.timing))}</p>`:''}${p.group!=='instant'&&p.outlook?`<p class="timing">${esc(outlookText(p.outlook))}</p>`:''}${p.warnings.length?`<p class="board-warn">${p.warnings.map(esc).join(' · ')}</p>`:''}</div><div class="now-money"><strong class="positive">+${n(p.profit)}</strong><small>투자 ${n(p.cost)} · ${n(p.roi*100,1)}%</small></div></li>`;
 const none=fresh?'지금 이 방식으로 이익이 남는 거래가 없습니다.':'최근 연 시장이 없습니다. 게임에서 도시 시장의 매수·매도 목록을 열면 바로 계산합니다.';
 for(const group of ['instant','wait'])updateHtml('now-'+group,groups[group].length?groups[group].map(card).join(''):`<li class="now-empty">${empty(none)}</li>`);
}
function renderBoard(){
 const board=state.city_board;
 if(!board){updateHtml('city-board',empty('도시별 정리를 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const hidden=settingsWarnings(),total=Object.keys(state.items).length;
 $('board-settings').hidden=!hidden.length;
 $('board-settings').textContent=hidden.length?hidden.join(' · ')+' · 예상 순익은 아래 설정의 현재 값 기준입니다. 게임에 표시된 값과 맞춘 뒤 확인란을 체크해 주세요.':'';
 $('board-context').textContent=`최근 ${n(state.settings.fresh_minutes)}분 안의 호가만 사용 · ${n(board.filter(c=>c.fresh_items).length)}/${n(board.length)}개 도시 자료 있음`;
 const gain=r=>`<strong class="positive">+${n(r.net_per_unit)} · ${n(r.roi*100,1)}%</strong>`;
 const dest=r=>r.sell_city===r.buy_city?'같은 도시':esc(state.cities[r.sell_city]);
 updateHtml('city-board',board.map(c=>{
  const name=esc(state.cities[c.city]);
  if(c.excluded)return `<article class="board-card excluded"><div class="board-head"><h3>${name}</h3><small>거래 분석 제외</small></div><p class="board-empty">설정에서 뺀 도시입니다. 시세는 시장 상세 탭에서 볼 수 있습니다.</p></article>`;
  const routes=c.routes.map(r=>{const alt=r.alternative;return `<li><div class="board-line"><b>${tierLabel(state.items[r.item])}</b>${gain(r)}</div><div class="board-sub">${n(r.buy_price)} → ${dest(r)} ${n(r.sell_price)}</div><div class="board-sub">${esc(shortGuide(guideFor(r.item,r.sell_city)))}</div><div class="board-mode"><span class="pill ${r.mode==='best'?'':'instant-badge'}">${esc(r.status)}</span>${alt?`<span class="board-alt">${esc(alt.status)}${alt.sell_city===r.sell_city?'':' · '+dest(alt)}: +${n(alt.net_per_unit)} · ${n(alt.roi*100,1)}%</span>`:''}</div>${r.warnings.length?`<div class="board-warn">${r.warnings.map(esc).join(' · ')}</div>`:''}</li>`}).join('');
  const best=(rows,label)=>rows.map(r=>`<li><div class="board-line"><b>${tierLabel(state.items[r.item])}</b><span>${n(r.price)} <small>${n(r.amount)}개</small></span></div><div class="board-sub">${n(r.compared)}개 도시 중 ${label}${r.lead?` · 다음 도시와 ${n(r.lead)} 차이`:' · 동률'}</div></li>`).join('');
  const sell=best(c.sell_here,'최고 매수'),buy=best(c.buy_here,'최저 매도'),b=c.buyout;
  const buyout=b?`<h4 class="buyout-head">매물 싹쓸이</h4><ul><li><div class="board-line"><b>${tierLabel(state.items[b.item])}</b><strong class="positive">+${n(b.best.profit)}</strong></div><div class="board-sub">${n(b.best.units)}개 · ${n(b.best.cost)} 실버로 최저가 ${n(b.floor_now)} → ${n(b.best.floor)} (+${n(b.best.lift*100,1)}%)</div><div class="board-mode"><span class="pill risk-${b.best.risk}">${riskText[b.best.risk]}</span></div></li></ul>`:'';
  return `<article class="board-card ${c.city===selectedCity?'active':''}"><div class="board-head"><h3>${name}</h3><small>최신 ${n(c.fresh_items)}/${n(total)} 품목${c.newest_seen?' · '+ago(c.newest_seen):''}</small></div>${routes?`<h4>여기서 사서 팔 곳</h4><ul>${routes}</ul>`:''}${sell?`<h4 class="sell-here">여기서 바로 팔기 가장 비쌈</h4><ul>${sell}</ul>`:''}${buy?`<h4 class="buy-here">여기서 바로 사기 가장 쌈</h4><ul>${buy}</ul>`:''}${buyout}${routes||sell||buy||buyout?'':`<p class="board-empty">${c.fresh_items?'수익 조건을 넘는 거래 없음':'유효 호가 없음 · 게임에서 이 도시 시장을 열면 채워집니다.'}</p>`}${c.missing.length?`<p class="board-missing">다시 열어볼 시장: ${c.missing.map(m=>`${esc(state.items[m.item])}(${esc(m.reason)})`).join(', ')}</p>`:''}<button type="button" class="secondary board-detail" data-city="${esc(c.city)}">${name} 상세 보기</button></article>`;
 }).join(''));
}
function renderBuyouts(){
 const rows=state.buyouts;
 if(!rows){updateHtml('buyouts',empty('싹쓸이 분석을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const hidden=settingsWarnings(),lists=Object.values(state.books).flatMap(cities=>Object.values(cities)).filter(pair=>pair.sell.fresh).length;
 $('buyout-notes').textContent=[...(state.buyout_notes||[]),...hidden].join(' · ');
 $('buyout-context').textContent=`유효 매도 목록 ${n(lists)}곳 중 이익 나는 곳 ${n(rows.length)}곳`;
 const buyoutRow=r=>{const b=r.best,reasons=r.reasons.filter(x=>!hidden.includes(x));return `<tr><td>${tierLabel(state.items[r.item])}<br>${esc(state.cities[r.city])}<small>매도 목록 ${ago(r.seen)} · ${n(r.visible_levels)}개 가격대 ${n(r.visible_units)}개</small></td><td>${n(r.floor_now)} → <b>${n(b.floor)}</b><small class="positive">+${n(b.lift*100,1)}%</small><small>다음 매물 ${n(b.next_amount)}개${b.next_wall?' · 물량 벽':''}</small></td><td><b>${n(b.units)}개</b><br>${n(b.cost)} 실버<small>평균 매입가 ${n(b.average_cost,1)}</small><span class="pill risk-${b.risk}">${riskText[b.risk]}</span></td><td>${n(r.history_average,1)}<small>최고 구간 평균 ${n(r.history_high,1)}</small><small>${esc(guideLine(guideFor(r.item,r.city)))}</small></td><td>${n(b.relist)}<small class="positive">+${n(b.profit)} · ${n(b.roi*100,1)}%</small></td><td><small>${r.steps.slice(0,5).map(s=>`${n(s.units)}개 ${n(s.cost)} → ${n(s.floor)} (+${n(s.lift*100,1)}%) · 이익 ${s.profit>0?'+':''}${n(s.profit)} · ${riskText[s.risk]}`).join('<br>')}</small>${reasons.length?`<div class="reason-list">${reasons.map(x=>`<span class="pill reason">${esc(x)}</span>`).join('')}</div>`:''}</td></tr>`};
 const buyoutHead=['품목 · 도시','지금 최저가 → 정리 후','사야 할 개수 · 총 비용','7일 평균가 · 판매 속도','다시 올릴 가격 · 예상 이익','단계별 비용 · 확인할 점'],pastBuyouts=state.past_buyouts?.rows||[];
 const noBuyouts='지금 싹쓸이해서 이익이 남는 시장 없음 · 유효 시간 안의 매도 목록만 계산합니다';
 updateHtml('buyouts',rows.length?table(buyoutHead,rows.map(buyoutRow),'buyouts'):pastBuyouts.length?`<p class="warn past-note">${esc(noBuyouts)}</p><h3 class="sub-head">지난 분석 · ${date(state.past_buyouts.seen)} (${ago(state.past_buyouts.seen)}) 기준</h3><p class="help">그때 본 매도 목록으로 계산한 결과입니다. 지금은 매물이 팔리거나 새로 올라와 바뀌었을 수 있으니, 사기 전에 게임에서 그 시장을 다시 열어 확인해 주세요.</p>${table(buyoutHead,pastBuyouts.map(buyoutRow),'buyouts-past')}`:empty(noBuyouts));
}
let buyMode=storage.get('buyMode','instant')==='order'?'order':'instant';
const buyOpen=new Set();
const shortAgo=v=>{const days=Math.floor((Date.now()-Date.parse(v))/86400000);return days>=2?`${days}일 전`:ago(v)};
function renderBuyUnder(){
 const order=buyMode==='order',rows=order?state.buy_order_under:state.buy_under;
 for(const b of document.querySelectorAll('#buy-under-mode button')){const on=b.dataset.mode===buyMode;b.classList.toggle('active',on);b.setAttribute('aria-pressed',on)}
 for(const h of document.querySelectorAll('[data-buy-help]'))h.hidden=h.dataset.buyHelp!==buyMode;
 if(!rows){updateHtml('buy-under',empty('분석 프로그램을 다시 실행하면 나옵니다.'));return}
 const cities=rows[0]?.rows.map(r=>r.city)||[],city=c=>esc(state.cities[c]||c);
 $('buy-under-context').textContent=`${cities.map(c=>state.cities[c]).join(', ')} · 각 시장의 마지막 ${order?'매수':'매도'} 목록`;
 const cell=r=>!r.known?`<td><small>${r.seen?'단일 주문만 봄':'미조회'}</small></td>`:`<td class="${r.units?'':'muted'}"><b>${n(r.units)}개</b>${r.units?`<small>평균 ${n(r.average)} · ${n(r.cost)} 실버</small>`:''}<small>최저 ${n(r.lowest)}</small><small>${r.fresh?'유효':'지난 자료'} · ${ago(r.seen)}</small></td>`;
 const targetCell=u=>`<td class="bu-target"><form class="buy-target-form" data-item="${esc(u.item)}"><input type="number" name="price" min="1" step="1" inputmode="numeric" aria-label="${esc(state.items[u.item])} 목표가" value="${u.chosen??''}" placeholder="${u.suggested??''}"></form><small>${u.chosen?'직접 정함':u.suggested?'자동 제안':'팔 곳 호가 없음'}${u.chosen&&u.suggested?` · 제안 ${n(u.suggested)}`:''}</small></td>`;
 const sellCell=u=>`<td>${u.sell_city?`${city(u.sell_city)} <b>${n(u.sell)}</b><small>7일 평균 체결 ${u.sell_average!=null?n(u.sell_average):'-'}</small>`:'-'}</td>`;
 const expected=v=>`예상 ${v>0?'+':''}${n(v)}`;
 const line=u=>`<tr><th scope="row">${tierLabel(state.items[u.item])}</th>${targetCell(u)}${u.rows.map(cell).join('')}<td><b>${n(u.units)}개</b>${u.units?`<small>${n(u.cost)} 실버</small>`:''}${u.profit!=null?`<small class="${u.profit>0?'positive':'warn'}">${expected(u.profit)}</small>`:''}</td>${sellCell(u)}</tr>`;
 // Order view: each city shows only the quantity and the orders ahead; the rest unfolds with the row.
 const orderCell=(u,r,top)=>!r.known?`<td class="bu-cell"><small>${r.seen?'단일 주문만 봄':'미조회'}</small></td>`:`<td class="bu-cell${r.units?'':' muted'}${r.fresh?'':' bu-stale'}${r.fresh&&r.units&&r.units===top?' bu-top':''}"><b class="bu-qty">${r.per_day?`${n(r.units)}개`:'물량 모름'}</b>${r.units&&r.price!==u.target?`<small class="bu-num">${n(r.price)}에 주문</small>`:''}${r.price!=null?`<small class="bu-num${r.ahead?'':' positive'}">앞선 ${n(r.ahead)}개</small>`:''}${r.fresh?'':`<small class="bu-age">${shortAgo(r.seen)}</small>`}</td>`;
 const orderTotal=u=>`<td class="bu-total"><b class="bu-qty">${n(u.fresh_units)}개</b>${u.fresh_units?`<small>${n(u.fresh_cost)} 실버</small>`:''}${u.fresh_profit!=null?`<small class="${u.fresh_profit>0?'positive':'warn'}">${expected(u.fresh_profit)}</small>`:''}${u.units!==u.fresh_units?`<small class="bu-all">지난 자료 포함 ${n(u.units)}개 · ${n(u.cost)} 실버${u.profit!=null?` · ${expected(u.profit)}`:''}</small>`:''}</td>`;
 const fact=(label,value,tone='')=>`<dt>${label}</dt><dd class="${tone}">${value}</dd>`;
 const orderDetail=r=>`<div class="bu-detail-city${r.fresh?'':' bu-stale'}"><strong>${city(r.city)}</strong>${r.known?`<dl>${fact('주문 값',r.price!=null?`${n(r.price)} 실버`:'-')}${fact('총 실버',r.units?n(r.cost):'-')}${fact('최고 매수',r.best!=null?n(r.best):'-')}${fact('앞선 주문',`${n(r.ahead)}개`,r.price!=null&&!r.ahead?'positive':'')}${fact('하루 매수 체결',r.per_day?`${n(r.per_day)}개`:'거래 기록 없음')}${fact(r.fresh?'유효':'지난 자료',date(r.seen))}</dl>`:`<dl>${fact(r.seen?'단일 주문만 봄':'미조회',r.seen?date(r.seen):'-')}</dl>`}</div>`;
 const orderLine=u=>{const top=Math.max(0,...u.rows.filter(r=>r.known&&r.fresh).map(r=>r.units)),open=buyOpen.has(u.item),detail=`buy-detail-${esc(u.item)}`;
  return `<tr class="bu-row${open?' open':''}" data-buy-row="${esc(u.item)}"><th scope="row"><button type="button" class="bu-toggle" data-buy-toggle="${esc(u.item)}" aria-expanded="${open}" aria-controls="${detail}">${tierLabel(state.items[u.item])}</button></th>${targetCell(u)}${u.rows.map(r=>orderCell(u,r,top)).join('')}${orderTotal(u)}${sellCell(u)}</tr><tr class="bu-detail" id="${detail}"${open?'':' hidden'}><td colspan="${cities.length+4}"><div class="bu-detail-grid">${u.rows.map(orderDetail).join('')}</div></td></tr>`};
 const focused=document.activeElement?.closest?.('[data-buy-toggle]')?.dataset.buyToggle;
 updateHtml('buy-under',rows.length?table(['품목',order?'목표가 (주문 매수)':'목표가 (바로 사기)',...cities.map(c=>state.cities[c]),order?`합계 (${rows[0].days}일)`:'합계','팔 곳 · 지금 최저 매도'],rows.map(order?orderLine:line),'buy-under'):empty('추적 품목이 없습니다.'));
 if(focused)for(const b of document.querySelectorAll('[data-buy-toggle]'))if(b.dataset.buyToggle===focused)b.focus({preventScroll:true});
}
function renderSellTargets(){
 const rows=state.sell_targets;if(!rows){updateHtml('sell-target',empty('분석 프로그램을 다시 실행하면 나옵니다.'));return}
 const cities=rows[0]?.rows.map(r=>r.city)||[],city=c=>esc(state.cities[c]||c);
 $('sell-target-context').textContent=`${cities.map(c=>state.cities[c]).join(', ')} · 각 시장의 마지막 매도 목록`;
 const ease=(d,partial)=>d==null?'<small>판매량 미확인</small>':`<small class="${partial?'':d<1?'positive':d<3?'':'warn'}">시장 소화 ${n(d,1)}일${partial?'':d<1?' · 쉽게 올라감':d<3?' · 며칠':' · 잘 안 올라감'}</small>`;
 const cell=(u,r)=>!r.known?`<td class="st-cell"><small>${r.seen?'단일 주문만 봄':'미조회'}</small></td>`:u.target==null?'<td class="st-cell">—</td>':`<td class="st-cell"><b class="st-units">${n(r.units)}개</b>${r.units?`<small class="st-money">${n(r.cost)} 실버</small>${ease(r.days,r.partial)}`:'<small class="positive">이미 희망가 이상</small>'}<small class="st-meta" title="${esc(date(r.seen))}">최저 ${n(r.lowest)} · ${r.fresh?'':'지난 · '}${ago(r.seen)}</small>${r.partial?`<small class="warn" title="실제 물량은 더 많을 수 있음">목록을 ${n(r.covered_to)}까지만 봄</small>`:''}${r.mine?.length?`<small class="st-mine" title="${r.mine.map(m=>`${n(m.price)}에 ${n(m.amount)}개`).join(' · ')}">내 매물 ${n(r.mine.length)}건 · ${n(r.mine.reduce((t,m)=>t+m.amount,0))}개</small>`:''}</td>`;
 const mine=u=>{const best=u.rows.filter(r=>r.value!=null&&!r.partial).sort((a,b)=>b.value-a.value)[0];if(!best||!u.held)return '<td>—</td>';
   const profit=u.average_cost!=null?best.value-u.held*u.average_cost:null;
   return `<td>${city(best.city)}<small>${n(best.units)}개 ${n(best.cost)} 실버 사고 ${n(u.target-1)}에 ${n(u.held+best.units)}개 걸기</small><small>받을 돈 ${n(Math.round(best.value))}</small>${profit!=null?`<small class="${profit>=0?'positive':'warn'}">원가 대비 ${profit>=0?'+':''}${n(Math.round(profit))}</small>`:''}</td>`};
 const line=u=>`<tr><th scope="row">${tierLabel(itemName(u.item))}<small>보유 ${n(u.held)}개${u.average_cost!=null?` · 평균 원가 ${n(Math.round(u.average_cost))}`:''}</small></th><td><form class="sell-target-form" data-item="${esc(u.item)}"><input type="number" name="price" min="1" step="1" inputmode="numeric" aria-label="${esc(state.items[u.item])} 희망 판매가" value="${u.chosen??''}" placeholder="${u.suggested??''}"></form><small>${u.chosen?'직접 정함':u.suggested?'원가 + 최소 수익률':'원가 미확인 · 직접 적기'}</small></td>${u.rows.map(r=>cell(u,r)).join('')}${mine(u)}</tr>`;
 updateHtml('sell-target',rows.length?table(['품목','희망 판매가',...cities.map(c=>state.cities[c]),'내가 다 사면 (가장 나은 도시)'],rows.map(line),'sell-target'):empty('추적 품목이 없습니다.'));
}
function saveSellTargets(){const targets={};for(const f of document.querySelectorAll('.sell-target-form')){const v=f.price.value.trim();if(v!=='')targets[f.dataset.item]=Number(v)}return post('/api/settings',{sell_targets:targets})}
function saveBuyTargets(){const targets={};for(const f of document.querySelectorAll('.buy-target-form')){const v=f.price.value.trim();if(v!=='')targets[f.dataset.item]=Number(v)}return post('/api/settings',{[buyMode==='order'?'buy_order_targets':'buy_targets']:targets})}
function renderVolumeGuide(){
 const g=guideFor(selectedItem,selectedCity),labels={1:'최근 24시간',7:'최근 7일',28:'최근 28일'};
 updateHtml('volume-guide',g?`<div class="volume-guide"><h3>사서 되팔 때 수량 경고 · ${esc(state.cities[selectedCity])} ${esc(state.items[selectedItem])}</h3><div class="guide-windows">${g.windows.map(w=>`<div><small>${labels[w.days]} 판매량</small><b>${w.amount==null?'—':n(w.amount)+'개'}</b><span>${w.per_day==null?'하루 평균 계산 불가':'하루 '+n(w.per_day)+'개'} · 관측 ${n(w.covered_hours)}시간</span></div>`).join('')}</div><p class="${g.per_day==null||g.per_day<1?'warn':'guide-line'}">${esc(guideLine(g))}</p>${state.outlook?`<p class="${state.outlook.low_window?'warn':'guide-line'}">앞으로 6시간 예상 판매 ${n(state.outlook.next6)}개(평소 ${n(state.outlook.usual6)}개) · 24시간 ${n(state.outlook.next24)}개 · ${state.outlook.weekend?'주말':'평일'} 시간대 기준${state.outlook.low_window?' · 지금부터 수요가 적은 시간대라 주문을 한꺼번에 쌓지 마세요':''}</p>${state.outlook.quiet_hours.length?`<p class="help">앞으로 24시간 중 수요가 평소 절반 아래인 시간: ${esc(hourRanges(state.outlook.quiet_hours))}</p>`:''}`:'<p class="help">시간대별 판매 기록(24시간 시장 이력)이 부족해 앞으로의 예상 판매량을 계산하지 못했습니다.</p>'}<p class="help">세 기간 중 가장 느린 하루 판매량에서 내 몫(설정의 '내 몫', 기본 20%)만 내가 팔 수 있다고 봅니다. 나머지는 다른 판매자 몫입니다. 내 몫 하루치보다 많이 사면 다 팔 때까지 하루 넘게 걸려 그사이 다른 판매자가 더 싸게 올릴 수 있고, 3일치를 넘기면 재고가 오래 묶일 위험이 큽니다. 관측 시간이 짧은 기간(24시간은 6시간 미만, 7일·28일은 24시간 미만)은 계산에서 뺍니다.</p></div>`:'');
}
function renderOverview(){
 const items=Object.keys(state.items);
 $('overview').style.setProperty('--cols',items.length);
 updateHtml('overview',table(['도시',...items.map(itemHead)],Object.entries(state.cities).map(([city,name])=>`<tr><th scope="row">${esc(name)}</th>${items.map(item=>{const b=state.books[item][city];return `<td><button type="button" class="market-cell ${item===selectedItem&&city===selectedCity?'selected':''}" data-item="${esc(item)}" data-city="${esc(city)}" aria-label="${esc(name)} ${esc(state.items[item])} 상세"><span>${extremeMark(item,city,'buy')}${quoteText(b.buy,'buy')}</span><span>${extremeMark(item,city,'sell')}${quoteText(b.sell,'sell')}</span>${personalMark(item,city)}</button></td>`}).join('')}</tr>`),'all-markets'));
}
function render(){
 // Watch rows use city and item names from the state, so they redraw once it arrives.
 if(watchView.data)renderWatches();
 renderNow();renderPickHistory();renderMyOrders();renderStale();renderCapital();renderHoldings();renderPurchases();renderPortfolio();renderStudies();renderBookPatterns();renderBoard();renderBuyouts();renderOverview();renderBuyUnder();renderSellTargets();renderPersonal();renderPersonalMails();renderMailDiagnosis();renderVolumeGuide();
 $('detail-context').textContent=state.cities[selectedCity]+' · '+state.items[selectedItem];
 const books=state.books[selectedItem];
 renderBook('sell',books[selectedCity].sell);renderBook('buy',books[selectedCity].buy);
 updateHtml('history',state.history.map(h=>`<article class="history-card"><h3>최근 ${h.days===1?'24시간':h.days+'일'}</h3><div class="metric">${n(h.amount)} <small>개 · 관측 판매량</small></div><div class="stats"><div><small>수량 가중평균가</small><b>${n(h.weighted_average,2)}</b></div><div><small>최고 구간 평균가</small><b>${n(h.highest_bucket_average,2)}</b></div><div><small>고가 구간 전체 판매량</small><b>${n(h.near_high_amount)}개</b></div><div><small>관측 시간 범위</small><b>${n(h.coverage*100,1)}%</b></div></div><div class="coverage">${n(h.covered_hours)} / ${n(h.requested_hours)}시간 확인 · ${n(h.resolution_hours)}시간 구간<br>최신 구간 끝 ${date(h.newest_bucket_end)}${h.stale?'<br><strong class="warn">최신 구간이 오래됨 · 시장 이력을 다시 열어 주세요.</strong>':''}</div></article>`).join(''));
 renderHigh();renderBars('weekdays',state.patterns.weekdays,x=>['월','화','수','목','금','토','일'][x]);renderBars('hours',state.patterns.hourly,x=>String(x).padStart(2,'0')+'시');$('pattern-note').textContent=state.patterns.note;
 const hidden=settingsWarnings();
 $('candidate-notes').textContent=[...(state.candidate_notes||[]),...hidden].join(' · ');
 $('reset-target').textContent=`대상: ${state.items[selectedItem]} · ${state.cities[selectedCity]} (시장 상세에서 고른 시장)`;
 const ownLeft=(item,city,s)=>(state.my_orders||[]).filter(m=>m.item===item&&m.city===city&&m.side===s).reduce((a,m)=>a+m.remaining,0);
 const style=$('candidate-filter').value,listed=state.candidates.filter(r=>style==='all'||(style==='instant')===(r.mode==='instant'));
 const candidateRow=r=>{const instantBuy=r.mode==='instant'||r.mode==='instant_list',reasons=r.reasons.filter(x=>!hidden.includes(x));return `<tr class="${r.mode==='depth'?'conditional':''}"><td>${tierLabel(state.items[r.item])}<br><span class="pill ${instantBuy?'instant-badge':''}">${esc(r.status)}</span></td><td>${esc(state.cities[r.buy_city])}<br>${n(r.buy_price)}<small>${instantBuy?`바로 살 수 있는 잔량 ${n(r.buy_available)}개`:`기준 잔량 ${n(r.buy_reference?.amount)}개`}</small></td><td>${esc(state.cities[r.sell_city])}<br>${n(r.sell_price)}<small>${r.mode==='instant'?`바로 팔 수 있는 잔량 ${n(r.sell_available)}개`:`기준 잔량 ${n(r.sell_reference?.amount)}개`}</small></td><td class="positive">${n(r.net_per_unit)}<br>${n(r.roi*100,1)}%</td><td>${instantBuy?'':`<b>매수 앞 ${n(r.buy_ahead)}개 / 매도 앞 ${n(r.sell_ahead)}개</b>`}<small>관측 7일 평균 ${n(r.history_weighted_average,2)} · 최고 구간 평균 ${n(r.history_highest_bucket_average,2)} · ${n(r.history_amount)}개</small><small>파는 곳 ${esc(guideLine(guideFor(r.item,r.sell_city)))}</small>${ownLeft(r.item,r.sell_city,'sell')?`<small class="warn">파는 곳에 내 매도 주문 ${n(ownLeft(r.item,r.sell_city,'sell'))}개 남음</small>`:''}${r.mode!=='instant'&&r.mode!=='instant_list'&&ownLeft(r.item,r.buy_city,'buy')?`<small class="warn">사는 곳에 내 매수 주문 ${n(ownLeft(r.item,r.buy_city,'buy'))}개 남음</small>`:''}${reasons.length?`<div class="reason-list">${reasons.map(x=>`<span class="pill reason">${esc(x)}</span>`).join('')}</div>`:''}</td></tr>`};
 const candidateHead=['품목·방식','사는 곳 / 가격','파는 곳 / 가격','개당 예상 순익','물량·확인할 점'],pastCandidates=(state.past_candidates?.rows||[]).filter(r=>style==='all'||(style==='instant')===(r.mode==='instant'));
 const noCandidates=`최근 ${n(state.settings.fresh_minutes)}분 안에 연 시장 기준으로 이익이 남는 후보가 없습니다. 시장을 다시 열면 바로 계산하고, 지난 추천은 기록 탭에 남아 있습니다.`;
 updateHtml('candidates',listed.length?table(candidateHead,listed.map(candidateRow),'candidates'):pastCandidates.length?`<p class="warn past-note">${esc(noCandidates)}</p><h3 class="sub-head">지난 후보 · ${date(state.past_candidates.seen)} (${ago(state.past_candidates.seen)}) 기준</h3><p class="help">그때 계산한 가격·잔량입니다. 지금은 다른 사람이 먼저 거래해 바뀌었을 수 있으니, 거래 전에 게임에서 그 시장을 다시 열어 확인해 주세요.</p>${table(candidateHead,pastCandidates.map(candidateRow),'candidates-past')}`:empty(noCandidates));
 $('mail').textContent=state.email.enabled?`메일 설정: ${state.email.configured?'등록됨':'설정 미완료'} · ${state.email.times.join(', ')}`:'메일 발송 꺼짐 · 수신 주소·발송 계정·프랑스 기준 발송 시간을 설정하면 사용할 수 있습니다.';
}
function renderBook(side,b){
 const mineAt=(s,price)=>(state.my_orders||[]).filter(m=>m.item===selectedItem&&m.city===selectedCity&&m.side===s&&m.price===price).reduce((a,m)=>a+m.remaining,0);
 const levels=b.levels,d=b.depth||{},s=state.settings;
 if(!levels.length){updateHtml(side,empty('조회 자료 없음 · 게임에서 주문 목록을 열어 주세요.'));return}
 const filter=$('book-filter').value;
 const shown=levels.filter(r=>filter==='all'||(filter==='small'&&r.small)||(filter==='large'&&r.large)||(filter==='meaningful'&&!r.small));
 const ref=d.reference,dominant=d.dominant,liveRef=b.analysis_depth?.reference;
 const freshness=b.single_only?'단일 주문 수신 · 이전 목록 보관 · 최우선 가격 재조회 필요':b.awaiting_scan?'새 조회 대기 · 지난 관측 보관':b.stale?'이전 관측 · 분석 제외':b.mixed_freshness?'최신·이전 호가 혼재 · 이전 호가 분석 제외':'유효 시간 내 관측';
 const large=(d.large_levels||[]).map(r=>`${n(r.price)} · ${n(r.amount)}개`).join(' / ');
 const summary=`<div class="book-summary ${b.stale?'stale-summary':''}"><span class="pill">${esc(freshness)}</span>${b.mixed_freshness?`<p class="warn">최신 호가만의 분석 기준 <strong>${n(liveRef?.price)}</strong> · ${n(liveRef?.amount)}개</p>`:''}<p>${side==='sell'?'관측 최저 매도가':'관측 최고 매수가'} <strong>${n(quote(b)?.price)}</strong> · ${n(quote(b)?.amount)}개</p><p>전체 관측의 물량 기준 가격 (최저가와 다를 수 있음) <strong>${n(ref?.price)}</strong> · ${n(ref?.amount)}개</p><p>인근 최대 잔량 <strong>${n(dominant?.price)}</strong> · ${n(dominant?.amount)}개</p><p class="${d.small_dominated?'warn':''}">소량 호가 ${n(d.small_levels)}개 가격대 (${n(d.small_level_fraction*100,1)}%) · 합계 ${n(d.small_amount)}개<br>맨 앞 소량 ${n(d.leading_small_levels)}개 호가 · 합계 ${n(d.leading_small_amount)}개</p><small>대량 주문 가격: ${esc(large||'기준에 맞는 가격대 없음')}</small>${d.distant_large_levels?.length?`<small>먼 가격의 대량 주문 ${d.distant_large_levels.map(r=>`${n(r.price)} · ${n(r.amount)}개`).join(' / ')} · 기준 가격에서 제외</small>`:''}</div>`;
 const rows=shown.map(r=>`<tr class="${r.wall?'wall':r.large?'large':r.small?'small-lot':''} ${r.stale?'stale-row':''}"><td>${n(r.price)} ${r.wall?'<span class="pill wall-badge">물량 벽</span>':r.large?'<span class="pill">대량</span>':r.small?'<span class="pill small-badge">소량</span>':''}${r.outside_band?'<small>인근 범위 밖</small>':''}${mineAt(side,r.price)?`<span class="pill mine-badge">내 주문 ${n(mineAt(side,r.price))}개</span>`:''}</td><td><strong>${n(r.amount)}</strong></td><td>${n(r.cumulative)}</td><td>${n(r.orders)}<small>소량 주문 ${n(r.small_orders)}건</small></td><td>${r.stale?'이전 관측':date(r.oldest_seen)}</td></tr>`);
 updateHtml(side,`<p class="help">${esc(b.coverage)} · ${date(b.seen)}<br>소량 ≤${n(s.small_quantity_max)}개 · 대량 ≥${n(s.large_quantity_min)}개 · 물량 벽 ≥${n(s.wall_quantity_min)}개 또는 주변 중앙 잔량의 ${n(s.wall_multiple)}배</p>${summary}`+(rows.length?table(['가격·구분','주문 잔량','누적 잔량','주문 수','관측 시각'],rows,`${selectedItem}-${selectedCity}-${side}-${filter}`):empty('현재 필터에 맞는 호가가 없습니다.')));
}
function renderHigh(){if(!state)return;const h=state.history[Number($('period').value)],threshold=state.settings.near_high_fraction;$('near-note').textContent=`최고 구간 평균가의 ${n(threshold*100)}% 이상인 구간을 표시합니다. 가격별 실제 체결 수량은 알 수 없습니다.`;updateHtml('high',h.near_high_buckets.length?table(['구간 시작 (프랑스)','구간 끝 (프랑스)','해상도','구간 평균가','구간 전체 판매량'],h.near_high_buckets.map(r=>`<tr><td>${esc(date(r.start))}</td><td>${esc(date(r.end))}</td><td>${r.hours}시간</td><td>${n(r.avg,2)}</td><td>${n(r.amount)}개</td></tr>`),`${selectedItem}-${selectedCity}-high-${$('period').value}`):empty('기간에 맞는 판매 이력이 아직 없습니다.'))}
function renderBars(id,rows,label){
 if(!rows.length){updateHtml(id,empty('1시간 구간 이력이 쌓이면 표시됩니다.'));return}
 const max=Math.max(1,...rows.map(x=>x.per_observed_hour??0));
 updateHtml(id,rows.map(r=>r.observed_hours?`<div class="bar-row"><span>${esc(label(r.key))}</span><div class="bar-track"><div class="bar-fill" style="width:${(r.per_observed_hour??0)/max*100}%"></div></div><small><b>${n(r.total)}개</b>${r.observed_hours}시간 관측 · 시간당 ${n(r.per_observed_hour,1)}</small></div>`:`<div class="bar-row missing"><span>${esc(label(r.key))}</span><div class="bar-track"></div><small>관측 없음</small></div>`).join(''));
}
function fillSettings(){const f=$('settings');if(!$('excluded-cities').children.length)$('excluded-cities').innerHTML=Object.entries(state.cities).map(([id,name])=>`<label class="check"><input type="checkbox" name="excluded_city" value="${esc(id)}">${esc(name)}</label>`).join('');const excluded=draft?.excluded_cities||state.settings.excluded_cities||[];$('item-cities').textContent=Object.entries(state.item_cities||{}).filter(([,cs])=>cs.length).map(([i,cs])=>`${state.items[i]||i}은(는) ${cs.map(c=>state.cities[c]||c).join(', ')}도 포함해 분석합니다 (config.json의 item_cities).`).join(' ');for(const box of $('excluded-cities').querySelectorAll('input'))box.checked=excluded.includes(box.value);for(const [k,v]of Object.entries(state.settings)){if(!f.elements[k])continue;if(typeof v==='boolean')f.elements[k].checked=v;else f.elements[k].value=percentKeys.includes(k)?Math.round(v*10000)/100:v}if(draft){for(const [k,v]of Object.entries(draft)){const el=f.elements[k];if(el&&k!=='excluded_cities'){if(el.type==='checkbox')el.checked=v;else el.value=v}}$('saved').textContent='이전 입력을 복원했습니다.'}}
const korean=v=>v>=1e8?`${n(v/1e8,2)}억`:`${n(v/1e4,1)}만`;
function numberHints(){for(const input of $('settings').querySelectorAll('input[type=number]')){let hint=input.nextElementSibling;if(!hint?.classList.contains('number-hint')){hint=document.createElement('small');hint.className='number-hint';input.after(hint)}const v=Number(input.value);hint.textContent=Number.isFinite(v)&&v>=10000?'= '+korean(v):''}}
function readDraft(){const f=$('settings'),value={};for(const key of [...percentKeys,...numberKeys,...boolKeys]){const el=f.elements[key];value[key]=el.type==='checkbox'?el.checked:el.value}value.excluded_cities=[...$('excluded-cities').querySelectorAll('input:checked')].map(box=>box.value);return value}
function queueDraft(){formVersion++;draft=readDraft();storage.set('settingsDraft',draft);$('saved').textContent='입력 보관됨 · 자동 저장 대기';scheduleSave()}
function scheduleSave(){clearTimeout(saveTimer);saveTimer=setTimeout(saveSettings,700)}
async function post(path,value){
 let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(value)});
 if(r.status===403){const fresh=await fetch(`/api/state?item=${encodeURIComponent(selectedItem)}&city=${encodeURIComponent(selectedCity)}`);if(fresh.ok){token=(await fresh.json()).token;r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(value)})}}
 const data=await r.json();if(!r.ok)throw Error(data.error||r.status);return data;
}
async function saveSettings(){
 if(!ready||!$('upgrade').hidden)return;if(saving){saveQueued=true;return}
 const f=$('settings'),values=readDraft();
 if(!f.checkValidity()||[...percentKeys,...numberKeys].some(k=>values[k]===''||!Number.isFinite(Number(values[k])))){$('saved').textContent='입력은 보관 중 · 빈칸과 입력 범위를 확인해 주세요.';return}
 const payload={};for(const key of percentKeys)payload[key]=Number(values[key])/100;for(const key of numberKeys)payload[key]=Number(values[key]);for(const key of boolKeys)payload[key]=values[key];payload.excluded_cities=values.excluded_cities;
 const revision=formVersion;saving=true;$('saved').textContent='자동 저장 중…';
 try{await post('/api/settings',payload);if(revision===formVersion){draft=null;storage.set('settingsDraft',null);$('saved').textContent='자동 저장 완료 · '+new Intl.DateTimeFormat('ko-KR',{timeZone:'Europe/Paris',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(new Date())}await refresh()}
 catch(e){$('saved').textContent='서버 저장 실패 · 입력은 이 브라우저에 보관됨: '+e.message}
 finally{saving=false;if(saveQueued||revision!==formVersion){saveQueued=false;scheduleSave()}}
}
function chooseView(){selectedItem=$('item').value;selectedCity=$('city').value;viewVersion++;rememberView();refresh()}
$('item').onchange=chooseView;$('city').onchange=chooseView;
$('overview').onclick=e=>{const cell=e.target.closest('[data-item][data-city]');if(cell){$('item').value=cell.dataset.item;$('city').value=cell.dataset.city;chooseView()}};
const tabs=[...document.querySelectorAll('.tabs [data-tab]')];
const movedTabs={cities:'trades',mail:'history'};
function showTab(name){if(name==='cities')setTradesView('city');name=movedTabs[name]||name;if(!tabs.some(b=>b.dataset.tab===name))name='now';for(const b of tabs){const on=b.dataset.tab===name;b.classList.toggle('active',on);b.setAttribute('aria-selected',String(on));if(on){const bar=b.parentElement;bar.scrollLeft=b.offsetLeft-(bar.clientWidth-b.offsetWidth)/2}}for(const panel of document.querySelectorAll('.tab-panel'))panel.hidden=panel.dataset.tab!==name;storage.set('tab',name)}
document.querySelector('.tabs').onclick=e=>{const b=e.target.closest('[data-tab]');if(b)showTab(b.dataset.tab)};
function setTradesView(view){if(!['list','city'].includes(view))view='list';for(const s of document.querySelectorAll('.trades-view'))s.value=view;$('candidate-section').hidden=view!=='list';$('board-section').hidden=view!=='city';storage.set('tradesView',view)}
setTradesView(storage.get('tradesView','list'));
for(const s of document.querySelectorAll('.trades-view'))s.onchange=()=>setTradesView(s.value);
for(const p of document.querySelectorAll('.tab-panel p.help:not([id])')){const html=p.innerHTML,m=/(니다|세요)\.\s/.exec(html);if(p.textContent.length<160||!m)continue;const cut=m.index+m[0].length,lead=document.createElement('p'),more=document.createElement('details'),rest=document.createElement('p');lead.className='help help-lead';lead.innerHTML=html.slice(0,cut);more.className='more-help';more.innerHTML='<summary>설명 더 보기</summary>';rest.className='help';rest.innerHTML=html.slice(cut);more.append(rest);p.replaceWith(lead,more)}
// Long tabs get a row of jump links built from their sections' data-jump labels.
for(const panel of document.querySelectorAll('.tab-panel')){const targets=[...panel.querySelectorAll(':scope > section[data-jump]')];if(targets.length<3)continue;const nav=document.createElement('nav');nav.className='section-nav';nav.setAttribute('aria-label','이 화면 바로가기');nav.innerHTML=targets.map(t=>`<a href="#${t.id}">${esc(t.dataset.jump)}</a>`).join('');nav.onclick=e=>{const a=e.target.closest('a');if(!a)return;e.preventDefault();$(a.getAttribute('href').slice(1)).scrollIntoView({block:'start'})};panel.prepend(nav)}
showTab(storage.get('tab','now'));
$('city-board').onclick=e=>{const button=e.target.closest('[data-city]');if(button){$('city').value=button.dataset.city;chooseView();showTab('market');document.querySelector('.tabs').scrollIntoView({block:'start'})}};
$('candidate-filter').value=['all','instant','wait'].includes(storage.get('candidateFilter','all'))?storage.get('candidateFilter','all'):'all';
$('my-order-form').onsubmit=async e=>{e.preventDefault();const f=e.target;try{await post('/api/my-orders',{item:f.item.value,city:f.city.value,side:f.side.value,price:Number(f.price.value),amount:Number(f.amount.value)});f.price.value='';f.amount.value='';$('my-order-status').textContent='추가했습니다. 게임에서 그 시장을 열면 자동으로 연결됩니다.';await refresh()}catch(err){$('my-order-status').textContent='추가 실패: '+err.message}};
$('my-orders-filters').onclick=e=>{const b=e.target.closest('[data-key]');if(!b||!state)return;storage.set('myOrdersFilter',{...myOrderFilter(state.my_orders||[]),[b.dataset.key]:b.dataset.value});preserveViewport(renderMyOrders)};
$('my-orders').onclick=async e=>{if(e.target.closest('[data-more]')){showAllMyOrders=!showAllMyOrders;preserveViewport(renderMyOrders);return}const b=e.target.closest('[data-close]');if(!b)return;try{await post('/api/my-orders',{action:'close',id:Number(b.dataset.close)});await refresh()}catch(err){$('my-order-status').textContent='목록에서 빼기 실패: '+err.message}};
$('bank-form').onsubmit=async e=>{e.preventDefault();const f=e.target;try{await post('/api/bank',{item:f.item.value,city:f.city.value,amount:Number(f.amount.value)});f.amount.value='';$('bank-status').textContent='은행 수량을 저장했습니다.';await refresh()}catch(err){$('bank-status').textContent='저장 실패: '+err.message}};
$('buy-under-mode').onclick=e=>{const b=e.target.closest('button');if(!b)return;buyMode=b.dataset.mode;storage.set('buyMode',buyMode);$('buy-under-status').textContent='';renderBuyUnder()};
$('buy-under').onclick=e=>{const row=e.target.closest('[data-buy-row]');if(!row||e.target.closest('.bu-target')||String(getSelection()))return;const item=row.dataset.buyRow;if(!buyOpen.delete(item))buyOpen.add(item);preserveViewport(renderBuyUnder)};
$('buy-under').onchange=async e=>{if(!e.target.closest('.buy-target-form'))return;const s=$('buy-under-status');try{await saveBuyTargets();s.textContent='목표가 저장됨';await refresh()}catch(err){s.textContent='저장 실패: '+err.message}};
$('buy-under').onsubmit=e=>{if(!e.target.closest('.buy-target-form'))return;e.preventDefault();document.activeElement?.blur()};
$('sell-target').onchange=async e=>{if(!e.target.closest('.sell-target-form'))return;const s=$('sell-target-status');try{await saveSellTargets();s.textContent='희망 판매가 저장됨';await refresh()}catch(err){s.textContent='저장 실패: '+err.message}};
$('sell-target').onsubmit=e=>{if(!e.target.closest('.sell-target-form'))return;e.preventDefault();document.activeElement?.blur()};
$('holdings').onsubmit=async e=>{const f=e.target.closest('.bank-city-form');if(!f)return;e.preventDefault();try{await post('/api/bank-city',{location:f.dataset.location,city:f.city.value});document.activeElement?.blur();await refresh()}catch(err){const s=$('bank-city-status');if(s)s.textContent='저장 실패: '+err.message}};
$('purchase-form').onsubmit=async e=>{e.preventDefault();const f=e.target;try{await post('/api/purchases',{item:f.item.value,city:f.city.value,kind:f.kind.value,price:Number(f.price.value),amount:Number(f.amount.value),bought:f.bought.value||null});f.price.value='';f.amount.value='';f.bought.value='';$('purchase-status').textContent='매수 기록을 추가했습니다.';await refresh()}catch(err){$('purchase-status').textContent='추가 실패: '+err.message}};
$('purchases').onclick=async e=>{const b=e.target.closest('[data-remove-purchase]');if(!b)return;try{await post('/api/purchases',{action:'remove',id:Number(b.dataset.removePurchase)});await refresh()}catch(err){$('purchase-status').textContent='삭제 실패: '+err.message}};
$('candidate-filter').onchange=()=>{storage.set('candidateFilter',$('candidate-filter').value);if(state)render()};
$('period').onchange=()=>{rememberView();preserveViewport(renderHigh)};
$('book-filter').onchange=()=>{rememberView();if(state)preserveViewport(()=>{renderBook('sell',state.books[selectedItem][selectedCity].sell);renderBook('buy',state.books[selectedItem][selectedCity].buy)})};
$('settings').addEventListener('input',queueDraft);$('settings').addEventListener('input',numberHints);$('settings').addEventListener('change',queueDraft);
$('settings').onsubmit=e=>{e.preventDefault();clearTimeout(saveTimer);saveSettings()};
window.addEventListener('scroll',()=>{if(!restorePending){clearTimeout(scrollTimer);scrollTimer=setTimeout(rememberView,120)}},{passive:true});
window.addEventListener('pagehide',()=>{if(ready){rememberView();if(draft)storage.set('settingsDraft',readDraft())}});
$('reset').onclick=async()=>{const target=`${state.items[selectedItem]} · ${state.cities[selectedCity]}`;if(!window.confirm(`${target} 시장의 지금 호가를 비울까요? 게임에서 그 시장을 다시 열기 전까지 호가와 매매 후보가 빈칸이 됩니다.`))return;try{await post('/api/reset',{item:selectedItem,city:selectedCity});$('reset-status').textContent=`${target} 호가를 비웠습니다 · 게임에서 그 시장의 매수·매도 목록을 다시 열어 주세요.`;await refresh()}catch(e){$('reset-status').textContent='호가 비우기 실패: '+e.message}};
// Any-item lookup: search an item family, then pick tier, enchant and quality like filters; poll that item's city prices while the tab is open.
const lookupView={hit:null,hits:new Map(),timer:null,searchTimer:null,data:null,combos:null,combosAt:0};
const lookupTier=()=>lookupView.hit?.tiers.find(t=>String(t.tier)===$('lookup-tier').value)||lookupView.hit?.tiers[0];
const lookupId=()=>{const h=lookupView.hit;if(!h)return null;const t=lookupTier(),e=Number($('lookup-enchant').value);return (t?.tier!=null?`T${t.tier}_${h.family}`:h.family)+(e?'@'+e:'')};
const lookupAge=v=>v?ago(v):'—';
const lookupName=h=>h.ko||h.en||h.family;
function remember(){const h=lookupView.hit;if(!h)return;storage.set('lookupLast2',{hit:h,tier:$('lookup-tier').value,enchant:$('lookup-enchant').value,quality:$('lookup-quality').value})}
function renderLookupRecent(){const recent=storage.get('lookupRecent2',[]);for(const h of recent)lookupView.hits.set(h.family,h);updateHtml('lookup-recent',recent.length?`<span class="lookup-label">최근 본 품목</span>${recent.map(h=>`<button type="button" class="secondary small-button" data-family="${esc(h.family)}">${tierLabel(lookupName(h))}</button>`).join('')}`:'')}
async function lookupSearch(){
 const q=$('lookup-q').value.trim();if(q.length<2){updateHtml('lookup-hits','');return}
 try{const r=await (await fetch('/api/lookup/search?q='+encodeURIComponent(q))).json();
  for(const h of r.items)lookupView.hits.set(h.family,h);
  const tiers=h=>h.tiers.length>1?`T${h.tiers[0].tier}~T${h.tiers.at(-1).tier}`:h.tiers[0].tier!=null?`T${h.tiers[0].tier}`:'';
  updateHtml('lookup-hits',r.items.length?r.items.map(h=>`<button type="button" class="secondary small-button" data-family="${esc(h.family)}" data-tier="${h.tier??''}">${tierLabel(lookupName(h))}<small>${esc(tiers(h))} · ${esc(h.family)}</small></button>`).join(''):`<p class="help">${r.loading?'아이템 이름 목록을 처음 받는 중입니다(약 24MB, 한 번만). 잠시 뒤 다시 입력해 주세요.':r.error?'이름 목록을 받지 못했습니다: '+esc(r.error):'맞는 품목이 없습니다.'}</p>`);
  if(r.loading)setTimeout(lookupSearch,3000)}catch(e){updateHtml('lookup-hits',`<p class="warn">검색 실패: ${esc(e.message)}</p>`)}}
// Tier choices come from the family; enchant choices from the chosen tier (T1~T3 have none).
function fillTiers(h,tier){const sel=$('lookup-tier');sel.innerHTML=h.tiers.map(t=>`<option value="${t.tier??''}">${t.tier!=null?'T'+t.tier:'—'}</option>`).join('');sel.disabled=h.tiers.length<2;if(h.tiers.some(t=>String(t.tier)===String(tier)))sel.value=String(tier);fillEnchants()}
function fillEnchants(){const have=lookupTier()?.enchants||[0];for(const o of $('lookup-enchant').options)o.disabled=!have.includes(Number(o.value));if($('lookup-enchant').selectedOptions[0]?.disabled)$('lookup-enchant').value='0'}
function chooseFamily(h,tier){
 lookupView.hit=h;lookupView.data=null;
 fillTiers(h,tier!==''&&tier!=null?tier:$('lookup-tier').value);
 storage.set('lookupRecent2',[h,...storage.get('lookupRecent2',[]).filter(r=>r.family!==h.family)].slice(0,8));
 remember();renderLookupRecent();updateHtml('lookup-hits','');loadLookup()}
async function loadCombos(){
 const h=lookupView.hit;if(!h||h.tiers.reduce((a,t)=>a+t.enchants.length,0)<2){updateHtml('lookup-combos','');return}
 const same=lookupView.combos?.family===h.family;if(same&&!lookupView.combos.public_loading&&Date.now()-lookupView.combosAt<60000){renderCombos();return}
 try{const r=await fetch('/api/lookup/family?family='+encodeURIComponent(h.family)),data=await r.json();if(!r.ok||h!==lookupView.hit)return;lookupView.combos=data;lookupView.combosAt=Date.now();renderCombos()}catch{}}
// Tier rows by enchant columns: where a sell order exists, its cheapest price and how many cities list it.
function renderCombos(){
 const d=lookupView.combos,h=lookupView.hit;if(!d||!h||d.family!==h.family){updateHtml('lookup-combos','');return}
 const at=new Map(d.combos.map(c=>[`${c.tier}/${c.enchant}`,c])),tiers=h.tiers.map(t=>t.tier),cur=lookupTier()?.tier,ce=Number($('lookup-enchant').value);
 const any=d.combos.some(c=>c.cities);
 const cell=(t,e)=>{const c=at.get(`${t}/${e}`);if(!c)return '<td class="cb-none"></td>';const on=String(t)===String(cur)&&e===ce;return `<td><button type="button" class="combo e${e} ${c.cities?'has':'empty'} ${on?'on':''}" data-combo-tier="${t??''}" data-combo-enchant="${e}" title="${c.cities?`${esc(state.cities[c.city]||c.city)} 최저 · ${c.cities}개 도시`:'최근 7일 매물 없음'}">${c.cities?`<b>${n(c.cheapest)}</b><small>${c.cities}곳</small>`:'—'}${c.average?`<small class="cb-avg">${c.average_days===7?'평균':'30일'} ${n(c.average)}</small>`:''}</button></td>`};
 updateHtml('lookup-combos',`<div class="combo-head"><span class="lookup-label">값이 있는 조합</span><small>${d.public_loading?'공개 서버에 묻는 중…':any?'칸을 누르면 그 티어·인챈트로 바뀝니다 · 큰 글자는 최근 7일 최저 매도, 평균은 공유 자료의 최근 7일 평균 체결가(거래가 없으면 30일, 아무 품질)':'이 품목은 최근 7일 동안 6개 도시 어디에도 매물이 없습니다.'}</small></div>`+table(['티어',...[0,1,2,3,4].map(e=>({html:`<span class="ench-tag e${e}">.${e}</span>`}))],tiers.map(t=>`<tr><th scope="row">${t!=null?tierSpan(t):'—'}</th>${[0,1,2,3,4].map(e=>cell(t,e)).join('')}</tr>`),'lookup-combos'))}
async function loadLookup(){
 clearTimeout(lookupView.timer);const id=lookupId();if(!id)return;loadCombos();
 try{const quality=$('lookup-quality').value,r=await fetch(`/api/lookup/item?id=${encodeURIComponent(id)}&quality=${quality}`),data=await r.json();if(!r.ok)throw Error(data.error||r.status);if(id!==lookupId()||quality!==$('lookup-quality').value)return;lookupView.data=data;renderLookup()}
 catch(e){updateHtml('lookup-view',`<p class="warn">시세를 못 불러왔습니다: ${esc(e.message)}</p>`)}
 // Poll faster while the public answer is still on its way.
 if(!document.querySelector('.tab-panel[data-tab=lookup]').hidden)lookupView.timer=setTimeout(loadLookup,lookupView.data?.public_loading?2500:15000)}
function renderLookup(){
 const d=lookupView.data;if(!d)return;
 const s=d.stream||{};$('lookup-status').textContent=s.stream_connected?`실시간 공유 받는 중 · 마지막 ${lookupAge(s.stream_last)} · 누적 ${n(s.stream_count)}건`:s.stream_error?`실시간 공유 연결 안 됨 · ${s.stream_error}`:'실시간 공유 꺼짐 · 분석 프로그램을 다시 시작하면 켜집니다';
 const src={me:'내가 연 시장',stream:'실시간 공유'},anyQuality=!Number(d.quality),qualityName=['','보통','좋음','뛰어남','훌륭함','걸작'];
 // The newer of what we heard and what the public server says wins; both stay visible.
 const old=v=>!v||Date.now()-Date.parse(v)>864e5;
 const pick=(local,pub,side)=>{const l=local?.[side],pubSeen=pub?.[side+'_seen'];const q=l&&(!pubSeen||Date.parse(l.seen)>=Date.parse(pubSeen))?{price:l.price,amount:l.amount,units:l.units,seen:l.seen,quality:l.quality,source:l.mine?'me':'stream'}:pub?.[side]?{price:pub[side],seen:pubSeen,quality:pub[side+'_quality'],source:'public'}:null;if(q)q.old=old(q.seen);return q};
 const cell=(q,side)=>q?`<b class="lk-price ${side} ${q.old?'lk-old':''}">${n(q.price)}</b>${anyQuality&&q.quality?`<small class="lk-quality">${qualityLabel(q.quality)}</small>`:''}${q.amount!=null?`<small>${n(q.amount)}개${q.units>q.amount?` · 전체 ${n(q.units)}개`:''}</small>`:''}<small class="lk-src ${q.source}" title="${esc(date(q.seen))}">${q.source==='public'?'공개 서버':src[q.source]} · ${lookupAge(q.seen)}${q.old?' · 오래된 값':''}</small>`:'<small>자료 없음</small>';
 const rows=d.cities.map(c=>({c,sell:pick(c.local,c.public,'sell'),buy:pick(c.local,c.public,'buy'),h:c.history}));
 const cheapest=Math.min(...rows.map(r=>r.sell&&!r.sell.old?r.sell.price:Infinity)),dearest=Math.max(...rows.map(r=>r.buy&&!r.buy.old?r.buy.price:-Infinity));
 const t=lookupTier(),e=Number($('lookup-enchant').value);
 const tag=t?.tier!=null?`T${t.tier}.${e}`:e?'.'+e:'';
 const none=rows.every(r=>!r.sell&&!r.buy)&&!d.public_loading;
 updateHtml('lookup-view',`<div class="lk-hero"><span class="lk-name">${tierLabel(lookupName(lookupView.hit))}</span>${tag?`<span class="lk-chip">${tierLabel(tag)}</span>`:''}<span class="lk-chip q">${anyQuality?'아무 품질':qualityLabel(d.quality)}</span><code>${esc(d.item)}</code><button type="button" class="secondary small-button" data-watch-add>시세 감시에 추가</button></div>`+(none?`<p class="warn">이 조합은 6개 도시 어디에도 최근 매물·매수 기록이 없습니다.${anyQuality?' 위 「값이 있는 조합」 표에서 금색 칸을 누르면 매물이 있는 티어·인챈트로 바뀝니다.':' 품질을 \'아무 품질\'로 바꿔 보세요. 장비는 한 품질에만 매물이 있는 경우가 많습니다.'}</p>`:'')+table(['도시','최저 매도 (바로 사기)','최고 매수 (바로 팔기)','7일 평균 체결가','하루 판매량 (7일)','마지막 체결일'],rows.map(({c,sell,buy,h})=>`<tr class="${sell&&!sell.old&&sell.price===cheapest?'lk-cheapest':''}"><td>${esc(state.cities[c.city]||c.city)}${sell&&!sell.old&&sell.price===cheapest?'<small class="positive">가장 쌈</small>':''}${buy&&!buy.old&&buy.price===dearest?'<small class="positive">가장 비싸게 사 줌</small>':''}</td><td>${cell(sell,'sell')}</td><td>${cell(buy,'buy')}</td><td>${h?.average!=null?n(h.average):'—'}${h?.all_qualities?'<small>모든 품질 합계</small>':''}</td><td>${h?n(h.per_day,1)+'개':'—'}</td><td>${h?`${n(h.last)}<small>${n(h.last_count)}개 · ${lookupAge(h.last_day)}</small>`:'—'}</td></tr>`),'lookup')
  +`<p class="help">${d.public_loading?'공개 서버에 묻는 중… ':d.public_error?'공개 서버 조회 실패: '+esc(d.public_error)+' · ':d.public_fetched?`공개 서버 값은 ${lookupAge(d.public_fetched)}에 받음 · `:''}실시간 공유·내가 연 시장 값은 최근 24시간 안에 들은 주문입니다. 그사이 팔렸거나 새 매물이 올라왔을 수 있으니 사고팔기 전에는 게임에서 한 번 확인하세요. 7일 평균·판매량은 공개 서버의 하루 단위 거래 이력입니다.</p>`)}
$('lookup-form').onsubmit=e=>{e.preventDefault();lookupSearch()};
$('lookup-q').oninput=()=>{clearTimeout(lookupView.searchTimer);lookupView.searchTimer=setTimeout(lookupSearch,250)};
$('lookup-tier').onchange=()=>{fillEnchants();remember();loadLookup()};
for(const id of ['lookup-enchant','lookup-quality'])$(id).onchange=()=>{remember();loadLookup()};
$('lookup-section').addEventListener('click',e=>{const c=e.target.closest('[data-combo-enchant]');if(c){$('lookup-tier').value=c.dataset.comboTier;fillEnchants();$('lookup-enchant').value=c.dataset.comboEnchant;remember();loadLookup();return}const b=e.target.closest('[data-family]');const h=b&&lookupView.hits.get(b.dataset.family);if(h)chooseFamily(h,b.dataset.tier)});
{renderLookupRecent();const last=storage.get('lookupLast2',null);if(last?.hit){lookupView.hit=last.hit;fillTiers(last.hit,last.tier);$('lookup-enchant').value=last.enchant||'0';fillEnchants();$('lookup-quality').value=last.quality??'0'}}
document.querySelector('.tabs').addEventListener('click',e=>{if(e.target.closest('[data-tab=lookup]'))loadLookup()});
if(storage.get('tab','now')==='lookup')loadLookup();
// Price watch: the cheapest sell and dearest buy of chosen items, refreshed every 10 seconds even in the background,
// with an alert once when a buy or sell price is reached on a value from the last hour (re-armed when it stops holding).
const watchView={data:null,prev:{},flash:{},armed:storage.get('watchArmed',{}),timer:null,audio:null};
const watchAge=m=>m==null?'—':m<1?'방금':m<60?`${m}분 전`:`${Math.round(m/60)}시간 전`;
// Tracked items keep the names used everywhere else in the app.
const watchName=w=>state?.items?.[w.item]?itemName(w.item):w.name;
const watchSrc={me:'내가 연 시장',stream:'실시간 공유',public:'공개 서버'};
function watchTag(item,quality){const [base,ench]=item.split('@'),t=/^T(\d)_/.exec(base);return `${t?'T'+t[1]+'.'+(ench||0):ench?'.'+ench:''}${Number(quality)?' · 품질 '+quality:''}`}
function watchTagHtml(item,quality){const [base,ench]=item.split('@'),t=/^T(\d)_/.exec(base);return `${t?tierSpan(t[1],ench||0):ench?`<span class="ench-tag e${ench}">.${ench}</span>`:''}${Number(quality)?' '+qualityLabel(quality):''}`}
async function postWatch(body){try{watchView.data=await post('/api/watches',body);renderWatches()}catch(e){$('watch-status').textContent='저장 실패: '+e.message}}
async function loadWatches(){
 clearTimeout(watchView.timer);
 try{const r=await fetch('/api/watches');if(r.ok){watchView.data=await r.json();renderWatches();checkWatchAlerts()}}catch{}
 watchView.timer=setTimeout(loadWatches,10000)}
function renderWatches(){
 const d=watchView.data;if(!d)return;
 const list=d.watches,hits=list.filter(w=>(w.buy_hit&&w.buy_live)||(w.order_hit&&w.order_live)||(w.sell_hit&&w.sell_live)).length;
 $('watch-badge').hidden=!hits;$('watch-badge').textContent=hits;
 const s=d.stream||{};$('watch-status').textContent=`${list.length}개 감시 중 · 10초마다 갱신${s.stream_connected?' · 실시간 공유 받는 중':''}`;
 const watched=new Set(list.map(w=>w.item));
 updateHtml('watch-quick',Object.keys(state?.items||{}).filter(i=>!watched.has(i)).map(i=>`<button type="button" class="secondary small-button" data-watch-quick="${esc(i)}">+ ${esc(itemName(i))}</button>`).join('')||'<small>추적 품목은 모두 감시 중입니다.</small>');
 // While a price box is being typed in, the table waits so the cursor is not lost.
 if(document.activeElement?.closest('#watch-list'))return;
 const now=Date.now();
 for(const w of list)for(const side of ['sell','order','list','buy']){const key=w.id+side,p=w[side]?.price??null;if(key in watchView.prev&&watchView.prev[key]!==p)watchView.flash[key]=now+2500;watchView.prev[key]=p}
 const man=v=>v>=10000?n(v/10000,v>=1000000?0:1)+'만':n(v);
 const cell=(w,side)=>{const q=w[side],key=w.id+side;if(!q)return `<td><small>${w.public_loading?'묻는 중…':'최근 하루 값 없음'}</small></td>`;const hit=side==='sell'?w.buy_hit:side==='order'?w.order_hit:side==='list'&&w.sell_hit;return `<td class="${watchView.flash[key]>now?'flash':''} ${hit?'hit':''}"><b class="lk-price ${side==='buy'||side==='order'?'buy':'sell'}">${n(q.price)}</b><small>${esc(state?.cities?.[q.city]||q.city)}${q.amount!=null?' · '+n(q.amount)+'개':''}</small>${!Number(w.quality)&&q.quality?`<small>${qualityLabel(q.quality)}</small>`:''}<small class="lk-src ${q.source}">${watchSrc[q.source]} · ${watchAge(q.age_minutes)}</small></td>`};
 const skippedNote=w=>{const s=w.list_skipped||{},parts=[s.own&&`내 매물이 최저인 ${s.own}곳`,s.outlier&&`튀는 값 ${s.outlier}곳`].filter(Boolean);return parts.length?`<small class="st-meta">${parts.join(' · ')} 뺌</small>`:''};
 const orderNote=w=>{const o=w.order;if(!o)return '';const s=w.order_skipped||{},parts=[s.own&&`내 매수가 1등인 ${s.own}곳`,s.outlier&&`튀는 값 ${s.outlier}곳`].filter(Boolean);return `<small>최고 매수 ${n(o.best)} 위로${o.per_day?` · 매수로 하루 ${n(o.per_day)}개 거래`:' · 최근 거래 기록 없음'}</small>${parts.length?`<small class="st-meta">${parts.join(' · ')} 뺌</small>`:''}`};
 const signed=v=>(v>0?'+':'')+n(Math.round(v));
 const cityUnits=o=>Object.entries(o).sort((a,b)=>b[1]-a[1]).map(([c,u])=>`${esc(state?.cities?.[c]||c)} ${n(u)}개`).join(' · ');
 const depthNote=w=>(w.depth||[]).filter((d,i,all)=>!i||!all[i-1].short).map(d=>`<small class="watch-depth"><b>${d.short?`확인된 ${man(d.spent)}어치`:`${man(d.budget)}어치`} 평균 ${n(d.average)}</b>${cityUnits(d.cities)}</small>`).join('');
 const pct=v=>(v>0?'+':'')+n(v*100,1)+'%';
 const salesBox=f=>{const s=f.sales;if(!s)return '';
  const tile=(label,value,note)=>`<div class="fc-tile"><span>${label}</span><b>${value}</b>${note?`<small>${note}</small>`:''}</div>`;
  const top=Math.max(0,...s.by_hour.map(v=>v||0)),busy=s.by_hour.map((v,i)=>[v||0,i]).sort((a,b)=>b[0]-a[0]).slice(0,3).map(p=>p[1]);
  const tiles=[tile('하루 평균',f.per_day?`${n(f.per_day)}개`:'—',f.per_day?man(f.per_day*f.list_price)+'어치':''),s.week_units?tile('주간',`${n(s.week_units)}개`,`기록 ${s.week_days}일`):'',s.month?tile('월간',`${n(s.month.units)}개`,`기록 ${s.month.days}일 · 평균 ${n(s.month.average)}`):'',s.per_hour?tile('시간당 평균',`${n(s.per_hour)}개`,`기록 ${s.hours_seen}시간 · 붐비는 때 ${busy.slice().sort((a,b)=>a-b).map(h=>h+'시').join(' ')}`):''].join('');
  const bars=top?`<div class="fc-hours" role="img" aria-label="시간대별 평균 거래량">${s.by_hour.map((v,i)=>`<i class="${busy.includes(i)?'busy':''}" style="height:${v?Math.max(4,v/top*100):0}%" title="${i}시 평균 ${v==null?'기록 없음':n(v)+'개'}"><em>${i%6?'':i}</em></i>`).join('')}</div>`:'';
  const money=(spent,profit,roi)=>`<td>${spent?man(spent):'—'}</td><td>${spent?`<b class="${profit>0?'up':'down'}">${signed(profit)}</b> <small>${pct(roi)}</small>`:'<small>이익 없음</small>'}</td>`;
  const rows=s.levels.map((l,i)=>`<tr><td><b>${n(l.price)}</b>${i?` <small>${pct(l.price/f.list_price-1)}</small>`:' <small>지금 값</small>'}</td><td>${l.hours}시간 <small>/ ${s.hours_seen}시간 · ${l.days}일</small></td><td>${l.per_day==null?'—':n(l.per_day)+'개'} <small>거래의 ${n(l.share*100)}%</small></td><td>${l.sells?`${n(l.sells)}개 <small>${man(l.sells*l.price)}어치</small>`:'—'}</td>${money(l.invested,l.profit,l.roi)}${money(l.order_invested,l.order_profit,l.order_roi)}</tr>`).join('');
  const levels=rows?`<div class="fc-levels-wrap"><table class="fc-levels"><thead><tr><th>걸 값</th><th>그 값 이상 거래된 시간</th><th>그 값에 하루 거래</th><th>내 몫 7일 판매</th><th>바로 사서 드는 돈</th><th>바로 사기 순이익</th><th>매수 걸어 드는 돈</th><th>매수 걸기 순이익</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<small>시간별 거래 기록이 아직 모자라 값별 비교는 없습니다.</small>';
  return `<div class="fc-sales"><div class="fc-tiles">${tiles}${bars}</div>${levels}</div>`};
 const forecast=w=>{const f=w.forecast;if(!f)return '';const cityName=esc(state?.cities?.[f.list_city]||f.list_city);
  const noneAtPrice=f.share_from_hours&&!f.sell_order_share;
  const salePill=days=>noneAtPrice?'<span class="pill fc-bad">최근 7일 이 값 이상 거래된 시간 없음 · 안 팔릴 수 있음</span>':days==null?(w.public_loading?'<span class="pill">거래량 묻는 중…</span>':w.public_error?'<span class="pill">거래량을 못 읽음</span>':'<span class="pill fc-slow">이 도시 최근 7일 거래 기록 없음 · 팔릴지 모름</span>'):days<1?'<span class="pill fc-ok">하루 안에 다 팔림</span>':days<=7?`<span class="pill ${days<=3?'fc-ok':'fc-slow'}">약 ${n(days,1)}일이면 다 팔림</span>`:`<span class="pill fc-bad">다 팔기 어려움 · 약 ${n(days,0)}일</span>`;
  const gains=r=>`<b class="fc-roi ${r.roi>0?'up':'down'}">수익률 ${pct(r.roi)}</b> <b class="fc-profit">순이익 ${signed(r.profit)}</b>`;
  const way=(name,body)=>`<div class="fc-way"><b class="fc-way-name">${name}</b> ${body}</div>`;
  const instant=()=>{if(!f.units)return way('바로 사기',`<span class="pill fc-bad">수익 없음</span> <span>수수료를 빼면 남는 값이 없습니다. 가장 싸게 사도 수익률 ${pct(f.best_roi)}</span>`);
   const notes=[f.my_per_day?`내 몫 하루 ${n(f.my_per_day)}개 (이 도시 하루 ${n(f.per_day)}개 거래 × ${f.share_from_hours?'이 값 이상 거래':'매도 주문'} ${n(f.sell_order_share*100)}% × 내 몫 ${n(f.cycle_share*100)}%)`:'',f.fit?`7일에 다 팔 만큼만 사면 ${man(f.fit.invested)} 투자 · 순이익 ${signed(f.fit.profit)}`:'',!f.depth_known?'매물 수량을 몰라 가장 싼 값으로만 계산':f.invested<f.budget*.95?`이익 나는 매물이 확인된 만큼만 계산 (${man(f.invested)})`:'',f.average_roi!=null?`최근 7일 평균 체결가 ${n(f.average)}에 팔리면 수익률 ${pct(f.average_roi)}`:'',f.sources?.me?`내가 연 시장 매물 ${n(f.sources.me)}개 · 공유 매물 ${n(f.sources.stream||0)}개`:'',f.sales_source==='me'?'거래량은 내 PC 기록':'',f.age_minutes>60?`${watchAge(f.age_minutes)} 값이 섞인 계산`:''].filter(Boolean);
   return way('바로 사기',`${gains(f)} ${salePill(f.days)}<small>${n(f.units)}개를 평균 ${n(f.invested/f.units)}에 사서(${cityUnits(f.cities)}) ${cityName}에 ${n(f.list_price)}로 걸기${notes.length?' · '+notes.join(' · '):''}</small>`)};
  const placed=()=>{const o=f.order;
   if(!o?.known)return way('매수 걸어 사기','<span class="pill fc-slow">최근 하루 매수 값 없음 · 걸 값을 모름</span>');
   if(!o.units)return way('매수 걸어 사기',o.best_roi>0?(w.public_loading?'<span class="pill">거래량 묻는 중…</span>':'<span class="pill fc-slow">최근 7일 거래 기록 없음 · 채워질지 모름</span>'):`<span class="pill fc-bad">수익 없음</span> <span>수수료를 빼면 남는 값이 없습니다. 가장 싸게 걸어도 수익률 ${pct(o.best_roi)}</span>`);
   const where=Object.entries(o.cities).sort((a,b)=>b[1]-a[1]).map(([c,u])=>`${esc(state?.cities?.[c]||c)} ${n(o.prices[c])}에 ${n(u)}개`).join(' · ');
   const notes=[`7일 안에 채워질 어림 수량 (그 도시 매수로 팔린 거래 × 내 몫 ${n(f.cycle_share*100)}%)`,o.invested<f.budget*.95?`${man(f.budget)}까지 못 채워 ${man(o.invested)}만 계산`:'',o.fit?`7일에 다 팔 만큼만 걸면 ${man(o.fit.invested)} 투자 · 순이익 ${signed(o.fit.profit)}`:'',o.average_roi!=null?`최근 7일 평균 체결가 ${n(f.average)}에 팔리면 수익률 ${pct(o.average_roi)}`:'',o.age_minutes>60?`${watchAge(o.age_minutes)} 값이 섞인 계산`:''].filter(Boolean);
   return way('매수 걸어 사기',`${gains(o)} ${salePill(o.days)}<small>주문 수수료 포함 평균 ${n(o.invested/o.units)}에 ${n(o.units)}개를 매수 걸어(${where}) ${cityName}에 ${n(f.list_price)}로 걸기 · ${notes.join(' · ')}</small>`)};
  return `<tr class="watch-forecast"><td colspan="10"><b>${man(f.budget)} 투자 예측</b>${instant()}${placed()}${salesBox(f)}</td></tr>`};
 const state_=w=>{const out=[];if(w.buy_hit)out.push(w.buy_live?'<span class="pill watch-hit buy">바로 살 때 됨</span>':`<span class="pill">바로 살 값 충족 · ${watchAge(w.sell.age_minutes)} 값</span>`);if(w.order_hit)out.push(w.order_live?'<span class="pill watch-hit buy">매수 걸 때 됨</span>':`<span class="pill">매수 걸 값 충족 · ${watchAge(w.order.age_minutes)} 값</span>`);if(w.sell_hit)out.push(w.sell_live?'<span class="pill watch-hit sell">팔 때 됨</span>':`<span class="pill">팔 가격 충족 · ${watchAge(w.list.age_minutes)} 값</span>`);return out.join(' ')||'<small>대기 중</small>'};
 updateHtml('watch-list',list.length?table(['품목','바로 사기 (가장 싼 도시)','매수 걸어 사기 (가장 싸게 걸 도시)','걸어 팔기 (가장 비싸게 걸 수 있는 도시)','바로 팔기 (최고 매수)','바로 사기 알림 (이 값 이하)','매수 걸기 알림 (걸 값 이하)','팔 때 알림 (걸어 팔 값 이상)','상태',''],list.map(w=>`<tr data-watch="${w.id}"><td>${tierLabel(watchName(w))}<small>${watchTagHtml(w.item,w.quality)}</small></td>${cell(w,'sell').replace('</td>',depthNote(w)+'</td>')}${cell(w,'order').replace('</td>',orderNote(w)+'</td>')}${cell(w,'list').replace('</td>',skippedNote(w)+'</td>')}${cell(w,'buy')}<td><input class="watch-price" type="number" min="1" step="1" inputmode="numeric" data-watch-id="${w.id}" data-side="buy_below" value="${w.buy_below??''}" placeholder="예: ${w.sell?n(Math.round(w.sell.price*.95)).replace(/,/g,''):''}" aria-label="${esc(w.name)} 바로 사기 알림 가격"></td><td><input class="watch-price" type="number" min="1" step="1" inputmode="numeric" data-watch-id="${w.id}" data-side="order_below" value="${w.order_below??''}" placeholder="예: ${w.order?n(Math.round(w.order.price*.95)).replace(/,/g,''):''}" aria-label="${esc(w.name)} 매수 걸기 알림 가격"></td><td><input class="watch-price" type="number" min="1" step="1" inputmode="numeric" data-watch-id="${w.id}" data-side="sell_above" value="${w.sell_above??''}" placeholder="예: ${w.list?n(Math.round(w.list.price*1.05)).replace(/,/g,''):''}" aria-label="${esc(w.name)} 팔 때 알림 가격"></td><td>${state_(w)}</td><td><button type="button" class="icon-button" data-watch-remove="${w.id}" title="감시에서 빼기" aria-label="${esc(w.name)} 감시에서 빼기">×</button></td></tr>${forecast(w)}`),'watch'):empty('감시 중인 품목이 없습니다. 위 빠른 추가를 누르거나, 품목 시세에서 품목을 고른 뒤 \'감시에 추가\'를 누르세요.'))}
const watchSoundOn=()=>storage.get('watchSound',true),watchDesktopOn=()=>storage.get('watchDesktop',true);
function chime(){if(!watchSoundOn())return;try{const a=watchView.audio??=new AudioContext(),t=a.currentTime;for(const [f,d] of [[880,0],[1318.5,.12],[1760,.24]]){const o=a.createOscillator(),g=a.createGain();o.type='triangle';o.frequency.value=f;g.gain.setValueAtTime(0,t+d);g.gain.linearRampToValueAtTime(.18,t+d+.02);g.gain.exponentialRampToValueAtTime(.001,t+d+.5);o.connect(g).connect(a.destination);o.start(t+d);o.stop(t+d+.55)}}catch{}}
function watchToast(title,body){const box=document.createElement('div');box.className='watch-toast';box.innerHTML=`<b>${esc(title)}</b><span>${esc(body)}</span><button type="button" class="icon-button" aria-label="알림 닫기">×</button>`;box.querySelector('button').onclick=()=>box.remove();$('watch-toasts').append(box);setTimeout(()=>box.remove(),60000)}
function checkWatchAlerts(){
 for(const w of watchView.data.watches)for(const [kind,hit,live,q,target,word] of [['buy',w.buy_hit,w.buy_live,w.sell,w.buy_below,'바로 살 때'],['order',w.order_hit,w.order_live,w.order,w.order_below,'매수 걸 때'],['sell',w.sell_hit,w.sell_live,w.list,w.sell_above,'팔 때']]){
  const key=w.id+':'+kind+':'+target;
  if(!(hit&&live)){delete watchView.armed[key];continue}
  if(watchView.armed[key])continue;
  watchView.armed[key]=true;
  const title=`${word}: ${watchName(w)} ${watchTag(w.item,w.quality)}`,body=`${state?.cities?.[q.city]||q.city} ${{buy:'최저 매도',order:'걸 값',sell:'걸어 팔 값'}[kind]} ${n(q.price)} (알림 ${kind==='sell'?'≥':'≤'} ${n(target)}) · ${watchSrc[q.source]} ${watchAge(q.age_minutes)}`;
  watchToast(title,body);chime();
  if('Notification' in window&&Notification.permission==='granted'&&watchDesktopOn())try{new Notification(title,{body,tag:key,silent:!watchSoundOn()})}catch{}}
 storage.set('watchArmed',watchView.armed)}
function notifyLabel(){const p='Notification' in window?Notification.permission:'denied';$('watch-notify').textContent=p==='granted'?(watchDesktopOn()?'데스크톱 알림 켜짐 (누르면 끔)':'데스크톱 알림 꺼짐 (누르면 켬)'):p==='denied'?'데스크톱 알림 막힘 (브라우저 설정)':'데스크톱 알림 켜기';$('watch-notify').disabled=p==='denied'}
$('watch-notify').onclick=async()=>{if(!('Notification' in window))return;if(Notification.permission==='granted')storage.set('watchDesktop',!watchDesktopOn());else if(await Notification.requestPermission()==='granted')storage.set('watchDesktop',true);notifyLabel()};
// Saved so a reload or a new build keeps them off, and shared with other open windows, which sound the alert too.
$('watch-sound').checked=watchSoundOn();
$('watch-sound').onchange=e=>storage.set('watchSound',e.target.checked);
window.addEventListener('storage',e=>{if(e.key==='albion.watchSound')$('watch-sound').checked=watchSoundOn();if(e.key==='albion.watchDesktop')notifyLabel()});
$('watch-section').addEventListener('click',e=>{const q=e.target.closest('[data-watch-quick]');if(q)return postWatch({action:'add',item:q.dataset.watchQuick,quality:0});const r=e.target.closest('[data-watch-remove]');if(r)postWatch({action:'remove',id:Number(r.dataset.watchRemove)})});
$('watch-list').addEventListener('change',e=>{const i=e.target.closest('.watch-price');if(!i)return;const row=i.closest('tr'),val=s=>{const v=row.querySelector(`[data-side=${s}]`).value.trim();return v===''?null:Number(v)};postWatch({action:'update',id:Number(i.dataset.watchId),buy_below:val('buy_below'),order_below:val('order_below'),sell_above:val('sell_above')})});
$('watch-list').addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.closest('.watch-price'))e.target.blur()});
$('lookup-section').addEventListener('click',e=>{if(!e.target.closest('[data-watch-add]'))return;const id=lookupId();if(id)postWatch({action:'add',item:id,quality:Number($('lookup-quality').value)}).then(()=>{$('lookup-status').textContent='시세 감시에 추가했습니다.'})});
// Browsers start sound only after a click on the page.
document.addEventListener('click',()=>{try{watchView.audio??=new AudioContext();watchView.audio.resume()}catch{}},{once:true});
notifyLabel();loadWatches();
// A hidden tab does not poll; the server rebuilds every market on each request.
refresh();setInterval(()=>{if(document.hidden)return;refresh();if(draft&&!saving)scheduleSave()},5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
document.addEventListener('focusout',()=>setTimeout(()=>{if(ready&&!editing())refresh()},0));

// Claude chat tab. The answer runs on the server, so this only polls it; a reload picks it back up.
const chatView={id:storage.get('chatConversation',null),data:null,timer:null,seen:''};
const chatOpen=()=>!document.querySelector('.tab-panel[data-tab=chat]').hidden;
function chatInline(text){return text.replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>')}
// Input is escaped first, so the markup added here is the only markup in the result.
function chatMarkdown(raw){
 const out=[];let table=null,list=null,para=[];
 const flush=()=>{if(para.length){out.push(`<p>${para.join('<br>')}</p>`);para=[]}if(list){out.push(`<${list.tag}>${list.rows.map(r=>`<li>${r}</li>`).join('')}</${list.tag}>`);list=null}if(table){const[head,...rows]=table;out.push(`<div class="table-wrap"><table><thead><tr>${head.map(c=>`<th>${c}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);table=null}};
 for(const line of esc(raw).split('\n')){
  const t=line.trim();let m;
  if(/^\|.*\|$/.test(t)){if(!table)flush();if(/^\|[\s:|-]+\|$/.test(t))continue;(table??=[]).push(t.slice(1,-1).split('|').map(c=>chatInline(c.trim())));continue}
  if(table)flush();
  if(!t){flush();continue}
  if(m=/^#{1,6}\s+(.*)$/.exec(t)){flush();out.push(`<h4>${chatInline(m[1])}</h4>`);continue}
  if(m=/^([-*]|\d+\.)\s+(.*)$/.exec(t)){const tag=/\d/.test(m[1])?'ol':'ul';if(para.length||(list&&list.tag!==tag))flush();(list??={tag,rows:[]}).rows.push(chatInline(m[2]));continue}
  if(list)flush();para.push(chatInline(t));
 }
 flush();return out.join('');
}
function renderChat(){
 const box=$('chat-log'),d=chatView.data,atEnd=box.scrollHeight-box.scrollTop-box.clientHeight<80;
 const rows=(d?.messages||[]).map(m=>`<div class="chat-msg ${m.role==='user'?'chat-user':'chat-claude'}${m.failed?' chat-failed':''}">${chatMarkdown(m.text)}</div>`);
 if(d?.running)rows.push(`<div class="chat-msg chat-claude">${chatMarkdown(d.running.text)}<small class="chat-status">${esc(d.running.status)}…</small></div>`);
 const html=rows.join('')||'<p class="help chat-empty">알비온 시세·재고·주문을 무엇이든 물어보세요. 예: T7 유물 재고를 어떻게 정리할까?</p>';
 if(html!==chatView.seen){box.innerHTML=html;chatView.seen=html;if(atEnd||d?.running)box.scrollTop=box.scrollHeight}
 $('chat-send').hidden=!!d?.running;$('chat-stop').hidden=!d?.running;
}
async function loadChatList(){
 try{const r=await (await fetch('/api/chat')).json();
  $('chat-pick').innerHTML='<option value="">새 대화</option>'+r.conversations.map(c=>`<option value="${esc(c.id)}">${esc(c.title)}${c.running?' (답하는 중)':''}</option>`).join('');
  if(chatView.id&&!r.conversations.some(c=>c.id===chatView.id))chatView.id=null;
  $('chat-pick').value=chatView.id||'';
  $('chat-status-line').textContent=r.available?'':'이 컴퓨터에서 Claude Code를 찾지 못했습니다.';
 }catch(e){$('chat-status-line').textContent='불러오지 못했습니다: '+e.message}
}
async function loadChat(){
 clearTimeout(chatView.timer);
 if(!chatView.id){chatView.data=null;renderChat();return}
 const id=chatView.id;
 try{const r=await fetch('/api/chat/conversation?id='+encodeURIComponent(id)),data=await r.json();if(id!==chatView.id)return;if(!r.ok)throw Error(data.error||r.status);
  const wasRunning=chatView.data?.running;chatView.data=data;renderChat();
  if(data.running){if(chatOpen())chatView.timer=setTimeout(loadChat,700)}else if(wasRunning)loadChatList();
 }catch(e){$('chat-status-line').textContent='불러오지 못했습니다: '+e.message}
}
function pickChat(id){chatView.id=id||null;chatView.data=null;storage.set('chatConversation',chatView.id);$('chat-pick').value=id||'';$('chat-status-line').textContent='';loadChat()}
async function sendChat(){
 const box=$('chat-input'),text=box.value.trim();if(!text||$('chat-send').disabled)return;
 $('chat-send').disabled=true;$('chat-status-line').textContent='자료를 준비하는 중…';
 try{const r=await post('/api/chat/send',{conversation:chatView.id,text});box.value='';storage.set('chatDraft','');$('chat-status-line').textContent='';chatView.id=r.conversation;storage.set('chatConversation',r.conversation);await loadChatList();await loadChat();box.focus()}
 catch(e){$('chat-status-line').textContent='보내지 못했습니다: '+e.message}
 finally{$('chat-send').disabled=false}
}
$('chat-input').value=storage.get('chatDraft','');
$('chat-input').oninput=e=>storage.set('chatDraft',e.target.value);
// isComposing: Enter that finishes a Korean syllable must not send the message.
$('chat-input').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();sendChat()}};
$('chat-send').onclick=sendChat;
$('chat-stop').onclick=async()=>{try{await post('/api/chat/stop',{conversation:chatView.id})}catch(e){$('chat-status-line').textContent='멈추지 못했습니다: '+e.message}};
$('chat-new').onclick=()=>{pickChat(null);$('chat-input').focus()};
$('chat-pick').onchange=e=>pickChat(e.target.value);
document.querySelector('.tabs').addEventListener('click',e=>{if(e.target.closest('[data-tab=chat]')){loadChatList();loadChat()}});
if(storage.get('tab','now')==='chat'){loadChatList();loadChat()}
