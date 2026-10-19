(() => {
  'use strict';
  const payload = document.getElementById('rental-market-data');
  const container = document.getElementById('rental-market-map');
  if (!payload || !container || typeof maplibregl === 'undefined') return;
  let layers;
  try { layers = JSON.parse(payload.textContent); } catch (_) { return; }
  const select = document.getElementById('rental-layer-select');
  const keys = Object.keys(layers).filter(key => ['monthly', 'daily'].includes(key) && layers[key].features.length);
  if (!keys.length) return;
  const map = new maplibregl.Map({container, style:'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json', center:[71.430,51.128], zoom:12});
  map.addControl(new maplibregl.NavigationControl(), 'top-right');
  const draw = () => {
    const period = keys.includes(select?.value) ? select.value : keys[0];
    const data = layers[period];
    if (!map.getSource('rental-index')) map.addSource('rental-index', {type:'geojson',data});
    else map.getSource('rental-index').setData(data);
    const prices = data.features.map(f => f.properties.median_price).filter(Number.isFinite);
    const lo = Math.min(...prices), hi = Math.max(lo + 1, ...prices);
    const color = ['interpolate',['linear'],['get','median_price'],lo,'#1d9cba',hi,'#f2b46a'];
    if (!map.getLayer('rental-price')) {
      map.addLayer({id:'rental-price',type:'fill',source:'rental-index',paint:{'fill-color':color,'fill-opacity':0.68,'fill-outline-color':'#83b9ca'}});
    } else map.setPaintProperty('rental-price','fill-color',color);
    const bounds = new maplibregl.LngLatBounds();
    data.features.forEach(f => f.geometry.coordinates[0].forEach(point => bounds.extend(point)));
    if (!bounds.isEmpty()) map.fitBounds(bounds,{padding:35,maxZoom:15,duration:0});
  };
  map.on('load',draw);
  select?.addEventListener('change',() => { if (map.isStyleLoaded()) draw(); });
  map.on('click','rental-price',event => {
    const p = event.features?.[0]?.properties;
    if (!p) return;
    const text = document.createElement('div');
    text.className = 'rental-map-popup';
    text.textContent = `${Number(p.median_price).toLocaleString('ru-RU')} ₸/${p.rent_period === 'daily' ? 'сут' : 'мес'}\nP25–P75: ${Number(p.p25_price).toLocaleString('ru-RU')}–${Number(p.p75_price).toLocaleString('ru-RU')} ₸\nn=${p.sample_count} · окно ${p.window_days} дн. · активно ${p.active_supply}\nНаблюдения: ${p.source_oldest_at}\nИсточник: ${p.source_newest_at}`;
    new maplibregl.Popup().setLngLat(event.lngLat).setDOMContent(text).addTo(map);
  });
})();
