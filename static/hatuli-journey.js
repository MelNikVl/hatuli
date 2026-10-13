(function(){
  'use strict';
  const text=(tag,value,cls)=>{const node=document.createElement(tag);node.textContent=String(value??'');if(cls)node.className=cls;return node;};
  const money=value=>typeof value==='number'&&Number.isFinite(value)?new Intl.NumberFormat('ru-RU').format(value)+' ₸':'Цена неизвестна';
  const square=value=>typeof value==='number'&&Number.isFinite(value)?new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(value):'?';
  function sourceUrl(value){try{const u=new URL(value);return u.protocol==='https:'&&['krisha.kz','www.krisha.kz'].includes(u.hostname)&&/^\/a\/show\/\d{5,20}\/?$/.test(u.pathname)&&!u.username&&!u.password&&!u.port?u.href:null;}catch{return null;}}
  function listingLink(id,label){const a=text('a',label);if(/^\d{5,20}$/.test(String(id)))a.href='/listing/'+id;return a;}
  function render(container,data){
    if(!container?.isConnected)return;
    container.replaceChildren(text('h3','История квартиры'));
    if(!data?.found){container.append(text('p',data?.message||'Разбор временно недоступен. Основная карточка и источник доступны.'));return;}
    if(data.source_check&&!data.source_check.price_matches){container.append(text('p',`Цена на Krisha при проверке: ${money(data.source_check.price)}. В Hatuli сохранено ${money(data.source_check.stored_price)}. Разбор использует сохранённую цену — подтвердите актуальную перед решением.`,'hj-differences'));}
    if(data.source_check?.active===false){container.append(text('p','При последней проверке объявление было снято. Наличие нужно уточнить.','hj-differences'));}
    const publications=data.publications||{};
    container.append(text('p',publications.message||'История повторных публикаций пока не подтверждена.','hj-note'));
    if(publications.count>1){const list=document.createElement('ul');for(const item of publications.items||[]){const li=text('li',`${item.rooms||'?'} комн. · ${square(item.area)} м² · ${money(item.price)} · `);li.append(listingLink(item.id,'Публикация '+item.id));list.append(li);}container.append(list);}
    for(const difference of publications.discrepancies||[]){const section=text('div','','hj-differences');section.append(text('strong',difference.label+': данные публикаций отличаются'));const list=document.createElement('ul');for(const observed of difference.observations||[]){const li=text('li',`${difference.field==='area'?square(observed.value):observed.value}${difference.field==='area'?' м²':''} · ${observed.observed_at?observed.observed_at.slice(0,10):'дата неизвестна'} · `);const url=sourceUrl(observed.url);if(url){const a=text('a','Источник');a.href=url;a.target='_blank';a.rel='noopener noreferrer';li.append(a);}list.append(li);}section.append(list,text('p',publications.identity_confirmed?'Уточните правильное значение у продавца.':'Сначала нужно подтвердить, что публикации относятся к одной квартире.'));container.append(section);}
    container.append(text('h3','Альтернативы рядом','hj-alternatives-title'),text('p',data.alternatives_message));
    const grid=text('div','','hj-alternatives');
    for(const item of (data.alternatives||[]).slice(0,3)){const card=text('article','','hj-alternative');card.append(text('span',item.category==='better'?'Есть преимущества':'Похожий вариант','hj-label'),text('h4',`${item.rooms||'?'} комн. · ${square(item.area)} м²`),text('strong',money(item.price),'hj-price'));if(Number.isFinite(item.distance_m))card.append(text('p',Math.round(item.distance_m)+' м от выбранной квартиры'));for(const [label,values] of [['Плюсы',item.advantages],['Что учесть',item.tradeoffs],['Надёжность данных',item.warnings]]){if(!values?.length)continue;card.append(text('strong',label,'hj-label'));const list=document.createElement('ul');for(const value of values.slice(0,3))list.append(text('li',value));card.append(list);}card.append(listingLink(item.id,'Разобрать вариант →'));const url=sourceUrl(item.url);if(url){const p=document.createElement('p'),a=text('a','Проверить на Krisha ↗');a.href=url;a.target='_blank';a.rel='noopener noreferrer';p.append(a);card.append(p);}grid.append(card);}
    if(grid.children.length)container.append(grid);
  }
  async function load(container,id){if(!container||!/^\d{5,20}$/.test(String(id)))return;try{const response=await fetch('/admin/api/buyer-journey/'+encodeURIComponent(id),{credentials:'same-origin'});const data=await response.json();render(container,response.ok?data:{found:false,message:data.message||'Разбор доступен после получения расширенного доступа.'});}catch{render(container,{found:false,message:'Разбор не удалось загрузить. Попробуйте открыть карточку ещё раз.'});}}
  window.HatuliBuyerJourney={load,render};
})();
