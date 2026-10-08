(async()=>{
'use strict';
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const map=L.map('map',{zoomControl:false,maxZoom:19}).setView([51.128,71.43],14);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,referrerPolicy:'origin',attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}).addTo(map);
const status=document.getElementById('status'),cards=document.getElementById('cards'),markers=[];
const money=v=>v?new Intl.NumberFormat('ru-RU').format(v)+' ₸':'Цена не указана';
function fit(){if(!markers.length)return;map.fitBounds(L.featureGroup(markers).getBounds(),{paddingTopLeft:[40,document.querySelector('header').offsetHeight+35],paddingBottomRight:[40,cards.offsetHeight+45],maxZoom:16});}
document.getElementById('fit').onclick=fit;
try{
 const ids=new URLSearchParams(location.search).get('ids')||'';
 const response=await fetch('/buyer/map/api/nearby?'+new URLSearchParams({ids}));
 const data=await response.json();if(!response.ok)throw Error(data.detail||'Не удалось загрузить карту.');
 data.items.forEach(item=>{
  const isBase=item.index===0,title=isBase?'Исходная квартира':`Вариант ${item.index}`;
  const info=[item.rooms?`${item.rooms} комн.`:null,item.area?`${item.area} м²`:null,item.distance_m!==null?`${item.distance_m} м от исходной`:null,item.is_active===false?'Снято с публикации':null].filter(Boolean).join(' · ');
  const marker=L.marker([item.lat,item.lon],{icon:L.divIcon({className:'pin'+(isBase?' base':''),html:isBase?'●':String(item.index),iconSize:[36,36],iconAnchor:[18,18]})}).addTo(map);markers.push(marker);
  const popup=document.createElement('div'),heading=document.createElement('strong'),details=document.createElement('div'),address=document.createElement('div'),link=document.createElement('a');
  heading.textContent=title+' · '+money(item.price);details.textContent=info;address.textContent=item.complex_name||item.address||'';link.textContent='Открыть объявление';link.href='https://krisha.kz/a/show/'+item.id;link.target='_blank';link.rel='noopener noreferrer';popup.append(heading,details,address,link);marker.bindPopup(popup);
  const card=document.createElement('div'),button=document.createElement('button'),label=document.createElement('strong'),sub=document.createElement('span');card.className='card';label.textContent=title+' · '+money(item.price);sub.textContent=info;button.append(label,sub);button.onclick=()=>{map.panTo(marker.getLatLng());marker.openPopup();};card.append(button,link.cloneNode(true));cards.append(card);
  marker.on('popupopen',()=>{cards.querySelectorAll('.card').forEach(c=>c.classList.remove('selected'));card.classList.add('selected');card.scrollIntoView({block:'nearest'});});
 });
 status.textContent=data.items.length?(data.missing?'Часть объявлений недоступна. Нажмите метку для подробностей.':'Нажмите метку или квартиру в списке.'):'Объявления недоступны. Пришлите ссылку боту для нового подбора.';
 fit();
}catch(error){status.textContent=error.message;}
})();
