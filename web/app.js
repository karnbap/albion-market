'use strict';
const $=id=>document.getElementById(id);
const n=(v,d=0)=>v===null||v===undefined?'—':Number(v).toLocaleString('ko-KR',{maximumFractionDigits:d});
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const storage={get(key,fallback){try{return JSON.parse(localStorage.getItem('albion.'+key))??fallback}catch{return fallback}},set(key,value){try{localStorage.setItem('albion.'+key,JSON.stringify(value))}catch{}}};
const savedView=storage.get('view',{});
let state,token,selectedItem=savedView.item||'',selectedCity=savedView.city||'1002',ready=false,loading=false,refreshQueued=false,viewVersion=0;
let formVersion=0,saving=false,saveQueued=false,saveTimer,scrollTimer,restorePending=true;
let draft=storage.get('settingsDraft',null);
const percentKeys=['sales_tax','setup_fee','minimum_margin','near_high_fraction','depth_price_band'];
const numberKeys=['trade_budget','fresh_minutes','transport_per_unit','small_quantity_max','large_quantity_min','wall_quantity_min','wall_multiple'];
const boolKeys=['fees_confirmed','transport_confirmed'];
const fragmentCache=new Map(),innerScroll=storage.get('tableScroll',{});
const formatters=new Map();
const date=v=>{if(!v)return '미조회';const zone=state?.timezone||'Europe/Paris';if(!formatters.has(zone))formatters.set(zone,new Intl.DateTimeFormat('ko-KR',{timeZone:zone,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZoneName:'shortOffset'}));return formatters.get(zone).format(new Date(v))};
const ago=v=>{const minutes=Math.max(0,Math.round((Date.now()-Date.parse(v))/60000));return minutes<1?'방금':minutes<60?`${minutes}분 전`:date(v)};
const riskText={ok:'무난',caution:'조금 위험',danger:'위험',unknown:'판매량 미확인'};
const guideFor=(item,city)=>state.volume_guides?.[item+'/'+city];
const guideLine=g=>!g||g.per_day==null?'판매량 이력 부족 · 몇 개까지 괜찮은지 모름':g.per_day<1?'최근 하루 1개도 안 팔림 · 되팔 목적이면 위험':`하루 ${n(g.per_day)}개 팔림 · ${n(g.caution_above)}개 넘게 사면 조금 위험 · ${n(g.danger_above)}개 넘으면 위험`;
const shortGuide=g=>!g||g.per_day==null?'파는 곳 판매량 미확인':`파는 곳 하루 ${n(g.per_day)}개 팔림 · ${n(g.caution_above)}개까지 무난`;
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
  if(ready&&state?.ui_revision&&next.ui_revision&&state.ui_revision!==next.ui_revision){rememberView();if(draft)storage.set('settingsDraft',readDraft());window.location.reload();return}
  const legacy=next.build!=='20261026.6';
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
 const recent=table(['처음 확인한 때','품목 · 도시','단가','수량'],(sales.recent||[]).map(r=>`<tr><td>${date(r.first_seen)}</td><td>${esc(state.items[r.item]||r.item)} · ${esc(state.cities[r.city]||r.city)}</td><td>${n(r.price)}</td><td>${n(r.amount)}개</td></tr>`),'personal-recent');
 updateHtml('personal-sales',`<h3 class="sub-head">시장별 합계</h3>${summary}<h3 class="sub-head">최근 메일 ${n((sales.recent||[]).length)}개</h3>${recent}<p class="help">${esc(sales.note)}</p>`+notices);
}
function renderMailDiagnosis(){
 const d=state.mail_diagnostics;
 if(!d){updateHtml('mail-diagnosis',empty('진단 기능을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const topics=Object.fromEntries(d.private_topics.map(t=>[t.topic,t])),mail=topics.marketnotifications,orders=topics['marketorders.ingest'];
 const c=d.counts,outcome={saved:'저장',duplicate:'이미 저장한 메일',conflict:'내용 충돌',ignored:'무시',rejected:'형식 오류'};
 let verdict;
 if(!d.private_topics.length)verdict=['warn','수집기의 개인 자료 통로(메일 전용)로 들어온 자료가 한 번도 없습니다. 수집기가 이 프로그램이 아닌 다른 방법으로 켜졌거나 오래된 실행 설정일 수 있습니다. 게임을 끄고 10초 뒤 다시 켜서 자동 수집으로 시작해 주세요.'];
 else if(!mail)verdict=['warn',`개인 자료 통로는 연결돼 있지만(시장 자료 ${n(orders?.count||0)}회) 판매 메일은 한 번도 오지 않았습니다. 수집기는 켜진 뒤 받은 우편함 목록으로만 메일을 읽습니다. 지역을 한 번 이동한 다음 우편함을 닫았다 다시 열고 판매 완료 메일을 열어 주세요. 아래 수집기 기록에 ‘Mail Info is not valid’가 보이면 이 경우입니다.`];
 else if(!c.saved)verdict=['warn','판매 메일은 들어왔지만 저장된 것이 없습니다. 아래 기록의 무시 이유를 확인해 주세요.'];
 else verdict=['ok',`판매 메일이 정상적으로 들어오고 있습니다 · 저장 ${n(c.saved)}건`];
 const events=d.events.length?table(['받은 때','종류','품목','도시 코드','결과','이유'],d.events.map(e=>`<tr><td>${date(e.received)}</td><td>${esc(e.kind||'—')}</td><td>${esc(state.items[e.item]||e.item||'—')}</td><td>${esc(e.location||'—')}</td><td>${esc(outcome[e.outcome]||e.outcome)}</td><td>${esc(e.detail||'')}</td></tr>`),'mail-events'):'';
 const log=d.client_log.lines.length?`<pre class="client-log">${d.client_log.lines.map(esc).join('\n')}</pre>`:`<p class="help">${d.client_log.file?'수집기 기록에 메일 관련 줄이 없습니다.':'수집기 기록 파일을 찾지 못했습니다.'}</p>`;
 updateHtml('mail-diagnosis',`<p class="${verdict[0]==='ok'?'positive':'warn'} diagnosis">${esc(verdict[1])}</p><div class="diag-grid"><div><small>개인 통로 · 메일</small><b>${mail?n(mail.count)+'회':'없음'}</b><span>${mail?'마지막 '+date(mail.last):''}</span></div><div><small>개인 통로 · 시장 자료</small><b>${orders?n(orders.count)+'회':'없음'}</b><span>${orders?'마지막 '+date(orders.last):''}</span></div><div><small>메일 처리 결과</small><b>${Object.entries(c).map(([k,v])=>`${outcome[k]||k} ${n(v)}`).join(' · ')||'없음'}</b></div></div>${events}<h3 class="sub-head">수집기 기록 (메일 관련 최근 줄)</h3>${log}<p class="help">수집기는 판매 주문 완료 메일만 보냅니다. 구매 주문 완료·만료 메일은 보내지 않습니다.</p>`);
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
function renderNow(){
 const picks=state.now;
 if(!picks){updateHtml('now-instant',empty('추천을 쓰려면 분석 프로그램을 다시 실행해 주세요.'));return}
 const fresh=Object.values(state.books).flatMap(c=>Object.values(c)).filter(p=>p.sell.fresh||p.buy.fresh).length,total=Object.keys(state.items).length*Object.keys(state.cities).length;
 const clock=new Date();$('now-context').textContent=`내 컴퓨터 ${WEEK[(clock.getDay()+6)%7]}요일 ${clock.toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'})} · 최근 ${n(state.settings.fresh_minutes)}분 안에 연 시장 ${n(fresh)}/${n(total)}곳 · 한 거래 최대 ${n(state.settings.trade_budget)} 실버`;
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
 updateHtml('buyouts',rows.length?table(['품목 · 도시','지금 최저가 → 정리 후','사야 할 개수 · 총 비용','7일 평균가 · 판매 속도','다시 올릴 가격 · 예상 이익','단계별 비용 · 확인할 점'],rows.map(r=>{const b=r.best,reasons=r.reasons.filter(x=>!hidden.includes(x));return `<tr><td>${esc(state.items[r.item])}<br>${esc(state.cities[r.city])}<small>매도 목록 ${ago(r.seen)} · ${n(r.visible_levels)}개 가격대 ${n(r.visible_units)}개</small></td><td>${n(r.floor_now)} → <b>${n(b.floor)}</b><small class="positive">+${n(b.lift*100,1)}%</small><small>다음 매물 ${n(b.next_amount)}개${b.next_wall?' · 물량 벽':''}</small></td><td><b>${n(b.units)}개</b><br>${n(b.cost)} 실버<small>평균 매입가 ${n(b.average_cost,1)}</small><span class="pill risk-${b.risk}">${riskText[b.risk]}</span></td><td>${n(r.history_average,1)}<small>최고 구간 평균 ${n(r.history_high,1)}</small><small>${esc(guideLine(guideFor(r.item,r.city)))}</small></td><td>${n(b.relist)}<small class="positive">+${n(b.profit)} · ${n(b.roi*100,1)}%</small></td><td><small>${r.steps.slice(0,5).map(s=>`${n(s.units)}개 ${n(s.cost)} → ${n(s.floor)} (+${n(s.lift*100,1)}%) · 이익 ${s.profit>0?'+':''}${n(s.profit)} · ${riskText[s.risk]}`).join('<br>')}</small>${reasons.length?`<div class="reason-list">${reasons.map(x=>`<span class="pill reason">${esc(x)}</span>`).join('')}</div>`:''}</td></tr>`}),'buyouts'):empty('지금 싹쓸이해서 이익이 남는 시장 없음 · 유효 시간 안의 매도 목록만 계산합니다'));
}
function renderVolumeGuide(){
 const g=guideFor(selectedItem,selectedCity),labels={1:'최근 24시간',7:'최근 7일',28:'최근 28일'};
 updateHtml('volume-guide',g?`<div class="volume-guide"><h3>사서 되팔 때 수량 경고 · ${esc(state.cities[selectedCity])} ${esc(state.items[selectedItem])}</h3><div class="guide-windows">${g.windows.map(w=>`<div><small>${labels[w.days]} 판매량</small><b>${w.amount==null?'—':n(w.amount)+'개'}</b><span>${w.per_day==null?'하루 평균 계산 불가':'하루 '+n(w.per_day)+'개'} · 관측 ${n(w.covered_hours)}시간</span></div>`).join('')}</div><p class="${g.per_day==null||g.per_day<1?'warn':'guide-line'}">${esc(guideLine(g))}</p>${state.outlook?`<p class="${state.outlook.low_window?'warn':'guide-line'}">앞으로 6시간 예상 판매 ${n(state.outlook.next6)}개(평소 ${n(state.outlook.usual6)}개) · 24시간 ${n(state.outlook.next24)}개 · ${state.outlook.weekend?'주말':'평일'} 시간대 기준${state.outlook.low_window?' · 지금부터 수요가 적은 시간대라 주문을 한꺼번에 쌓지 마세요':''}</p>${state.outlook.quiet_hours.length?`<p class="help">앞으로 24시간 중 수요가 평소 절반 아래인 시간: ${esc(hourRanges(state.outlook.quiet_hours))}</p>`:''}`:'<p class="help">시간대별 판매 기록(24시간 시장 이력)이 부족해 앞으로의 예상 판매량을 계산하지 못했습니다.</p>'}<p class="help">세 기간 중 가장 느린 하루 판매량이 기준입니다. 하루치보다 많이 사면 다 팔 때까지 하루 넘게 걸려 그사이 다른 판매자가 더 싸게 올릴 수 있고, 3일치를 넘기면 재고가 오래 묶일 위험이 큽니다. 관측 시간이 짧은 기간(24시간은 6시간 미만, 7일·28일은 24시간 미만)은 계산에서 뺍니다.</p></div>`:'');
}
function renderOverview(){
 const items=Object.keys(state.items);
 updateHtml('overview',table(['도시',...items.map(i=>state.items[i])],Object.entries(state.cities).map(([city,name])=>`<tr><th scope="row">${esc(name)}</th>${items.map(item=>{const b=state.books[item][city];return `<td><button type="button" class="market-cell ${item===selectedItem&&city===selectedCity?'selected':''}" data-item="${esc(item)}" data-city="${esc(city)}" aria-label="${esc(name)} ${esc(state.items[item])} 상세"><span>${extremeMark(item,city,'buy')}${quoteText(b.buy,'buy')}</span><span>${extremeMark(item,city,'sell')}${quoteText(b.sell,'sell')}</span>${personalMark(item,city)}</button></td>`}).join('')}</tr>`),'all-markets'));
 $('quick-city').textContent='도시 비교 + '+state.cities[selectedCity]+' 상세';
 updateHtml('all-items',items.map(item=>{const b=state.books[item][selectedCity];return `<article class="panel item-summary"><h3>${esc(state.items[item])}</h3><div class="extremes-pair">${extremeSummary(item,'buy')}${extremeSummary(item,'sell')}</div><div class="item-city-table"><table><thead><tr><th>도시</th><th>최고 매수</th><th>최저 매도</th></tr></thead><tbody>${Object.entries(state.cities).map(([city,name])=>{const pair=state.books[item][city];return `<tr><th scope="row">${esc(name)}</th>${['buy','sell'].map(side=>{const q=quote(pair[side]),best=state.city_extremes?.[item]?.[side]?.observations?.some(o=>o.city===city);return `<td class="quote-cell ${side} ${best?'best-'+side:''}">${n(q?.price)}<small>${n(q?.amount)}개</small><small class="quote-status">${pair[side].single_only?'미확인':q?.stale||pair[side].stale?'지난 자료':q?'유효 관측':'미조회'}</small></td>`}).join('')}</tr>`}).join('')}</tbody></table></div><h4 class="selected-city-label">${esc(state.cities[selectedCity])} 물량 상세</h4>${['buy','sell'].map(side=>{const book=b[side],d=book.fresh?book.analysis_depth:book.depth;return `<div class="item-side ${side}"><p>${quoteText(book,side)}</p><small>${d?.reference?.large?'대량 기준':'소량 제외 기준'} ${n(d?.reference?.price)} · ${n(d?.reference?.amount)}개</small><small>앞쪽 소량 ${n(d?.leading_small_levels)}호가 · ${n(d?.leading_small_amount)}개</small><div class="wall-list">${(d?.large_levels||[]).slice(0,3).map(r=>`<span class="pill ${r.wall?'wall-badge':''}">${r.wall?'벽':'대량'} ${n(r.price)} · ${n(r.amount)}개</span>`).join('')||'대량 관측 없음'}</div></div>`}).join('')}</article>`}).join(''));
}
function render(){
 renderNow();renderPickHistory();renderBookPatterns();renderBoard();renderBuyouts();renderOverview();renderPersonal();renderMailDiagnosis();renderVolumeGuide();
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
 const style=$('candidate-filter').value,listed=state.candidates.filter(r=>style==='all'||(style==='instant')===(r.mode==='instant'));
 updateHtml('candidates',listed.length?table(['품목·방식','사는 곳 / 가격','파는 곳 / 가격','개당 예상 순익','물량·확인할 점'],listed.map(r=>{const instantBuy=r.mode==='instant'||r.mode==='instant_list',reasons=r.reasons.filter(x=>!hidden.includes(x));return `<tr class="${r.mode==='depth'?'conditional':''}"><td>${esc(state.items[r.item])}<br><span class="pill ${instantBuy?'instant-badge':''}">${esc(r.status)}</span></td><td>${esc(state.cities[r.buy_city])}<br>${n(r.buy_price)}<small>${instantBuy?`바로 살 수 있는 잔량 ${n(r.buy_available)}개`:`기준 잔량 ${n(r.buy_reference?.amount)}개`}</small></td><td>${esc(state.cities[r.sell_city])}<br>${n(r.sell_price)}<small>${r.mode==='instant'?`바로 팔 수 있는 잔량 ${n(r.sell_available)}개`:`기준 잔량 ${n(r.sell_reference?.amount)}개`}</small></td><td class="positive">${n(r.net_per_unit)}<br>${n(r.roi*100,1)}%</td><td>${instantBuy?'':`<b>매수 앞 ${n(r.buy_ahead)}개 / 매도 앞 ${n(r.sell_ahead)}개</b>`}<small>관측 7일 평균 ${n(r.history_weighted_average,2)} · 최고 구간 평균 ${n(r.history_highest_bucket_average,2)} · ${n(r.history_amount)}개</small><small>파는 곳 ${esc(guideLine(guideFor(r.item,r.sell_city)))}</small>${reasons.length?`<div class="reason-list">${reasons.map(x=>`<span class="pill reason">${esc(x)}</span>`).join('')}</div>`:''}</td></tr>`}),'candidates'):empty(`최근 ${n(state.settings.fresh_minutes)}분 안에 연 시장 기준으로 이익이 남는 후보가 없습니다. 시장을 다시 열면 바로 계산하고, 지난 추천은 기록 탭에 남아 있습니다.`));
 $('mail').textContent=state.email.enabled?`메일 설정: ${state.email.configured?'등록됨':'설정 미완료'} · ${state.email.times.join(', ')}`:'메일 발송 꺼짐 · 수신 주소·발송 계정·프랑스 기준 발송 시간을 설정하면 사용할 수 있습니다.';
}
function renderBook(side,b){
 const levels=b.levels,d=b.depth||{},s=state.settings;
 if(!levels.length){updateHtml(side,empty('조회 자료 없음 · 게임에서 주문 목록을 열어 주세요.'));return}
 const filter=$('book-filter').value;
 const shown=levels.filter(r=>filter==='all'||(filter==='small'&&r.small)||(filter==='large'&&r.large)||(filter==='meaningful'&&!r.small));
 const ref=d.reference,dominant=d.dominant,liveRef=b.analysis_depth?.reference;
 const freshness=b.single_only?'단일 주문 수신 · 이전 목록 보관 · 최우선 가격 재조회 필요':b.awaiting_scan?'새 조회 대기 · 지난 관측 보관':b.stale?'이전 관측 · 분석 제외':b.mixed_freshness?'최신·이전 호가 혼재 · 이전 호가 분석 제외':'유효 시간 내 관측';
 const large=(d.large_levels||[]).map(r=>`${n(r.price)} · ${n(r.amount)}개`).join(' / ');
 const summary=`<div class="book-summary ${b.stale?'stale-summary':''}"><span class="pill">${esc(freshness)}</span>${b.mixed_freshness?`<p class="warn">최신 호가만의 분석 기준 <strong>${n(liveRef?.price)}</strong> · ${n(liveRef?.amount)}개</p>`:''}<p>${side==='sell'?'관측 최저 매도가':'관측 최고 매수가'} <strong>${n(quote(b)?.price)}</strong> · ${n(quote(b)?.amount)}개</p><p>전체 관측의 물량 기준 가격 (최저가와 다를 수 있음) <strong>${n(ref?.price)}</strong> · ${n(ref?.amount)}개</p><p>인근 최대 잔량 <strong>${n(dominant?.price)}</strong> · ${n(dominant?.amount)}개</p><p class="${d.small_dominated?'warn':''}">소량 호가 ${n(d.small_levels)}개 가격대 (${n(d.small_level_fraction*100,1)}%) · 합계 ${n(d.small_amount)}개<br>맨 앞 소량 ${n(d.leading_small_levels)}개 호가 · 합계 ${n(d.leading_small_amount)}개</p><small>대량 주문 가격: ${esc(large||'기준에 맞는 가격대 없음')}</small>${d.distant_large_levels?.length?`<small>먼 가격의 대량 주문 ${d.distant_large_levels.map(r=>`${n(r.price)} · ${n(r.amount)}개`).join(' / ')} · 기준 가격에서 제외</small>`:''}</div>`;
 const rows=shown.map(r=>`<tr class="${r.wall?'wall':r.large?'large':r.small?'small-lot':''} ${r.stale?'stale-row':''}"><td>${n(r.price)} ${r.wall?'<span class="pill wall-badge">물량 벽</span>':r.large?'<span class="pill">대량</span>':r.small?'<span class="pill small-badge">소량</span>':''}${r.outside_band?'<small>인근 범위 밖</small>':''}</td><td><strong>${n(r.amount)}</strong></td><td>${n(r.cumulative)}</td><td>${n(r.orders)}<small>소량 주문 ${n(r.small_orders)}건</small></td><td>${r.stale?'이전 관측':date(r.oldest_seen)}</td></tr>`);
 updateHtml(side,`<p class="help">${esc(b.coverage)} · ${date(b.seen)}<br>소량 ≤${n(s.small_quantity_max)}개 · 대량 ≥${n(s.large_quantity_min)}개 · 물량 벽 ≥${n(s.wall_quantity_min)}개 또는 주변 중앙 잔량의 ${n(s.wall_multiple)}배</p>${summary}`+(rows.length?table(['가격·구분','주문 잔량','누적 잔량','주문 수','관측 시각'],rows,`${selectedItem}-${selectedCity}-${side}-${filter}`):empty('현재 필터에 맞는 호가가 없습니다.')));
}
function renderHigh(){if(!state)return;const h=state.history[Number($('period').value)],threshold=state.settings.near_high_fraction;$('near-note').textContent=`최고 구간 평균가의 ${n(threshold*100)}% 이상인 구간을 표시합니다. 가격별 실제 체결 수량은 알 수 없습니다.`;updateHtml('high',h.near_high_buckets.length?table(['구간 시작 (프랑스)','구간 끝 (프랑스)','해상도','구간 평균가','구간 전체 판매량'],h.near_high_buckets.map(r=>`<tr><td>${esc(date(r.start))}</td><td>${esc(date(r.end))}</td><td>${r.hours}시간</td><td>${n(r.avg,2)}</td><td>${n(r.amount)}개</td></tr>`),`${selectedItem}-${selectedCity}-high-${$('period').value}`):empty('기간에 맞는 판매 이력이 아직 없습니다.'))}
function renderBars(id,rows,label){
 if(!rows.length){updateHtml(id,empty('1시간 구간 이력이 쌓이면 표시됩니다.'));return}
 const max=Math.max(1,...rows.map(x=>x.per_observed_hour??0));
 updateHtml(id,rows.map(r=>r.observed_hours?`<div class="bar-row"><span>${esc(label(r.key))}</span><div class="bar-track"><div class="bar-fill" style="width:${(r.per_observed_hour??0)/max*100}%"></div></div><small><b>${n(r.total)}개</b>${r.observed_hours}시간 관측 · 시간당 ${n(r.per_observed_hour,1)}</small></div>`:`<div class="bar-row missing"><span>${esc(label(r.key))}</span><div class="bar-track"></div><small>관측 없음</small></div>`).join(''));
}
function fillSettings(){const f=$('settings');if(!$('excluded-cities').children.length)$('excluded-cities').innerHTML=Object.entries(state.cities).map(([id,name])=>`<label class="check"><input type="checkbox" name="excluded_city" value="${esc(id)}">${esc(name)}</label>`).join('');const excluded=draft?.excluded_cities||state.settings.excluded_cities||[];for(const box of $('excluded-cities').querySelectorAll('input'))box.checked=excluded.includes(box.value);for(const [k,v]of Object.entries(state.settings)){if(!f.elements[k])continue;if(typeof v==='boolean')f.elements[k].checked=v;else f.elements[k].value=percentKeys.includes(k)?Math.round(v*10000)/100:v}if(draft){for(const [k,v]of Object.entries(draft)){const el=f.elements[k];if(el&&k!=='excluded_cities'){if(el.type==='checkbox')el.checked=v;else el.value=v}}$('saved').textContent='이전 입력을 복원했습니다.'}}
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
function showTab(name){if(!tabs.some(b=>b.dataset.tab===name))name='cities';for(const b of tabs){const on=b.dataset.tab===name;b.classList.toggle('active',on);b.setAttribute('aria-selected',String(on))}for(const panel of document.querySelectorAll('.tab-panel'))panel.hidden=panel.dataset.tab!==name;storage.set('tab',name)}
document.querySelector('.tabs').onclick=e=>{const b=e.target.closest('[data-tab]');if(b)showTab(b.dataset.tab)};
showTab(storage.get('tab','cities'));
$('city-board').onclick=e=>{const button=e.target.closest('[data-city]');if(button){$('city').value=button.dataset.city;chooseView();showTab('market');document.querySelector('.tabs').scrollIntoView({block:'start'})}};
$('cities').onclick=e=>{const card=e.target.closest('[data-city]');if(card){$('city').value=card.dataset.city;chooseView()}};
$('candidate-filter').value=['all','instant','wait'].includes(storage.get('candidateFilter','all'))?storage.get('candidateFilter','all'):'all';
$('candidate-filter').onchange=()=>{storage.set('candidateFilter',$('candidate-filter').value);if(state)render()};
$('period').onchange=()=>{rememberView();preserveViewport(renderHigh)};
$('book-filter').onchange=()=>{rememberView();if(state)preserveViewport(()=>{renderBook('sell',state.books[selectedItem][selectedCity].sell);renderBook('buy',state.books[selectedItem][selectedCity].buy)})};
$('settings').addEventListener('input',queueDraft);$('settings').addEventListener('change',queueDraft);
$('settings').onsubmit=e=>{e.preventDefault();clearTimeout(saveTimer);saveSettings()};
window.addEventListener('scroll',()=>{if(!restorePending){clearTimeout(scrollTimer);scrollTimer=setTimeout(rememberView,120)}},{passive:true});
window.addEventListener('pagehide',()=>{if(ready){rememberView();if(draft)storage.set('settingsDraft',readDraft())}});
$('reset').onclick=async()=>{try{await post('/api/reset',{item:selectedItem,city:selectedCity});await refresh();$('last').textContent='새 조회 시작됨 · 게임에서 양쪽 주문 목록을 다시 열어 주세요.'}catch(e){$('last').textContent='새 조회 시작 실패: '+e.message}};
// A hidden tab does not poll; the server rebuilds every market on each request.
refresh();setInterval(()=>{if(document.hidden)return;refresh();if(draft&&!saving)scheduleSave()},5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
