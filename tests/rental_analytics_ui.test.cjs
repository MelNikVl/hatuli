const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../static/hatuli-rental-analytics.js'),'utf8');
const makeFeature = period => ({geometry:{coordinates:[[[71.43,51.128],[71.431,51.128],[71.43,51.129],[71.43,51.128]]]},properties:{rent_period:period,median_price:period==='daily'?20000:250000,p25_price:18000,p75_price:24000,sample_count:5,active_supply:3,window_days:90,source_newest_at:'16.10.2026 UTC'}});
let map, popup, changed, sourceData;
const selector = {value:'daily',addEventListener(event,handler){changed=handler;}};
class Map {
  constructor(){map=this;this.events={};this.layers={};}
  addControl(){}
  on(name,a,b){this.events[name]=typeof a==='function'?a:b;}
  getSource(){return sourceData;}
  addSource(name,value){sourceData={data:value.data,setData(data){this.data=data;}};}
  getLayer(name){return this.layers[name];}
  addLayer(value){this.layers[value.id]=value;}
  setPaintProperty(){}
  fitBounds(){}
  isStyleLoaded(){return true;}
}
class Bounds {extend(){this.any=true;return this;}isEmpty(){return !this.any;}}
class Popup {setLngLat(){return this;}setDOMContent(element){popup=element;return this;}addTo(){return this;}}
const layers={daily:{type:'FeatureCollection',features:[makeFeature('daily')]},monthly:{type:'FeatureCollection',features:[makeFeature('monthly')]}};
vm.runInNewContext(source,{document:{getElementById(id){return id==='rental-market-data'?{textContent:JSON.stringify(layers)}:id==='rental-layer-select'?selector:{};},createElement(){return {};}},maplibregl:{Map,LngLatBounds:Bounds,Popup,NavigationControl:class{}}});
map.events.load();
assert.equal(sourceData.data.features[0].properties.rent_period,'daily');
selector.value='monthly';changed();
assert.equal(sourceData.data.features[0].properties.rent_period,'monthly');
map.events.click({features:[makeFeature('daily')],lngLat:{}});
assert.match(popup.textContent,/₸\/сут/);
assert.match(popup.textContent,/n=5.*окно 90 дн/);
assert.match(popup.textContent,/Источник:/);
assert.equal(popup.innerHTML,undefined);
vm.runInNewContext(source,{document:{getElementById(){return null;}}});
console.log('Rental analytics: separate price units, layer selection, source/window/n and safe popup passed.');
