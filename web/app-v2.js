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
const numberKeys=['fresh_minutes','transport_per_unit','small_quantity_max','large_quantity_min','wall_quantity_min','wall_multiple'];
const boolKeys=['fees_confirmed','transport_confirmed'];
const fragmentCache=new Map(),innerScroll=storage.get('tableScroll',{});
const date=v=>v?new Intl.DateTimeFormat('ko-KR',{timeZone:state?.timezone||'Europe/Paris',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZoneName:'shortOffset'}).format(new Date(v)):'미조회';
const empty=text=>`<div class="empty">${esc(text)}</div>`;
function table(headers,rows,key){return `<div class="table-wrap" data-scroll-key="${esc(key)}" tabindex="0" aria-label="${esc(key)} 표 스크롤"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
function rememberView(){storage.set('view',{item:selectedItem,city:selectedCity,period:$('period').value,bookFilter:$('book-filter').value,settingsOpen:$('settings-panel').open,scrollY:window.scrollY})}
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
 const anchor=[...document.querySelectorAll('main > section, main > details, .toolbar')].find(el=>{const r=el.getBoundingClientRect();return r.top<=64&&r.bottom>64});
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
  const legacy=next.build!=='20261025.7';
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
   $('settings-panel').open=Boolean(savedView.settingsOpen);fillSettings();ready=true;
  }
  preserveViewport(()=>{
   const packetAge=state.last_packet?.received?Math.max(0,(Date.now()-Date.parse(state.last_packet.received))/60000):null;
   $('capture-warning').hidden=state.capture?.running!==false;
   $('connection').textContent=state.capture?.running===false?'수집기 꺼짐 · 지난 자료만 표시':!state.packets?'저장 프로그램 연결됨 · 첫 자료 대기':packetAge<=state.settings.fresh_minutes?'저장 프로그램 연결됨 · 최근 자료 수신':`저장 프로그램 연결됨 · ${n(packetAge)}분간 새 자료 없음`;
   $('last').textContent=`마지막 자료 ${date(state.last_packet?.received)} · ${n(state.packets)}회 수신`;
   $('auto-update').textContent=state.auto_update?.enabled?state.auto_update.status:'자동 반영 준비 · 분석 프로그램을 한 번 다시 실행해 주세요.';
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
 const market=sales.by_market[selectedItem+'/'+selectedCity];
 $('personal-context').textContent=state.cities[selectedCity]+' · '+state.items[selectedItem];
 const notices=(sales.conflicts?`<p class="warn">내용이 달라진 메일 ${n(sales.conflicts)}건은 합계에서 제외했습니다.</p>`:'')+(sales.unknown_identity?'<p class="help">캐릭터 식별 정보가 없는 메일이 있습니다. 여러 캐릭터 사용 시 중복 구분에 한계가 있습니다.</p>':'');
 updateHtml('personal-sales',market?`<div class="personal-metrics"><div><small>내 판매 메일 · 누적 최고 단가</small><b>${n(market.highest_price)}</b><span>이 단가의 수량 ${n(market.highest_price_amount)}개</span></div><div><small>확인한 판매 수량</small><b>${n(market.total_amount)}<span>개</span></b><span>중복 제외 ${n(market.mail_count)}개 메일</span></div></div>`+table(['메일 단가','판매 수량','처음 확인한 때'],sales.selected.map(r=>`<tr><td>${n(r.price)}</td><td>${n(r.amount)}개</td><td>${date(r.first_seen)}</td></tr>`),'personal-sales-detail')+`<p class="help">${n(sales.selected.length)} / ${n(sales.selected_count)}개 메일 표시 · ${esc(sales.note)}</p>`+notices:empty('이 도시·품목의 판매 완료 메일은 아직 없습니다. 수집기 연결 후 게임에서 해당 메일을 열면 여기에 쌓입니다.')+`<p class="help">${esc(sales.note)}</p>`+notices);
}
function renderOverview(){
 const items=Object.keys(state.items);
 updateHtml('overview',table(['도시',...items.map(i=>state.items[i])],Object.entries(state.cities).map(([city,name])=>`<tr><th scope="row">${esc(name)}</th>${items.map(item=>{const b=state.books[item][city];return `<td><button type="button" class="market-cell ${item===selectedItem&&city===selectedCity?'selected':''}" data-item="${esc(item)}" data-city="${esc(city)}" aria-label="${esc(name)} ${esc(state.items[item])} 상세"><span>${extremeMark(item,city,'buy')}${quoteText(b.buy,'buy')}</span><span>${extremeMark(item,city,'sell')}${quoteText(b.sell,'sell')}</span>${personalMark(item,city)}</button></td>`}).join('')}</tr>`),'all-markets'));
 $('quick-city').textContent='도시 비교 + '+state.cities[selectedCity]+' 상세';
 updateHtml('all-items',items.map(item=>{const b=state.books[item][selectedCity];return `<article class="panel item-summary"><h3>${esc(state.items[item])}</h3><div class="extremes-pair">${extremeSummary(item,'buy')}${extremeSummary(item,'sell')}</div><div class="item-city-table"><table><thead><tr><th>도시</th><th>최고 매수</th><th>최저 매도</th></tr></thead><tbody>${Object.entries(state.cities).map(([city,name])=>{const pair=state.books[item][city];return `<tr><th scope="row">${esc(name)}</th>${['buy','sell'].map(side=>{const q=quote(pair[side]),best=state.city_extremes?.[item]?.[side]?.observations?.some(o=>o.city===city);return `<td class="quote-cell ${side} ${best?'best-'+side:''}">${n(q?.price)}<small>${n(q?.amount)}개</small><small class="quote-status">${pair[side].single_only?'미확인':q?.stale||pair[side].stale?'지난 자료':q?'유효 관측':'미조회'}</small></td>`}).join('')}</tr>`}).join('')}</tbody></table></div><h4 class="selected-city-label">${esc(state.cities[selectedCity])} 물량 상세</h4>${['buy','sell'].map(side=>{const book=b[side],d=book.fresh?book.analysis_depth:book.depth;return `<div class="item-side ${side}"><p>${quoteText(book,side)}</p><small>${d?.reference?.large?'대량 기준':'소량 제외 기준'} ${n(d?.reference?.price)} · ${n(d?.reference?.amount)}개</small><small>앞쪽 소량 ${n(d?.leading_small_levels)}호가 · ${n(d?.leading_small_amount)}개</small><div class="wall-list">${(d?.large_levels||[]).slice(0,3).map(r=>`<span class="pill ${r.wall?'wall-badge':''}">${r.wall?'벽':'대량'} ${n(r.price)} · ${n(r.amount)}개</span>`).join('')||'대량 관측 없음'}</div></div>`}).join('')}</article>`}).join(''));
}
function render(){
 renderOverview();renderPersonal();
 $('detail-context').textContent=state.cities[selectedCity]+' · '+state.items[selectedItem];
 const books=state.books[selectedItem];
 updateHtml('cities',Object.entries(state.cities).map(([id,name])=>{
  const b=books[id],sell=quote(b.sell),buy=quote(b.buy),sr=b.sell.depth?.reference,br=b.buy.depth?.reference;
  return `<button type="button" class="city-card ${id===selectedCity?'active':''}" data-city="${id}" aria-pressed="${id===selectedCity}"><h3>${esc(name)}</h3><div class="price-pair"><div><span>${b.buy.single_only?'매수 관측 · 최고가 미확인':'최고 매수'}</span><br><b>${n(buy?.price)}</b><small>${n(buy?.amount)}개 ${buy?.stale||b.buy.stale?'· 이전 관측':''}</small></div><div><span>${b.sell.single_only?'매도 관측 · 최저가 미확인':'최저 매도'}</span><br><b>${n(sell?.price)}</b><small>${n(sell?.amount)}개 ${sell?.stale||b.sell.stale?'· 이전 관측':''}</small></div></div><div class="reference-mini">관측 물량 기준 매수 ${n(br?.price)} / 매도 ${n(sr?.price)}</div><div class="age">매수 ${date(b.buy.seen)}<br>매도 ${date(b.sell.seen)}</div></button>`
 }).join(''));
 renderBook('sell',books[selectedCity].sell);renderBook('buy',books[selectedCity].buy);
 updateHtml('history',state.history.map(h=>`<article class="history-card"><h3>최근 ${h.days===1?'24시간':h.days+'일'}</h3><div class="metric">${n(h.amount)} <small>개 · 관측 판매량</small></div><div class="stats"><div><small>수량 가중평균가</small><b>${n(h.weighted_average,2)}</b></div><div><small>최고 구간 평균가</small><b>${n(h.highest_bucket_average,2)}</b></div><div><small>고가 구간 전체 판매량</small><b>${n(h.near_high_amount)}개</b></div><div><small>관측 시간 범위</small><b>${n(h.coverage*100,1)}%</b></div></div><div class="coverage">${n(h.covered_hours)} / ${n(h.requested_hours)}시간 확인 · ${n(h.resolution_hours)}시간 구간<br>최신 구간 끝 ${date(h.newest_bucket_end)}${h.stale?'<br><strong class="warn">최신 구간이 오래됨 · 시장 이력을 다시 열어 주세요.</strong>':''}</div></article>`).join(''));
 renderHigh();renderBars('weekdays',state.patterns.weekdays,x=>['월','화','수','목','금','토','일'][x]);renderBars('hours',state.patterns.hourly,x=>String(x).padStart(2,'0')+'시');$('pattern-note').textContent=state.patterns.note;
 updateHtml('candidates',state.candidates.length?table(['품목·분석 기준','매수 도시 / 주문가','매도 도시 / 주문가','개당 예상 순익','앞선 물량·조건'],state.candidates.map(r=>`<tr class="${r.mode==='depth'?'conditional':''}"><td>${esc(state.items[r.item])}<br><span class="pill">${esc(r.status)}</span></td><td>${esc(state.cities[r.buy_city])}<br>${n(r.buy_price)}<small>기준 잔량 ${n(r.buy_reference?.amount)}개</small></td><td>${esc(state.cities[r.sell_city])}<br>${n(r.sell_price)}<small>기준 잔량 ${n(r.sell_reference?.amount)}개</small></td><td class="positive">${n(r.net_per_unit)}<br>${n(r.roi*100,1)}%</td><td><b>매수 앞 ${n(r.buy_ahead)}개 / 매도 앞 ${n(r.sell_ahead)}개</b><small>관측 7일 평균 ${n(r.history_weighted_average,2)} · 최고 구간 평균 ${n(r.history_highest_bucket_average,2)} · ${n(r.history_amount)}개</small><small>${r.reasons.map(esc).join('<br>')}</small></td></tr>`),'candidates'):empty('현재 조건을 만족하는 검토 후보 없음 · 자료 부족 또는 예상 차익 부족'));
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
function fillSettings(){const f=$('settings');for(const [k,v]of Object.entries(state.settings)){if(!f.elements[k])continue;if(typeof v==='boolean')f.elements[k].checked=v;else f.elements[k].value=percentKeys.includes(k)?Math.round(v*10000)/100:v}if(draft){for(const [k,v]of Object.entries(draft)){const el=f.elements[k];if(el){if(el.type==='checkbox')el.checked=v;else el.value=v}}$('saved').textContent='이전 입력을 복원했습니다.'}}
function readDraft(){const f=$('settings'),value={};for(const key of [...percentKeys,...numberKeys,...boolKeys]){const el=f.elements[key];value[key]=el.type==='checkbox'?el.checked:el.value}return value}
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
 const payload={};for(const key of percentKeys)payload[key]=Number(values[key])/100;for(const key of numberKeys)payload[key]=Number(values[key]);for(const key of boolKeys)payload[key]=values[key];
 const revision=formVersion;saving=true;$('saved').textContent='자동 저장 중…';
 try{await post('/api/settings',payload);if(revision===formVersion){draft=null;storage.set('settingsDraft',null);$('saved').textContent='자동 저장 완료 · '+new Intl.DateTimeFormat('ko-KR',{timeZone:'Europe/Paris',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(new Date())}await refresh()}
 catch(e){$('saved').textContent='서버 저장 실패 · 입력은 이 브라우저에 보관됨: '+e.message}
 finally{saving=false;if(saveQueued||revision!==formVersion){saveQueued=false;scheduleSave()}}
}
function chooseView(){selectedItem=$('item').value;selectedCity=$('city').value;viewVersion++;rememberView();refresh()}
$('item').onchange=chooseView;$('city').onchange=chooseView;
$('overview').onclick=e=>{const cell=e.target.closest('[data-item][data-city]');if(cell){$('item').value=cell.dataset.item;$('city').value=cell.dataset.city;chooseView()}};
$('cities').onclick=e=>{const card=e.target.closest('[data-city]');if(card){$('city').value=card.dataset.city;chooseView()}};
$('period').onchange=()=>{rememberView();preserveViewport(renderHigh)};
$('book-filter').onchange=()=>{rememberView();if(state)preserveViewport(()=>{renderBook('sell',state.books[selectedItem][selectedCity].sell);renderBook('buy',state.books[selectedItem][selectedCity].buy)})};
$('settings').addEventListener('input',queueDraft);$('settings').addEventListener('change',queueDraft);
$('settings').onsubmit=e=>{e.preventDefault();clearTimeout(saveTimer);saveSettings()};
$('settings-panel').addEventListener('toggle',()=>{if(ready)rememberView()});
window.addEventListener('scroll',()=>{if(!restorePending){clearTimeout(scrollTimer);scrollTimer=setTimeout(rememberView,120)}},{passive:true});
window.addEventListener('pagehide',()=>{if(ready){rememberView();if(draft)storage.set('settingsDraft',readDraft())}});
$('reset').onclick=async()=>{try{await post('/api/reset',{item:selectedItem,city:selectedCity});await refresh();$('last').textContent='새 조회 시작됨 · 게임에서 양쪽 주문 목록을 다시 열어 주세요.'}catch(e){$('last').textContent='새 조회 시작 실패: '+e.message}};
refresh();setInterval(()=>{refresh();if(draft&&!saving)scheduleSave()},5000);
