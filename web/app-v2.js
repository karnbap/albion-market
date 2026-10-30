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
const ago=v=>{const minutes=Math.max(0,Math.round((Date.now()-Date.parse(v))/60000));return minutes<1?'방금':minutes<60?`${minutes}분 전`:date(v)};
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
const editing=()=>!!document.activeElement?.closest('#my-order-form,#purchase-form,#bank-form,.bank-city-form,.buy-target-form')&&document.activeElement.matches('select,input');
function table(headers,rows,key){return `<div class="table-wrap" data-scroll-key="${esc(key)}" tabindex="0" aria-label="${esc(key)} 표 스크롤"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
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
  const legacy=next.build!=='20261030.1';
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
   fillSettings();ready=true;
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
function quoteText(book,side){const r=quote(book);return `<span class="quote-label ${side}">${book.single_only?(side==='buy'?'매수 관측 · 최고가 미확인':'매도 관측 · 최저가 미확인'):(side==='buy'?'최고 매수':'최저 매도')}</span><span class="quote-value"><b>${n(r?.price)}</b><span class="quote-quantity">${n(r?.amount)}개</span></span><small class="quote-time">${r?.stale||book.stale?'지난 관측 · ':''}${date(r?.seen||book.seen)}</small>`}
function extremeMark(item,city,side){const e=state.city_extremes?.[item]?.[side];return e?.observations?.some(r=>r.city===city)?`<span class="extreme-badge ${side}">${side==='buy'?'도시 중 최고 매수':'도시 중 최저 매도'}${e.fresh?'':' · 지난 자료'}</span>`:''}
function extremeSummary(item,side){const e=state.city_extremes?.[item]?.[side];if(!e?.observations?.length)return `<div class="extreme-summary"><small>${side==='buy'?'도시 중 최고 매수':'도시 중 최저 매도'}</small><b>비교 자료 없음</b></div>`;return `<div class="extreme-summary ${side}"><small>${side==='buy'?'도시 중 최고 매수':'도시 중 최저 매도'} · ${e.fresh?'유효 관측':'지난 자료 비교'}</small><b>${n(e.price)}</b><span>${e.observations.map(r=>`${esc(state.cities[r.city])} · ${n(r.amount)}개`).join('<br>')}</span><small>${n(e.compared_cities)}도시 비교 · ${e.observations.map(r=>date(r.seen)).join(' / ')}</small></div>`}
function personalMark(item,city){const sale=state.personal_sales?.by_market?.[item+'/'+city];return sale?`<span class="personal-mark">내 판매 메일 · 누적 최고 ${n(sale.highest_price)} / ${n(sale.highest_price_amount)}개<small>체결 시각 미확인 · 최근 메일 ${date(sale.first_read)}</small></span>`:''}
function renderPersonal(){
 const sales=state.personal_sales;
 if(!sales){updateHtml('personal-sales',empty('거래 메일 기능을 적용하려면 분석 프로그램을 다시 실행해 주세요.'));return}
 $('personal-context').textContent=`전체 ${n(sales.mail_count)}개 메일`;
 const notices=(sales.conflicts?`<p class="warn">내용이 달라진 메일 ${n(sales.conflicts)}건은 합계에서 제외했습니다.</p>`:'')+(sales.unknown_identity?'<p class="help">캐릭터 식별 정보가 없는 메일이 있습니다. 여러 캐릭터 사용 시 중복 구분에 한계가 있습니다.</p>':'');
 if(!sales.mail_count){updateHtml('personal-sales',empty('저장된 판매 완료 메일이 아직 없습니다. 아래 메일 수집 진단에서 원인을 확인해 주세요.')+`<p class="help">${esc(sales.note)}</p>`+notices);return}
 const markets=Object.entries(sales.by_market).sort((a,b)=>b[1].mail_count-a[1].mail_count);
 const summary=table(['품목 · 도시','메일','판매 수량','최고 단가 (그 단가 수량)','최근 메일'],markets.map(([key,m])=>{const [item,city]=key.split('/');return `<tr><td>${esc(state.items[item]||item)} · ${esc(state.cities[city]||city)}</td><td>${n(m.mail_count)}</td><td>${n(m.total_amount)}개</td><td>${n(m.highest_price)} (${n(m.highest_price_amount)}개)</td><td>${date(m.first_read)}</td></tr>`}),'personal-markets');
 const recent=table(['처음 확인한 때','메일 받은 때','품목 · 도시','단가','수량'],(sales.recent||[]).map(r=>`<tr><td>${date(r.first_seen)}</td><td>${r.mailed?date(r.mailed):'—'}</td><td>${esc(state.items[r.item]||r.item)} · ${esc(state.cities[r.city]||r.city)}</td><td>${n(r.price)}</td><td>${n(r.amount)}개</td></tr>`),'personal-recent');
 updateHtml('personal-sales',`<h3 class="sub-head">시장별 합계</h3>${summary}<h3 class="sub-head">최근 메일 ${n((sales.recent||[]).length)}개</h3>${recent}<p class="help">${esc(sales.note)}</p>`+notices);
}
function renderPersonalMails(){
 const m=state.personal_mails;
 if(!m){updateHtml('personal-mails',empty('구매·만료 메일을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const kind={buy:'구매 주문 완료',buy_expired:'구매 주문 만료',sell_expired:'판매 주문 만료'};
 $('personal-mails-context').textContent=Object.entries(kind).map(([k,v])=>`${v} ${n(m.counts[k]||0)}개`).join(' · ');
 const traded=r=>r.kind==='buy'?`${n(r.amount)}개 구매`:`${n(r.amount)} / ${n(r.order_amount)}개 ${r.kind==='buy_expired'?'구매':'판매'}`;
 const silver=r=>`${n(r.silver)} ${r.kind==='buy'?'지출':r.kind==='buy_expired'?'환불':'수입'}`;
 const rows=m.recent.map(r=>`<tr><td>${date(r.mailed||r.first_seen)}</td><td>${kind[r.kind]||esc(r.kind)}</td><td>${esc(state.items[r.item]||r.item)} · ${esc(state.cities[r.city]||r.city)}</td><td>${n(r.price)}</td><td>${traded(r)}</td><td>${silver(r)}</td></tr>`);
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
 updateHtml('pick-history',rows.length?table(['시각','순위','거래','수량','투자','예상 이익','방식'],rows.flatMap(entry=>entry.picks.map((p,i)=>`<tr class="${i?'':'log-first'}"><td>${i?'':`${date(entry.created)}<small>${ago(entry.created)}</small>`}</td><td>${i+1}</td><td>${esc(state.items[p.item]||p.item)} · ${esc(state.cities[p.buy_city]||p.buy_city)}${p.kind==='buyout'?' 싹쓸이':' → '+esc(state.cities[p.sell_city]||p.sell_city)}</td><td>${n(p.units)}개</td><td>${n(p.cost)}</td><td class="positive">+${n(p.profit)}</td><td>${esc(kind[p.kind]||p.kind)}</td></tr>`)),'pick-history'):empty('아직 저장된 추천이 없습니다. 게임에서 시장을 열면 추천이 나올 때마다 여기에 쌓입니다.'));
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
 updateHtml('studies',`<p class="help">아래는 시장(도시)마다 따로 본 기초 자료입니다. 다른 도시에 나눠 파는 계획은 위 매매 계획에 있습니다.</p>`+table(['품목 · 도시','평균 체결가 평일 → 주말','하루 판매량 평일 / 주말','즉시 구매 비율','체결 관찰 (28일)','최저가 깎임','같은 도시 안에서 평일 매수 주문 → 주말 매도 주문 (수수료 후)'],rows.map(r=>{const a=r.plan_a;return `<tr><td>${esc(state.items[r.item])}<br><small>${esc(state.cities[r.city])}</small></td><td>${money(r.weekday?.price)} → ${money(r.weekend?.price)}${r.price_gap==null?'':`<small class="${r.price_gap>0?'positive':'warn'}">${pct(r.price_gap)}</small>`}</td><td>${r.weekday?n(r.weekday.per_day)+'개':'—'} / ${r.weekend?n(r.weekend.per_day)+'개':'—'}</td><td>${r.instant_buy_share==null?'—':n(r.instant_buy_share*100)+'%'}<small>근거 ${n(r.share_samples)}구간</small></td><td><small>매도 주문 ${n(r.fills_sell)}개 팔림<br>매수 주문 ${n(r.fills_buy)}개 채워짐</small></td><td>${r.undercut_hours==null?'—':'평균 '+n(r.undercut_hours,1)+'시간마다'}<small>관찰 ${n(r.watched_hours,1)}시간 · ${n(r.undercuts)}번</small></td><td>${a?`<span class="${a.margin>0?'positive':'warn'}">${a.margin>0?'+':''}${n(a.margin)} · ${n(a.roi*100,1)}%</span><small>${n(a.buy)} → ${n(a.sell)} · ${esc(a.basis)} 기준</small>`:'—'}</td></tr>`}),'studies'));
}
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
 updateHtml('my-orders',!rows.length?empty('등록한 내 매물이 없습니다. 게임 시장 창에서 내 주문 탭(매도·매수)을 열면 자동으로 들어옵니다.'):!shown.length?empty('고른 조건에 맞는 내 주문이 없습니다.'):table(['품목 · 도시','종류','내 가격','처음 수량 → 남은 수량','팔림/채워짐','내 앞 물량','상태',''],shown.map(r=>`<tr><td>${esc(state.items[r.item])}${r.source==='auto'?' <span class="pill">자동</span>':''}<br><small>${esc(state.cities[r.city])}</small></td><td>${sideName[r.side]}</td><td>${n(r.price)}</td><td>${n(r.amount)} → <b>${n(r.remaining)}</b></td><td>${n(r.filled)}개</td><td>${r.order_id?n(r.ahead)+'개':'—'}<small>${r.best==null?'':(r.side==='sell'?'최저 매도 ':'최고 매수 ')+n(r.best)}</small></td><td>${!r.order_id?'<span class="warn">아직 연결 안 됨 · 게임에서 내 주문 탭이나 이 시장을 열어 주세요</span>':r.is_best?'<span class="positive">맨 앞</span>':'<span class="warn">더 좋은 가격 있음</span>'}<small>${r.seen?'마지막 확인 '+ago(r.seen):'등록 '+ago(r.created)}</small></td><td><button type="button" class="secondary small-button" data-close="${r.id}">목록에서 빼기</button></td></tr>`),'my-orders'));
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
  return `<article class="stale-card"><div class="stale-head"><b>${esc(state.items[r.item]||r.item)} · ${esc(state.cities[r.city]||r.city)}</b><span class="pill">${n(r.idle_days,1)}일째 안 팔림</span></div><p class="help">내 가격 ${n(r.price)} · 남은 ${n(r.remaining)}개 · ${r.sold_before?'마지막 판매 후':'처음 본 뒤 판매 없음'} · ${r.cost==null?'원가 미확인 (매수 기록을 넣으면 손익 계산)':'원가 '+n(r.cost)} · 내 앞 ${n(r.ahead)}개 (최저가 ${n(r.best)}, 호가 ${r.book_seen?ago(r.book_seen):'미조회'}) · ${pace(r)}</p><p class="stale-advice">추천: <b class="${tone}">${title}</b> — ${why}</p><div class="diag-grid">${tile('그대로 기다리기',r.wait,`${n(r.wait.price)} 그대로`,'',wait)}${tile('가격 고치기',r.relist,r.relist?`${n(r.relist.price)}로 다시 올림 · 수수료 ${n(r.relist.fee)}`:'','이미 최저가',relist)}${tile('매수 주문에 바로 팔기',r.instant,r.instant?`${r.instant.units<r.remaining?`${n(r.instant.units)} / ${n(r.remaining)}개만`:`${n(r.instant.units)}개`} · 평균 ${n(r.instant.price)}`:'','매수 주문 없음',false)}</div></article>`}).join(''):empty('3일 넘게 안 팔린 내 매도 주문이 없습니다.'));
}
function renderCapital(){
 const c=state.capital;
 if(!c){updateHtml('capital',empty('투자금 추적을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const items=Object.entries(c.items);
 $('capital-context').textContent=`재고 경고 기준 ${n(c.heavy_days,1)}일치`;
 const change=ch=>ch.delta==null?`<b>—</b><span>${ch.days}일 전 기록 없음 · 쌓는 중</span>`:`<b>${ch.delta>0?'▲ +':ch.delta<0?'▼ ':''}${n(ch.delta)}</b><span>${ch.ratio==null?'':`${ch.ratio>0?'+':''}${n(ch.ratio*100,1)}% · `}${date(ch.at)} ${n(ch.total)}에서</span>`;
 const tiles=`<div class="diag-grid capital-grid"><div><small>지금 총 투자금</small><b>${n(c.total)}</b><span>보유 원가 ${n(c.holdings)} + 매수 주문 ${n(c.orders)}${c.unpriced?` · 원가 미확인 ${n(c.unpriced)}개 제외`:''}</span></div>${c.changes.map(ch=>`<div><small>${ch.days===1?'하루':ch.days+'일'} 전 대비</small>${change(ch)}</div>`).join('')}</div>`;
 const rows=items.map(([item,it])=>`<tr><td>${esc(state.items[item]||item)}</td><td>${n(it.held)}개</td><td>${n(it.buying)}개</td><td>${it.cost==null?'원가 미확인':n(it.cost)}</td><td>${n(it.value+it.orders_silver)}${it.unpriced?`<small>원가 미확인 ${n(it.unpriced)}개 제외</small>`:''}</td><td>${it.per_day==null?'판매량 미확인':n(it.per_day)+'개'}</td><td>${it.days==null?'—':n(it.days,1)+'일'}</td><td>${it.heavy?'<span class="warn">재고 많음 · 재고 쌓는 매수 추천 줄임</span>':it.days==null?'<small>판매량 이력이 없어 판단 못 함</small>':'<span class="positive">괜찮음</span>'}</td></tr>`);
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
 const rows=Object.entries(byCity).flatMap(([city,list])=>list.map((r,i)=>`<tr class="${i?'':'log-first'}"><td>${i?'':esc(place(city))}</td><td>${esc(state.items[r.item]||r.item)}</td><td>${r.city==='carried'?'—':n(r.listed)+'개'}</td><td>${bankCell(r)}</td><td>${r.city==='carried'?'—':`${n(r.finished)}개${r.finished_seen?`<small>${ago(r.finished_seen)} 확인</small>`:''}`}</td><td>${r.city==='carried'?`${n(r.carried)}개<small>${ago(r.bank_updated)} 확인</small>`:'—'}</td><td><b>${n(r.total)}개</b></td><td>${r.value==null?'원가 미확인':n(r.value)}</td><td>${r.city==='carried'?'—':r.value_now==null?'사는 사람 없음':`${n(Math.round(r.value_now))}<small>단가 ${n(Math.round(r.net_now))} · 잔량 ${n(r.sell_now_amount)}개</small>`}</td></tr>`));
 const finished=h.finished.length?`<h3 class="sub-head">거래 완료 탭 (시장별 마지막으로 받은 목록)</h3>${table(['시장','항목','추적 품목','내가 사서 안 찾은 것','받은 때'],h.finished.map(f=>`<tr><td>${esc(state.cities[f.city]||f.location)}</td><td>${n(f.entries)}건</td><td>${n(f.tracked)}건</td><td>${f.identity_known?n(f.waiting)+'개':'<span class="warn">캐릭터 확인 불가</span>'}</td><td>${ago(f.received)}</td></tr>`),'finished-lists')}`:'<p class="help">거래 완료 탭 목록은 아직 받지 않았습니다. 전용 수집기 local.3부터 게임 시장 창에서 거래 완료 탭을 열면 받아 둡니다.</p>';
 const unmapped=(h.unmapped_banks||[]).length?`<div><p class="warn">어느 도시 은행인지 모르는 은행이 있습니다. 빠른 이동 도착지처럼 도시가 아닌 곳에서 은행에 들어가면 이렇게 됩니다. 도시를 한 번 골라 주면 기억합니다.</p>${h.unmapped_banks.map(b=>`<form class="bank-city-form my-order-form" data-location="${esc(b.location)}"><span>은행 구역 ${esc(b.location)} · ${Object.entries(b.items).map(([k,v])=>`${esc(state.items[k]||k)} ${n(v)}개`).join(', ')||'추적 품목 없음'} · ${ago(b.seen)} 연 은행</span><label>도시<select name="city">${Object.entries(state.cities).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('')}</select></label><button type="submit">이 도시로 정하기</button></form>`).join('')}<p class="help" id="bank-city-status"></p></div>`:'';
 // 품목별 한 줄 요약: 얼마에 사서 몇 개 들고 있고, 지금 팔면 얼마가 되는가.
 const byItem={};
 for(const r of h.rows){const g=byItem[r.item]??={total:0,cost_silver:0,costed:0,value_now:0,priced:0,unpriced:0,instant:0,instant_units:0,cities:new Set()};
  g.total+=r.total; if(r.city!=='carried')g.cities.add(r.city);
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
     const where=b?`${esc(state.cities[b.city]||b.city)} <b>${n(b.price)}</b><small>${b.own?'내 주문이 최저가 · 같은 값':'최저 매도가보다 1 싸게'}</small><small>${b.fresh?'유효':'지난 자료'} · ${ago(b.seen)}</small>`:'—';
     const instant=g.instant_units?`<small>바로 팔면 ${money(g.instant/g.instant_units)}</small>`:'';
     return `<tr><td>${esc(state.items[item]||item)}<small>${n(g.cities.size)}개 도시</small></td>`
       +`<td><b>${n(g.total)}개</b></td><td>${money(avg)}</td><td>${money(g.cost_silver||null)}</td>`
       +`<td>${where}</td><td>${money(unit)}${instant}${warn}</td><td>${money(g.value_now||null)}</td>`
       +`<td class="${profit==null?'':profit>=0?'positive':'warn'}">${money(profit)}</td></tr>`}),
   'holdings-by-item')
   +`<p class="help"><b>제일 비싸게 팔 곳</b> = 거래 분석 도시 중 매도 주문을 가장 비싸게 걸 수 있는 곳입니다. 마지막으로 연 매도 목록의 최저 매도가보다 1 싸게 걸고, 최저가가 내 주문이면 같은 값으로 봅니다. <b>팔면 단가</b> = 그 값에서 판매세 ${n(state.settings.sales_tax*100,1)}%와 설치 수수료 ${n(state.settings.setup_fee*100,1)}%를 빼고, 다른 도시에 있는 물량은 개당 운송비(설정)를 뺀 값입니다. 그 값에 다 팔린다는 보장은 없습니다. 작은 글씨 '바로 팔면'은 매수 요청서에 즉시 팔 때(최고 매수가 − 판매세)입니다. 총수익은 원가를 아는 수량만 셉니다.</p>`
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
 const summary=p.summary.length?table(['품목',`최근 ${p.window_days}일 수량`,'평균 매수가','원가 (수수료 포함)','전체 수량','전체 평균 매수가','전체 원가'],p.summary.map(s=>`<tr><td>${esc(state.items[s.item]||s.item)}</td><td>${n(s.recent.units)}개</td><td>${n(s.recent.average)}</td><td>${n(s.recent.cost_average)}</td><td>${n(s.all.units)}개</td><td>${n(s.all.average)}</td><td>${n(s.all.cost_average)}</td></tr>`),'purchase-summary'):'';
 const rows=p.recent.map(r=>`<tr><td>${date(r.at)}</td><td>${esc(state.items[r.item]||r.item)} · ${esc(state.cities[r.city]||r.city)}</td><td>${how(r)}</td><td>${n(r.unit)}${r.order_price!=null&&Math.round(r.order_price)!==Math.round(r.unit)?`<small>주문 단가 ${n(r.order_price)}</small>`:''}</td><td>${n(r.amount)}개</td><td>${n(r.silver)}</td><td>${r.id!=null?`<button type="button" class="secondary small-button" data-remove-purchase="${r.id}">삭제</button>`:''}</td></tr>`);
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
 const title=p=>p.kind==='buyout'?`${city(p.buy_city)} ${esc(state.items[p.item])} 매물 싹쓸이`:`${esc(state.items[p.item])} · ${city(p.buy_city)} → ${city(p.sell_city)}`;
 const history=state.pick_history?.[0],last=!picks.length&&history;
 const groupOf=p=>p.group||(p.kind==='instant'?'instant':'wait');
 const groups=last?{instant:last.picks.filter(p=>groupOf(p)==='instant'),wait:last.picks.filter(p=>groupOf(p)==='wait')}:(state.now_groups||{instant:picks.filter(p=>groupOf(p)==='instant'),wait:picks.filter(p=>groupOf(p)==='wait')});
 if(last)$('now-note').textContent=`지금 계산할 최근 호가가 없어 ${ago(last.created)}(${date(last.created)}) 마지막 추천을 보여 줍니다. 가격이 바뀌었을 수 있으니 게임에서 시장을 다시 열어 확인하세요.`;
 $('now').classList.toggle('stale-picks',Boolean(last));
 const card=(p,i)=>`<li class="now-card ${p.kind}"><div class="now-rank">${i+1}</div><div class="now-body"><div class="now-title"><b>${title(p)}</b><span class="pill ${p.kind==='instant'?'instant-badge':''}">${esc(p.certainty)}</span></div><ol class="now-steps">${steps(p).map(x=>`<li>${x}</li>`).join('')}</ol>${p.timing?`<p class="timing">${esc(timingText(p.timing))}</p>`:''}${p.group!=='instant'&&p.outlook?`<p class="timing">${esc(outlookText(p.outlook))}</p>`:''}${p.warnings.length?`<p class="board-warn">${p.warnings.map(esc).join(' · ')}</p>`:''}</div><div class="now-money"><strong class="positive">+${n(p.profit)}</strong><small>투자 ${n(p.cost)} · ${n(p.roi*100,1)}%</small></div></li>`;
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
  const routes=c.routes.map(r=>{const alt=r.alternative;return `<li><div class="board-line"><b>${esc(state.items[r.item])}</b>${gain(r)}</div><div class="board-sub">${n(r.buy_price)} → ${dest(r)} ${n(r.sell_price)}</div><div class="board-sub">${esc(shortGuide(guideFor(r.item,r.sell_city)))}</div><div class="board-mode"><span class="pill ${r.mode==='best'?'':'instant-badge'}">${esc(r.status)}</span>${alt?`<span class="board-alt">${esc(alt.status)}${alt.sell_city===r.sell_city?'':' · '+dest(alt)}: +${n(alt.net_per_unit)} · ${n(alt.roi*100,1)}%</span>`:''}</div>${r.warnings.length?`<div class="board-warn">${r.warnings.map(esc).join(' · ')}</div>`:''}</li>`}).join('');
  const best=(rows,label)=>rows.map(r=>`<li><div class="board-line"><b>${esc(state.items[r.item])}</b><span>${n(r.price)} <small>${n(r.amount)}개</small></span></div><div class="board-sub">${n(r.compared)}개 도시 중 ${label}${r.lead?` · 다음 도시와 ${n(r.lead)} 차이`:' · 동률'}</div></li>`).join('');
  const sell=best(c.sell_here,'최고 매수'),buy=best(c.buy_here,'최저 매도'),b=c.buyout;
  const buyout=b?`<h4 class="buyout-head">매물 싹쓸이</h4><ul><li><div class="board-line"><b>${esc(state.items[b.item])}</b><strong class="positive">+${n(b.best.profit)}</strong></div><div class="board-sub">${n(b.best.units)}개 · ${n(b.best.cost)} 실버로 최저가 ${n(b.floor_now)} → ${n(b.best.floor)} (+${n(b.best.lift*100,1)}%)</div><div class="board-mode"><span class="pill risk-${b.best.risk}">${riskText[b.best.risk]}</span></div></li></ul>`:'';
  return `<article class="board-card ${c.city===selectedCity?'active':''}"><div class="board-head"><h3>${name}</h3><small>최신 ${n(c.fresh_items)}/${n(total)} 품목${c.newest_seen?' · '+ago(c.newest_seen):''}</small></div>${routes?`<h4>여기서 사서 팔 곳</h4><ul>${routes}</ul>`:''}${sell?`<h4 class="sell-here">여기서 바로 팔기 가장 비쌈</h4><ul>${sell}</ul>`:''}${buy?`<h4 class="buy-here">여기서 바로 사기 가장 쌈</h4><ul>${buy}</ul>`:''}${buyout}${routes||sell||buy||buyout?'':`<p class="board-empty">${c.fresh_items?'수익 조건을 넘는 거래 없음':'유효 호가 없음 · 게임에서 이 도시 시장을 열면 채워집니다.'}</p>`}${c.missing.length?`<p class="board-missing">다시 열어볼 시장: ${c.missing.map(m=>`${esc(state.items[m.item])}(${esc(m.reason)})`).join(', ')}</p>`:''}<button type="button" class="secondary board-detail" data-city="${esc(c.city)}">${name} 상세 보기</button></article>`;
 }).join(''));
}
function renderBuyouts(){
 const rows=state.buyouts;
 if(!rows){updateHtml('buyouts',empty('싹쓸이 분석을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const hidden=settingsWarnings(),lists=Object.values(state.books).flatMap(cities=>Object.values(cities)).filter(pair=>pair.sell.fresh).length;
 $('buyout-notes').textContent=[...(state.buyout_notes||[]),...hidden].join(' · ');
 $('buyout-context').textContent=`유효 매도 목록 ${n(lists)}곳 중 이익 나는 곳 ${n(rows.length)}곳`;
 const buyoutRow=r=>{const b=r.best,reasons=r.reasons.filter(x=>!hidden.includes(x));return `<tr><td>${esc(state.items[r.item])}<br>${esc(state.cities[r.city])}<small>매도 목록 ${ago(r.seen)} · ${n(r.visible_levels)}개 가격대 ${n(r.visible_units)}개</small></td><td>${n(r.floor_now)} → <b>${n(b.floor)}</b><small class="positive">+${n(b.lift*100,1)}%</small><small>다음 매물 ${n(b.next_amount)}개${b.next_wall?' · 물량 벽':''}</small></td><td><b>${n(b.units)}개</b><br>${n(b.cost)} 실버<small>평균 매입가 ${n(b.average_cost,1)}</small><span class="pill risk-${b.risk}">${riskText[b.risk]}</span></td><td>${n(r.history_average,1)}<small>최고 구간 평균 ${n(r.history_high,1)}</small><small>${esc(guideLine(guideFor(r.item,r.city)))}</small></td><td>${n(b.relist)}<small class="positive">+${n(b.profit)} · ${n(b.roi*100,1)}%</small></td><td><small>${r.steps.slice(0,5).map(s=>`${n(s.units)}개 ${n(s.cost)} → ${n(s.floor)} (+${n(s.lift*100,1)}%) · 이익 ${s.profit>0?'+':''}${n(s.profit)} · ${riskText[s.risk]}`).join('<br>')}</small>${reasons.length?`<div class="reason-list">${reasons.map(x=>`<span class="pill reason">${esc(x)}</span>`).join('')}</div>`:''}</td></tr>`};
 const buyoutHead=['품목 · 도시','지금 최저가 → 정리 후','사야 할 개수 · 총 비용','7일 평균가 · 판매 속도','다시 올릴 가격 · 예상 이익','단계별 비용 · 확인할 점'],pastBuyouts=state.past_buyouts?.rows||[];
 const noBuyouts='지금 싹쓸이해서 이익이 남는 시장 없음 · 유효 시간 안의 매도 목록만 계산합니다';
 updateHtml('buyouts',rows.length?table(buyoutHead,rows.map(buyoutRow),'buyouts'):pastBuyouts.length?`<p class="warn past-note">${esc(noBuyouts)}</p><h3 class="sub-head">지난 분석 · ${date(state.past_buyouts.seen)} (${ago(state.past_buyouts.seen)}) 기준</h3><p class="help">그때 본 매도 목록으로 계산한 결과입니다. 지금은 매물이 팔리거나 새로 올라와 바뀌었을 수 있으니, 사기 전에 게임에서 그 시장을 다시 열어 확인해 주세요.</p>${table(buyoutHead,pastBuyouts.map(buyoutRow),'buyouts-past')}`:empty(noBuyouts));
}
function renderBuyUnder(){
 const rows=state.buy_under;if(!rows){updateHtml('buy-under',empty('분석 프로그램을 다시 실행하면 나옵니다.'));return}
 const cities=rows[0]?.rows.map(r=>r.city)||[],city=c=>esc(state.cities[c]||c);
 $('buy-under-context').textContent=`${cities.map(c=>state.cities[c]).join(', ')} · 각 시장의 마지막 매도 목록`;
 const cell=r=>!r.known?`<td><small>${r.seen?'단일 주문만 봄':'미조회'}</small></td>`:`<td class="${r.units?'':'muted'}"><b>${n(r.units)}개</b>${r.units?`<small>평균 ${n(r.average)} · ${n(r.cost)} 실버</small>`:''}<small>최저 ${n(r.lowest)}</small><small>${r.fresh?'유효':'지난 자료'} · ${ago(r.seen)}</small></td>`;
 const line=u=>`<tr><th scope="row">${esc(state.items[u.item])}</th><td><form class="buy-target-form" data-item="${esc(u.item)}"><input type="number" name="price" min="1" step="1" inputmode="numeric" aria-label="${esc(state.items[u.item])} 목표가" value="${u.chosen??''}" placeholder="${u.suggested??''}"></form><small>${u.chosen?'직접 정함':u.suggested?'자동 제안':'팔 곳 호가 없음'}${u.chosen&&u.suggested?` · 제안 ${n(u.suggested)}`:''}</small></td>${u.rows.map(cell).join('')}<td><b>${n(u.units)}개</b>${u.units?`<small>${n(u.cost)} 실버</small>`:''}${u.profit!=null?`<small class="${u.profit>0?'positive':'warn'}">예상 ${u.profit>0?'+':''}${n(u.profit)}</small>`:''}</td><td>${u.sell_city?`${city(u.sell_city)} <b>${n(u.sell)}</b><small>7일 평균 체결 ${u.sell_average!=null?n(u.sell_average):'-'}</small>`:'-'}</td></tr>`;
 updateHtml('buy-under',rows.length?table(['품목','목표가 (바로 사기)',...cities.map(c=>state.cities[c]),'합계','팔 곳 · 지금 최저 매도'],rows.map(line),'buy-under'):empty('추적 품목이 없습니다.'));
}
function saveBuyTargets(){const targets={};for(const f of document.querySelectorAll('.buy-target-form')){const v=f.price.value.trim();if(v!=='')targets[f.dataset.item]=Number(v)}return post('/api/settings',{buy_targets:targets})}
function renderVolumeGuide(){
 const g=guideFor(selectedItem,selectedCity),labels={1:'최근 24시간',7:'최근 7일',28:'최근 28일'};
 updateHtml('volume-guide',g?`<div class="volume-guide"><h3>사서 되팔 때 수량 경고 · ${esc(state.cities[selectedCity])} ${esc(state.items[selectedItem])}</h3><div class="guide-windows">${g.windows.map(w=>`<div><small>${labels[w.days]} 판매량</small><b>${w.amount==null?'—':n(w.amount)+'개'}</b><span>${w.per_day==null?'하루 평균 계산 불가':'하루 '+n(w.per_day)+'개'} · 관측 ${n(w.covered_hours)}시간</span></div>`).join('')}</div><p class="${g.per_day==null||g.per_day<1?'warn':'guide-line'}">${esc(guideLine(g))}</p>${state.outlook?`<p class="${state.outlook.low_window?'warn':'guide-line'}">앞으로 6시간 예상 판매 ${n(state.outlook.next6)}개(평소 ${n(state.outlook.usual6)}개) · 24시간 ${n(state.outlook.next24)}개 · ${state.outlook.weekend?'주말':'평일'} 시간대 기준${state.outlook.low_window?' · 지금부터 수요가 적은 시간대라 주문을 한꺼번에 쌓지 마세요':''}</p>${state.outlook.quiet_hours.length?`<p class="help">앞으로 24시간 중 수요가 평소 절반 아래인 시간: ${esc(hourRanges(state.outlook.quiet_hours))}</p>`:''}`:'<p class="help">시간대별 판매 기록(24시간 시장 이력)이 부족해 앞으로의 예상 판매량을 계산하지 못했습니다.</p>'}<p class="help">세 기간 중 가장 느린 하루 판매량에서 내 몫(설정의 '내 몫', 기본 20%)만 내가 팔 수 있다고 봅니다. 나머지는 다른 판매자 몫입니다. 내 몫 하루치보다 많이 사면 다 팔 때까지 하루 넘게 걸려 그사이 다른 판매자가 더 싸게 올릴 수 있고, 3일치를 넘기면 재고가 오래 묶일 위험이 큽니다. 관측 시간이 짧은 기간(24시간은 6시간 미만, 7일·28일은 24시간 미만)은 계산에서 뺍니다.</p></div>`:'');
}
function renderOverview(){
 const items=Object.keys(state.items);
 updateHtml('overview',table(['도시',...items.map(i=>state.items[i])],Object.entries(state.cities).map(([city,name])=>`<tr><th scope="row">${esc(name)}</th>${items.map(item=>{const b=state.books[item][city];return `<td><button type="button" class="market-cell ${item===selectedItem&&city===selectedCity?'selected':''}" data-item="${esc(item)}" data-city="${esc(city)}" aria-label="${esc(name)} ${esc(state.items[item])} 상세"><span>${extremeMark(item,city,'buy')}${quoteText(b.buy,'buy')}</span><span>${extremeMark(item,city,'sell')}${quoteText(b.sell,'sell')}</span>${personalMark(item,city)}</button></td>`}).join('')}</tr>`),'all-markets'));
 $('quick-city').textContent='도시 비교 + '+state.cities[selectedCity]+' 상세';
 updateHtml('all-items',items.map(item=>{const b=state.books[item][selectedCity];return `<article class="panel item-summary"><h3>${esc(state.items[item])}</h3><div class="extremes-pair">${extremeSummary(item,'buy')}${extremeSummary(item,'sell')}</div><div class="item-city-table"><table><thead><tr><th>도시</th><th>최고 매수</th><th>최저 매도</th></tr></thead><tbody>${Object.entries(state.cities).map(([city,name])=>{const pair=state.books[item][city];return `<tr><th scope="row">${esc(name)}</th>${['buy','sell'].map(side=>{const q=quote(pair[side]),best=state.city_extremes?.[item]?.[side]?.observations?.some(o=>o.city===city);return `<td class="quote-cell ${side} ${best?'best-'+side:''}">${n(q?.price)}<small>${n(q?.amount)}개</small><small class="quote-status">${pair[side].single_only?'미확인':q?.stale||pair[side].stale?'지난 자료':q?'유효 관측':'미조회'}</small></td>`}).join('')}</tr>`}).join('')}</tbody></table></div><h4 class="selected-city-label">${esc(state.cities[selectedCity])} 물량 상세</h4>${['buy','sell'].map(side=>{const book=b[side],d=book.fresh?book.analysis_depth:book.depth;return `<div class="item-side ${side}"><p>${quoteText(book,side)}</p><small>${d?.reference?.large?'대량 기준':'소량 제외 기준'} ${n(d?.reference?.price)} · ${n(d?.reference?.amount)}개</small><small>앞쪽 소량 ${n(d?.leading_small_levels)}호가 · ${n(d?.leading_small_amount)}개</small><div class="wall-list">${(d?.large_levels||[]).slice(0,3).map(r=>`<span class="pill ${r.wall?'wall-badge':''}">${r.wall?'벽':'대량'} ${n(r.price)} · ${n(r.amount)}개</span>`).join('')||'대량 관측 없음'}</div></div>`}).join('')}</article>`}).join(''));
}
function render(){
 renderNow();renderPickHistory();renderMyOrders();renderStale();renderCapital();renderHoldings();renderPurchases();renderPortfolio();renderStudies();renderBookPatterns();renderBoard();renderBuyouts();renderOverview();renderBuyUnder();renderPersonal();renderPersonalMails();renderMailDiagnosis();renderVolumeGuide();
 $('detail-context').textContent=state.cities[selectedCity]+' · '+state.items[selectedItem];
 const books=state.books[selectedItem];
 updateHtml('cities',Object.entries(state.cities).map(([id,name])=>{
  const b=books[id],sell=quote(b.sell),buy=quote(b.buy),sr=b.sell.depth?.reference,br=b.buy.depth?.reference;
  return `<button type="button" class="city-card ${id===selectedCity?'active':''}" data-city="${id}" aria-pressed="${id===selectedCity}"><h3>${esc(name)}</h3><div class="price-pair"><div><span>${b.buy.single_only?'매수 관측 · 최고가 미확인':'최고 매수'}</span><br><b>${n(buy?.price)}</b><small>${n(buy?.amount)}개 ${buy?.stale||b.buy.stale?'· 이전 관측':''}</small></div><div><span>${b.sell.single_only?'매도 관측 · 최저가 미확인':'최저 매도'}</span><br><b>${n(sell?.price)}</b><small>${n(sell?.amount)}개 ${sell?.stale||b.sell.stale?'· 이전 관측':''}</small></div></div><div class="reference-mini">관측 물량 기준 매수 ${n(br?.price)} / 매도 ${n(sr?.price)}</div><div class="age">매수 ${date(b.buy.seen)}<br>매도 ${date(b.sell.seen)}</div></button>`
 }).join(''));
 renderBook('sell',books[selectedCity].sell);renderBook('buy',books[selectedCity].buy);
 updateHtml('history',state.history.map(h=>`<article class="history-card"><h3>최근 ${h.days===1?'24시간':h.days+'일'}</h3><div class="metric">${n(h.amount)} <small>개 · 관측 판매량</small></div><div class="stats"><div><small>수량 가중평균가</small><b>${n(h.weighted_average,2)}</b></div><div><small>최고 구간 평균가</small><b>${n(h.highest_bucket_average,2)}</b></div><div><small>고가 구간 전체 판매량</small><b>${n(h.near_high_amount)}개</b></div><div><small>관측 시간 범위</small><b>${n(h.coverage*100,1)}%</b></div></div><div class="coverage">${n(h.covered_hours)} / ${n(h.requested_hours)}시간 확인 · ${n(h.resolution_hours)}시간 구간<br>최신 구간 끝 ${date(h.newest_bucket_end)}${h.stale?'<br><strong class="warn">최신 구간이 오래됨 · 시장 이력을 다시 열어 주세요.</strong>':''}</div></article>`).join(''));
 renderHigh();renderBars('weekdays',state.patterns.weekdays,x=>['월','화','수','목','금','토','일'][x]);renderBars('hours',state.patterns.hourly,x=>String(x).padStart(2,'0')+'시');$('pattern-note').textContent=state.patterns.note;
 const hidden=settingsWarnings();
 $('candidate-notes').textContent=[...(state.candidate_notes||[]),...hidden].join(' · ');
 $('reset-target').textContent=`대상: ${state.items[selectedItem]} · ${state.cities[selectedCity]} (시장 상세에서 고른 시장)`;
 const ownLeft=(item,city,s)=>(state.my_orders||[]).filter(m=>m.item===item&&m.city===city&&m.side===s).reduce((a,m)=>a+m.remaining,0);
 const style=$('candidate-filter').value,listed=state.candidates.filter(r=>style==='all'||(style==='instant')===(r.mode==='instant'));
 const candidateRow=r=>{const instantBuy=r.mode==='instant'||r.mode==='instant_list',reasons=r.reasons.filter(x=>!hidden.includes(x));return `<tr class="${r.mode==='depth'?'conditional':''}"><td>${esc(state.items[r.item])}<br><span class="pill ${instantBuy?'instant-badge':''}">${esc(r.status)}</span></td><td>${esc(state.cities[r.buy_city])}<br>${n(r.buy_price)}<small>${instantBuy?`바로 살 수 있는 잔량 ${n(r.buy_available)}개`:`기준 잔량 ${n(r.buy_reference?.amount)}개`}</small></td><td>${esc(state.cities[r.sell_city])}<br>${n(r.sell_price)}<small>${r.mode==='instant'?`바로 팔 수 있는 잔량 ${n(r.sell_available)}개`:`기준 잔량 ${n(r.sell_reference?.amount)}개`}</small></td><td class="positive">${n(r.net_per_unit)}<br>${n(r.roi*100,1)}%</td><td>${instantBuy?'':`<b>매수 앞 ${n(r.buy_ahead)}개 / 매도 앞 ${n(r.sell_ahead)}개</b>`}<small>관측 7일 평균 ${n(r.history_weighted_average,2)} · 최고 구간 평균 ${n(r.history_highest_bucket_average,2)} · ${n(r.history_amount)}개</small><small>파는 곳 ${esc(guideLine(guideFor(r.item,r.sell_city)))}</small>${ownLeft(r.item,r.sell_city,'sell')?`<small class="warn">파는 곳에 내 매도 주문 ${n(ownLeft(r.item,r.sell_city,'sell'))}개 남음</small>`:''}${r.mode!=='instant'&&r.mode!=='instant_list'&&ownLeft(r.item,r.buy_city,'buy')?`<small class="warn">사는 곳에 내 매수 주문 ${n(ownLeft(r.item,r.buy_city,'buy'))}개 남음</small>`:''}${reasons.length?`<div class="reason-list">${reasons.map(x=>`<span class="pill reason">${esc(x)}</span>`).join('')}</div>`:''}</td></tr>`};
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
function showTab(name){if(!tabs.some(b=>b.dataset.tab===name))name='now';for(const b of tabs){const on=b.dataset.tab===name;b.classList.toggle('active',on);b.setAttribute('aria-selected',String(on))}for(const panel of document.querySelectorAll('.tab-panel'))panel.hidden=panel.dataset.tab!==name;storage.set('tab',name)}
document.querySelector('.tabs').onclick=e=>{const b=e.target.closest('[data-tab]');if(b)showTab(b.dataset.tab)};
showTab(storage.get('tab','now'));
$('city-board').onclick=e=>{const button=e.target.closest('[data-city]');if(button){$('city').value=button.dataset.city;chooseView();showTab('market');document.querySelector('.tabs').scrollIntoView({block:'start'})}};
$('cities').onclick=e=>{const card=e.target.closest('[data-city]');if(card){$('city').value=card.dataset.city;chooseView()}};
$('candidate-filter').value=['all','instant','wait'].includes(storage.get('candidateFilter','all'))?storage.get('candidateFilter','all'):'all';
$('my-order-form').onsubmit=async e=>{e.preventDefault();const f=e.target;try{await post('/api/my-orders',{item:f.item.value,city:f.city.value,side:f.side.value,price:Number(f.price.value),amount:Number(f.amount.value)});f.price.value='';f.amount.value='';$('my-order-status').textContent='추가했습니다. 게임에서 그 시장을 열면 자동으로 연결됩니다.';await refresh()}catch(err){$('my-order-status').textContent='추가 실패: '+err.message}};
$('my-orders-filters').onclick=e=>{const b=e.target.closest('[data-key]');if(!b||!state)return;storage.set('myOrdersFilter',{...myOrderFilter(state.my_orders||[]),[b.dataset.key]:b.dataset.value});preserveViewport(renderMyOrders)};
$('my-orders').onclick=async e=>{const b=e.target.closest('[data-close]');if(!b)return;try{await post('/api/my-orders',{action:'close',id:Number(b.dataset.close)});await refresh()}catch(err){$('my-order-status').textContent='목록에서 빼기 실패: '+err.message}};
$('bank-form').onsubmit=async e=>{e.preventDefault();const f=e.target;try{await post('/api/bank',{item:f.item.value,city:f.city.value,amount:Number(f.amount.value)});f.amount.value='';$('bank-status').textContent='은행 수량을 저장했습니다.';await refresh()}catch(err){$('bank-status').textContent='저장 실패: '+err.message}};
$('buy-under').onchange=async e=>{if(!e.target.closest('.buy-target-form'))return;const s=$('buy-under-status');try{await saveBuyTargets();s.textContent='목표가 저장됨';await refresh()}catch(err){s.textContent='저장 실패: '+err.message}};
$('buy-under').onsubmit=e=>{if(!e.target.closest('.buy-target-form'))return;e.preventDefault();document.activeElement?.blur()};
$('holdings').onsubmit=async e=>{const f=e.target.closest('.bank-city-form');if(!f)return;e.preventDefault();try{await post('/api/bank-city',{location:f.dataset.location,city:f.city.value});document.activeElement?.blur();await refresh()}catch(err){const s=$('bank-city-status');if(s)s.textContent='저장 실패: '+err.message}};
$('purchase-form').onsubmit=async e=>{e.preventDefault();const f=e.target;try{await post('/api/purchases',{item:f.item.value,city:f.city.value,kind:f.kind.value,price:Number(f.price.value),amount:Number(f.amount.value),bought:f.bought.value||null});f.price.value='';f.amount.value='';f.bought.value='';$('purchase-status').textContent='매수 기록을 추가했습니다.';await refresh()}catch(err){$('purchase-status').textContent='추가 실패: '+err.message}};
$('purchases').onclick=async e=>{const b=e.target.closest('[data-remove-purchase]');if(!b)return;try{await post('/api/purchases',{action:'remove',id:Number(b.dataset.removePurchase)});await refresh()}catch(err){$('purchase-status').textContent='삭제 실패: '+err.message}};
$('candidate-filter').onchange=()=>{storage.set('candidateFilter',$('candidate-filter').value);if(state)render()};
$('period').onchange=()=>{rememberView();preserveViewport(renderHigh)};
$('book-filter').onchange=()=>{rememberView();if(state)preserveViewport(()=>{renderBook('sell',state.books[selectedItem][selectedCity].sell);renderBook('buy',state.books[selectedItem][selectedCity].buy)})};
$('settings').addEventListener('input',queueDraft);$('settings').addEventListener('change',queueDraft);
$('settings').onsubmit=e=>{e.preventDefault();clearTimeout(saveTimer);saveSettings()};
window.addEventListener('scroll',()=>{if(!restorePending){clearTimeout(scrollTimer);scrollTimer=setTimeout(rememberView,120)}},{passive:true});
window.addEventListener('pagehide',()=>{if(ready){rememberView();if(draft)storage.set('settingsDraft',readDraft())}});
$('reset').onclick=async()=>{const target=`${state.items[selectedItem]} · ${state.cities[selectedCity]}`;if(!window.confirm(`${target} 시장의 지금 호가를 비울까요? 게임에서 그 시장을 다시 열기 전까지 호가와 매매 후보가 빈칸이 됩니다.`))return;try{await post('/api/reset',{item:selectedItem,city:selectedCity});$('reset-status').textContent=`${target} 호가를 비웠습니다 · 게임에서 그 시장의 매수·매도 목록을 다시 열어 주세요.`;await refresh()}catch(e){$('reset-status').textContent='호가 비우기 실패: '+e.message}};
// A hidden tab does not poll; the server rebuilds every market on each request.
refresh();setInterval(()=>{if(document.hidden)return;refresh();if(draft&&!saving)scheduleSave()},5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
document.addEventListener('focusout',()=>setTimeout(()=>{if(ready&&!editing())refresh()},0));
