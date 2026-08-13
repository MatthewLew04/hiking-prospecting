#!/usr/bin/env node
/* WS11 browser acceptance and hotfix-budget measurement.

   This launches the checked-in app through the Range-capable local server,
   exercises every state switch and the heaviest tiled claim view, and exits
   nonzero on a browser, delivery, heap, or origin-storage regression. The
   large national layers must stay PMTiles sources; external basemap requests
   are replaced by a one-pixel PNG so CI is deterministic and offline apart
   from its local HTTP server. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const {zxyToTileId} = require('pmtiles');

const ROOT = path.resolve(__dirname, '..');
const BUDGETS = JSON.parse(fs.readFileSync(path.join(ROOT, 'ci', 'budgets.json')));
const PORT = Number(process.env.NWMM_ACCEPTANCE_PORT || 8765);
const BASE = `http://127.0.0.1:${PORT}`;
const EXPECTED_STATES = [
  'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','ID','IL','IN','IA','KS','KY',
  'LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC',
  'ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
];
const CLAIM_STATE_CODES = new Set([
  'AK','AZ','AR','CA','CO','FL','ID','LA','MS','MT','NE','NV','NM','ND','OR','SD','UT','WA','WY',
]);
const DARK_PX = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64');

// Build a deterministic, dependency-free PMTiles v3 fixture in the test's
// private temp directory. This is deliberately tiny, but it is a real vector
// archive: MapLibre exercises HTTP Range delivery, protocol/cache lifecycle,
// MVT decoding, state filters, source queries, popups, and tile-scoped search.
function unsignedVarint(value) {
  let n=BigInt(value), bytes=[];
  if(n<0n)throw new Error(`negative unsigned varint ${value}`);
  do{
    let byte=Number(n&127n);n>>=7n;
    if(n)byte|=128;bytes.push(byte);
  }while(n);
  return Buffer.from(bytes);
}
function pbfBytes(field,value){
  const body=Buffer.isBuffer(value)?value:Buffer.from(value);
  return Buffer.concat([unsignedVarint((field<<3)|2),unsignedVarint(body.length),body]);
}
function pbfInteger(field,value){
  return Buffer.concat([unsignedVarint(field<<3),unsignedVarint(value)]);
}
function mvtValue(value){
  return typeof value==='number'&&Number.isSafeInteger(value)&&value>=0
    ?pbfInteger(4,value):pbfBytes(1,String(value));
}
function zigzag(value){return value<0?-2*value-1:2*value;}
function mvtGeometry(type,cx,cy){
  if(type==='point')return {type:1,commands:[9,zigzag(cx),zigzag(cy)]};
  if(type==='line')return {type:2,commands:[
    9,zigzag(cx-110),zigzag(cy),10,zigzag(220),zigzag(0),
  ]};
  if(type==='polygon')return {type:3,commands:[
    9,zigzag(cx-90),zigzag(cy-90),26,
    zigzag(180),zigzag(0),zigzag(0),zigzag(180),
    zigzag(-180),zigzag(0),15,
  ]};
  throw new Error(`unsupported synthetic geometry ${type}`);
}
function mvtLayer(layer,cx,cy){
  const keys=Object.keys(layer.properties),values=Object.values(layer.properties);
  const tags=[];
  for(let index=0;index<keys.length;index++)tags.push(index,index);
  const geometry=mvtGeometry(layer.geometry,cx,cy);
  const feature=Buffer.concat([
    pbfInteger(1,layer.properties.fid),
    pbfBytes(2,Buffer.concat(tags.map(unsignedVarint))),
    pbfInteger(3,geometry.type),
    pbfBytes(4,Buffer.concat(geometry.commands.map(unsignedVarint))),
  ]);
  return Buffer.concat([
    pbfBytes(1,layer.id),pbfBytes(2,feature),
    ...keys.map(key=>pbfBytes(3,key)),
    ...values.map(value=>pbfBytes(4,mvtValue(value))),
    pbfInteger(5,4096),pbfInteger(15,2),
  ]);
}
function mvtTile(layers,cx,cy){
  return Buffer.concat(layers.map(layer=>pbfBytes(3,mvtLayer(layer,cx,cy))));
}
function tilePosition(zoom,lng,lat){
  const scale=2**zoom,worldX=(lng+180)/360*scale;
  const radians=lat*Math.PI/180;
  const worldY=(1-Math.asinh(Math.tan(radians))/Math.PI)/2*scale;
  return {x:Math.floor(worldX),y:Math.floor(worldY),
    cx:Math.round((worldX-Math.floor(worldX))*4096),
    cy:Math.round((worldY-Math.floor(worldY))*4096)};
}
function pmtilesDirectory(entries){
  const sections=[unsignedVarint(entries.length)];
  let previous=0;
  for(const entry of entries){
    sections.push(unsignedVarint(entry.tileId-previous));previous=entry.tileId;
  }
  for(const entry of entries)sections.push(unsignedVarint(entry.runLength));
  for(const entry of entries)sections.push(unsignedVarint(entry.length));
  for(let index=0;index<entries.length;index++){
    const entry=entries[index],prior=entries[index-1];
    sections.push(unsignedVarint(index&&entry.offset===prior.offset+prior.length
      ?0:entry.offset+1));
  }
  return Buffer.concat(sections);
}
function writeUint64(buffer,offset,value){
  buffer.writeBigUInt64LE(BigInt(value),offset);
}
function syntheticPmtiles(layers){
  const center=[-111.9,39.3],tiles=[];
  for(const zoom of [5,6]){
    const position=tilePosition(zoom,...center);
    tiles.push({tileId:zxyToTileId(zoom,position.x,position.y),
      data:mvtTile(layers,position.cx,position.cy)});
  }
  tiles.sort((a,b)=>a.tileId-b.tileId);
  let tileOffset=0;
  const directory=pmtilesDirectory(tiles.map(tile=>{
    const entry={tileId:tile.tileId,runLength:1,length:tile.data.length,
      offset:tileOffset};tileOffset+=tile.data.length;return entry;
  }));
  const metadata=Buffer.from(JSON.stringify({name:'Synthetic Utah state survey',
    vector_layers:layers.map(layer=>({id:layer.id,fields:Object.fromEntries(
      Object.entries(layer.properties).map(([key,value])=>
        [key,typeof value==='number'?'Number':'String']))}))}));
  const header=Buffer.alloc(127);header.write('PMTiles',0,'ascii');header[7]=3;
  const rootOffset=127,metadataOffset=rootOffset+directory.length;
  const tileDataOffset=metadataOffset+metadata.length;
  writeUint64(header,8,rootOffset);writeUint64(header,16,directory.length);
  writeUint64(header,24,metadataOffset);writeUint64(header,32,metadata.length);
  writeUint64(header,40,tileDataOffset);writeUint64(header,48,0);
  writeUint64(header,56,tileDataOffset);writeUint64(header,64,tileOffset);
  writeUint64(header,72,tiles.length);writeUint64(header,80,tiles.length);
  writeUint64(header,88,tiles.length);
  header[96]=1;header[97]=1;header[98]=1;header[99]=1;
  header[100]=5;header[101]=6;
  const bounds=[-114.05287,36.99766,-109.04157,42.0017];
  header.writeInt32LE(Math.round(bounds[0]*1e7),102);
  header.writeInt32LE(Math.round(bounds[1]*1e7),106);
  header.writeInt32LE(Math.round(bounds[2]*1e7),110);
  header.writeInt32LE(Math.round(bounds[3]*1e7),114);
  header[118]=6;header.writeInt32LE(Math.round(center[0]*1e7),119);
  header.writeInt32LE(Math.round(center[1]*1e7),123);
  return Buffer.concat([header,directory,metadata,...tiles.map(tile=>tile.data)]);
}
function createUtahBrowserFixture(directory){
  const bounds=[-114.05287,36.99766,-109.04157,42.0017];
  const stateFilter=['==',['get','st'],'UT'];
  const common=(fid,dataset,sourceId,record,scale,publication)=>({
    fid,st:'UT',source_dataset:dataset,source_id:sourceId,
    source_record_id:String(record),source_scale:scale,
    source_scale_status:'official source scale retained',
    source_ref:`Utah Geological Survey ${publication}`,
    source_url:'https://geology.utah.gov/map-pub/',publication_id:publication,
  });
  const layers={
    ut_ugs_map179dm_geology:{geometry:'polygon',activation_zoom:5,
      title:'Utah geology — UGS Map 179DM, 1:500,000',
      properties:{...common(101,'UGS Map 179DM','ugs-map179dm:geology:101',101,
        '1:500,000','UGS Map 179DM'),'map_unit':'Jg',
        unit_name:'Acceptance Jurassic sandstone',unit_age:'Jurassic'},
      style:{type:'fill',filter:stateFilter,
        paint:{'fill-color':'#a88c67','fill-opacity':.25,'fill-outline-color':'#6f604d'}},
      semantic_note:'Statewide 1:500,000 map units; not site-scale geology.'},
    ut_ugs_map179dm_structures:{geometry:'line',activation_zoom:6,
      title:'Utah contacts and structures — UGS Map 179DM',
      properties:{...common(202,'UGS Map 179DM','ugs-map179dm:structure:202',202,
        '1:500,000','UGS Map 179DM'),feature_type:'Acceptance normal fault',
        feature_subtype:'normal fault',location_modifier:'approximately located'},
      style:{type:'line',filter:stateFilter,paint:{'line-color':'#4a3b32',
        'line-opacity':.72,'line-width':['interpolate',['linear'],['zoom'],6,.45,12,1.5]}},
      semantic_note:'Map contacts and structures; not an activity classification.'},
    ut_ugs_ds7_quaternary_faults:{geometry:'line',activation_zoom:5,
      title:'Utah Quaternary faults — UGS Data Series 7 (2026)',
      properties:{...common(303,'UGS Data Series 7','ugs-ds7:fault:303',303,
        '1:24,000','UGS Data Series 7'),fault_age:'late Quaternary',
        mapped_scale:'1:24,000',mapping_constraint:'field mapped',
        slip_sense:'normal',slip_rate:'less than 0.2 mm/yr'},
      style:{type:'line',filter:stateFilter,paint:{'line-color':'#d94841',
        'line-opacity':.88,'line-width':['interpolate',['linear'],['zoom'],5,.8,12,2.2]}},
      semantic_note:'UGS Quaternary compilation; not mineral-tenure evidence.'},
    ut_ugs_ofr695_mining_districts:{geometry:'polygon',activation_zoom:5,
      title:'Utah historic mining districts — UGS OFR-695',
      properties:{...common(404,'UGS OFR-695','ugs-ofr695:district:404',404,
        'compilation scale','UGS OFR-695'),
        district_name:'Acceptance Tintic Mining District',boundary_status:'approximate'},
      style:{type:'fill',filter:stateFilter,
        paint:{'fill-color':'#d19a37','fill-opacity':.14,'fill-outline-color':'#8a5b16'}},
      semantic_note:'Approximate historic district footprint; not tenure.'},
    ut_ugs_ofr757_umos:{geometry:'point',activation_zoom:6,
      title:'Utah Mineral Occurrence System — UGS OFR-757',
      properties:{...common(505,'UGS OFR-757 UMOS','ugs-ofr757:umos:505',505,
        'source record location','UGS OFR-757'),
        site_name:'Acceptance UMOS Gold Prospect',commodity:'Gold',
        occurrence_scope:'prospect',deposit_type:'epithermal vein',
        occurrence_status:'historic prospect',synonym:'Acceptance Mine',
        quadrangle:'Acceptance 7.5-minute',summary:'Synthetic official-field popup fixture.'},
      style:{type:'circle',filter:stateFilter,paint:{'circle-color':'#d86cff',
        'circle-radius':['interpolate',['linear'],['zoom'],6,2,12,5],
        'circle-stroke-color':'#ffffff','circle-stroke-width':.7,'circle-opacity':.9}},
      semantic_note:'Occurrence points are not current land status.'},
  };
  const commonRequired=['fid','st','source_dataset','source_id','source_record_id',
    'source_scale','source_scale_status','source_ref','source_url','publication_id'];
  const required={
    ut_ugs_map179dm_geology:[...commonRequired,'map_unit','unit_name','unit_age'],
    ut_ugs_map179dm_structures:[...commonRequired,'feature_type','feature_subtype','location_modifier'],
    ut_ugs_ds7_quaternary_faults:[...commonRequired,'fault_age','mapped_scale','mapping_constraint'],
    ut_ugs_ofr695_mining_districts:[...commonRequired,'district_name','boundary_status'],
    ut_ugs_ofr757_umos:[...commonRequired,'site_name','commodity','occurrence_scope'],
  };
  const archives=[
    ['ut_ugs_map179dm_500k','ugs-map179dm-500k.pmtiles',
      ['ut_ugs_map179dm_geology','ut_ugs_map179dm_structures']],
    ['ut_ugs_ds7_quaternary_faults','ugs-ds7-quaternary-faults.pmtiles',
      ['ut_ugs_ds7_quaternary_faults']],
    ['ut_ugs_ofr695_mining_districts','ugs-ofr695-mining-districts.pmtiles',
      ['ut_ugs_ofr695_mining_districts']],
    ['ut_ugs_ofr757_umos','ugs-ofr757-umos.pmtiles',['ut_ugs_ofr757_umos']],
  ];
  const files=new Map(),entries={};
  for(const [key,name,layerIds] of archives){
    const file=`__fixture__/ut/${name}`,local=path.join(directory,name);
    fs.writeFileSync(local,syntheticPmtiles(layerIds.map(id=>({id,...layers[id]}))));
    files.set('/'+file,local);
    const descriptorLayers=layerIds.map(id=>({
      layer_id:`${id}_baseline`,title:layers[id].title,source_layer:id,
      geometry:layers[id].geometry,style:layers[id].style,
      required_properties:required[id],feature_count:1,bounds,
      activation_zoom:layers[id].activation_zoom,default_visible:false,
      state_filter:stateFilter,semantic_note:layers[id].semantic_note,
    }));
    entries[key]={schema_version:1,status:'baseline_not_release',state:'UT',
      format:'pmtiles',file,source:{title:layers[layerIds[0]].title},
      n:layerIds.length,states:{UT:layerIds.length},bounds,minzoom:5,maxzoom:6,
      required_properties:Object.fromEntries(layerIds.map(id=>[id,required[id]])),
      provenance_note:'Synthetic browser-only Utah state-survey contract.',
      browser_descriptor:{schema_version:1,
        status:'proposed_lazy_state_survey_descriptor',manifest_key:key,file,
        protocol_url:`pmtiles://${file}`,state:'UT',lazy:true,
        default_visible:false,activation_zoom:Math.min(
          ...descriptorLayers.map(layer=>layer.activation_zoom)),bounds,
        minzoom:5,maxzoom:6,state_filter:stateFilter,layers:descriptorLayers}};
  }
  return {files,entries};
}

function fulfillLocalRange(route, file) {
  const size = fs.statSync(file).size;
  const range = route.request().headers().range;
  if (!range) {
    const body = fs.readFileSync(file);
    return route.fulfill({status: 200,
    contentType: 'application/octet-stream',
    headers: {'Accept-Ranges': 'bytes', 'Content-Length': String(size)}, body});
  }
  const match = /^bytes=(\d+)-(\d*)$/.exec(range);
  if (!match) return route.fulfill({status: 416,
    headers: {'Content-Range': `bytes */${size}`}});
  const start = Number(match[1]);
  const end = Math.min(match[2] ? Number(match[2]) : size - 1, size - 1);
  if (start > end || start >= size) return route.fulfill({status: 416,
    headers: {'Content-Range': `bytes */${size}`}});
  const chunk = Buffer.allocUnsafe(end - start + 1), fd=fs.openSync(file,'r');
  try{fs.readSync(fd,chunk,0,chunk.length,start);}finally{fs.closeSync(fd);}
  return route.fulfill({status: 206, contentType: 'application/octet-stream',
    headers: {'Accept-Ranges': 'bytes',
      'Content-Range': `bytes ${start}-${end}/${size}`,
      'Content-Length': String(chunk.length)}, body: chunk});
}

function browserExecutable() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const mac = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  return fs.existsSync(mac) ? mac : undefined;
}

function startServer() {
  const child = spawn('python3', ['tools/range_server.py', String(PORT)], {
    cwd: ROOT,
    stdio: 'ignore',
  });
  return child;
}

async function waitForServer(child) {
  let last;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (child.exitCode !== null) throw new Error(`range server exited ${child.exitCode}`);
    try {
      const response = await fetch(`${BASE}/data/manifest.json`, {
        headers: {Range: 'bytes=0-31'},
      });
      if (response.status === 206 && response.headers.get('accept-ranges') === 'bytes') {
        await new Promise(resolve => setTimeout(resolve, 100));
        if (child.exitCode !== null) throw new Error(`range server exited ${child.exitCode}`);
        return;
      }
      last = new Error(`unexpected range response ${response.status}`);
    } catch (error) {
      last = error;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`range server did not become ready: ${last}`);
}

async function settle(page, timeout = 30_000) {
  await page.waitForFunction(() => {
    try { return map.loaded() && map.areTilesLoaded(); } catch (_) { return false; }
  }, null, {timeout});
  await page.waitForTimeout(750);
  await page.evaluate(async () => {
    if (typeof gc === 'function') {
      gc();
      await new Promise(resolve => setTimeout(resolve, 150));
      gc();
    }
  });
}

async function waitForSourceFeatures(page, specs, timeout = 30_000) {
  await page.waitForFunction(items => items.every(([source, sourceLayer]) => {
    try { return map.querySourceFeatures(source, {sourceLayer}).length > 0; }
    catch (_) { return false; }
  }), specs, {timeout});
}

async function sample(page, label) {
  const value = await page.evaluate(async measuredLabel => {
    const estimate = await navigator.storage.estimate().catch(() => ({}));
    const style = map.getStyle();
    const sources = Object.fromEntries(Object.entries(style.sources).map(
      ([id, source]) => [id, {type: source.type, url: source.url || null}]));
    return {
      label: measuredLabel,
      heap_mb: performance.memory
        ? +(performance.memory.usedJSHeapSize / 1048576).toFixed(1) : null,
      origin_storage_mb: +((estimate.usage || 0) / 1048576).toFixed(2),
      zoom: +map.getZoom().toFixed(2),
      sources,
      rejections: typeof DBG_REJ === 'number' ? DBG_REJ : null,
      last_rejection: typeof DBG_LAST_REJ === 'string' ? DBG_LAST_REJ : null,
    };
  }, label);
  console.log(JSON.stringify(value));
  return value;
}

async function robustnessSample(page, label) {
  const value=await page.evaluate(async measuredLabel=>{
    if(typeof gc==='function'){
      gc();await new Promise(resolve=>setTimeout(resolve,100));gc();
    }
    const estimate=await navigator.storage.estimate().catch(()=>({}));
    const records=await idbAll().catch(()=>[]);
    const userLayerBytes=records.reduce((n,row)=>n+JSON.stringify(row).length,0);
    return {label:measuredLabel,
      heap_mb:performance.memory
        ? +(performance.memory.usedJSHeapSize/1048576).toFixed(1):null,
      origin_storage_mb:+((estimate.usage||0)/1048576).toFixed(3),
      user_layer_records:records.length,
      user_layer_json_bytes:userLayerBytes,
      user_layer_json_mb:+(userLayerBytes/1048576).toFixed(3)};
  },label);
  console.log(JSON.stringify(value));
  return value;
}

async function runRobustness() {
  const indexHtml=fs.readFileSync(path.join(ROOT,'site','index.html'),'utf8');
  assert.doesNotMatch(indexHtml,/data\/[^'"`\s]+\.(?:pmtiles|tif)\b/,
    'browser binary URLs must come from the runtime manifest');
  assert.match(indexHtml,/addPmtilesSource\('admin',national\.admin\.file\)/,
    'admin source must use national_baselines.admin.file');
  assert.match(indexHtml,/addPmtilesSource\('gp-surveys',surveys\.file\)/,
    'geophysics survey source must use ws56.geophys_surveys.file');
  const manifest=JSON.parse(fs.readFileSync(path.join(ROOT,'site','data','manifest.json'),'utf8'));
  const scenarios=['curated','cassia','auto'].map(key=>({key,
    path:new URL(manifest.districts[key].file,BASE+'/').pathname}));
  const districtPaths=new Set(scenarios.map(row=>row.path));
  const server=startServer();
  let browser,context;
  const measurements=[];
  try{
    await waitForServer(server);
    browser=await chromium.launch({headless:true,executablePath:browserExecutable(),args:[
      '--js-flags=--expose-gc','--enable-precise-memory-info',
      '--use-angle=swiftshader','--disable-dev-shm-usage',
    ]});
    context=await browser.newContext({viewport:{width:1280,height:800}});
    for(const scenario of scenarios){
      const page=await context.newPage(), pageErrors=[], districtResponses=[];
      page.on('pageerror',error=>pageErrors.push(String(error)));
      page.on('response',response=>{
        const url=new URL(response.url());
        if(districtPaths.has(url.pathname))districtResponses.push([url.pathname,response.status()]);
      });
      await page.route('**/*',route=>{
        const url=new URL(route.request().url());
        if((url.hostname==='127.0.0.1'||url.hostname==='localhost')&&
            url.pathname===scenario.path)
          return route.fulfill({status:500,contentType:'application/json',
            body:JSON.stringify({error:`acceptance ${scenario.key} failure`})});
        if(url.hostname==='127.0.0.1'||url.hostname==='localhost')return route.continue();
        if(/(?:basemaps\.cartocdn\.com|server\.arcgisonline\.com|basemap\.nationalmap\.gov)$/.test(url.hostname))
          return route.fulfill({status:200,contentType:'image/png',body:DARK_PX});
        if(url.hostname==='gis.blm.gov'&&url.pathname.endsWith('/Mining_Claims/MiningClaims/MapServer/1/query'))
          return route.fulfill({status:200,contentType:'application/geo+json',
            body:JSON.stringify({type:'FeatureCollection',features:[]})});
        return route.abort('blockedbyclient');
      });
      await page.goto(`${BASE}/?debug=1`,{waitUntil:'domcontentloaded',timeout:90_000});
      await page.waitForFunction(()=>typeof DBG_BOOT_SETTLED!=='undefined'&&DBG_BOOT_SETTLED,
        null,{timeout:90_000});
      const result=await page.evaluate(key=>{
        const taskStatus=Object.fromEntries(Object.entries(DBG_BOOT_TASKS)
          .map(([name,row])=>[name,row.status]));
        const districtCounts={curated:curated.length,cassia:cassia.length,auto:auto.length};
        return {auxErrors:{...DBG_AUX_ERRORS},taskStatus,districtCounts,
          visibleError:document.getElementById('contextNote').textContent,
          continuations:{
            grades:!!(GR&&GR.n&&map.getSource('grades')),
            quad:QSTATE.status==='ready'&&!!QUAD,
            openGround:!!(OG&&map.getSource('og')),
            geoTargets:!!(TG&&map.getSource('tg')),
            countyGold:!!(CG&&map.getLayer('ctygold-fill')),
            mapGeology:!!document.getElementById('msFillTgl'),
            geophys:!!(map.getSource('gp-surveys')&&document.getElementById('gp-surv-tgl')),
            userLayers:!!map.getSource('u-inbox-messy-cassia-geojson'),
            countyAlerts:!!CTY&&getComputedStyle(document.getElementById('btnAlerts')).display!=='none',
            stats:document.querySelectorAll('#chart .bar').length===GROUPS.length,
            live:!!map.getSource('liveClaims'),
          },
          urls:{
            admin:map.getStyle().sources.admin&&map.getStyle().sources.admin.url,
            adminManifest:'pmtiles://'+MAN.national_baselines.admin.file,
            geophys:map.getStyle().sources['gp-surveys']&&map.getStyle().sources['gp-surveys'].url,
            geophysManifest:'pmtiles://'+MAN.ws56.geophys_surveys.file,
          },rejections:DBG_REJ,mapErrors:[...DBG_MAP_ERRORS]};
      },scenario.key);
      assert.deepEqual(Object.keys(result.auxErrors),[`districts.${scenario.key}`],
        `${scenario.key}: failure must remain scoped to its district resource`);
      assert.equal(result.auxErrors[`districts.${scenario.key}`].status,'rejected');
      assert.equal(new URL(result.auxErrors[`districts.${scenario.key}`].url,BASE+'/').pathname,
        scenario.path);
      assert.match(result.visibleError,/District context partial:/);
      assert.match(result.visibleError,/Other map layers are still running/);
      assert.equal(result.districtCounts[scenario.key],0,
        `${scenario.key}: failed district collection must remain empty`);
      for(const other of scenarios.filter(row=>row.key!==scenario.key))
        assert.ok(result.districtCounts[other.key]>0,
          `${scenario.key}: ${other.key} districts must survive sibling failure`);
      assert.deepEqual(result.taskStatus,Object.fromEntries(Object.keys(result.taskStatus)
        .map(name=>[name,'fulfilled'])),`${scenario.key}: boot task cascade`);
      assert.ok(Object.values(result.continuations).every(Boolean),
        `${scenario.key}: independent loader did not continue: ${JSON.stringify(result.continuations)}`);
      assert.deepEqual(result.urls.admin,result.urls.adminManifest);
      assert.deepEqual(result.urls.geophys,result.urls.geophysManifest);
      assert.equal(result.rejections,0,`${scenario.key}: unhandled rejection`);
      assert.deepEqual(result.mapErrors,[],`${scenario.key}: MapLibre errors`);
      assert.deepEqual(pageErrors,[],`${scenario.key}: browser exceptions`);
      for(const other of scenarios){
        const statuses=districtResponses.filter(([pathname])=>pathname===other.path).map(([,status])=>status);
        assert.ok(statuses.includes(other.key===scenario.key?500:200),
          `${scenario.key}: missing deterministic ${other.key} response evidence`);
      }
      measurements.push(await robustnessSample(page,`district-${scenario.key}-500`));

      if(scenario.key==='auto'){
        const slug='acceptance-hidden';
        await page.evaluate(async layerSlug=>idbPut({slug:layerSlug,name:'Acceptance hidden layer',
          added:'2026-08-13',n:1,misses:0,plssAmbiguous:0,visible:false,
          fc:{type:'FeatureCollection',features:[{type:'Feature',properties:{name:'Lifecycle point'},
            geometry:{type:'Point',coordinates:[-98.5,39.4]}}]}}),slug);
        await page.reload({waitUntil:'domcontentloaded',timeout:90_000});
        await page.waitForFunction(()=>DBG_BOOT_SETTLED&&DBG_BOOT_TASKS.userLayers.status==='fulfilled',
          null,{timeout:90_000});
        const selector=`#layersUser .ulrow[data-slug="${slug}"] [data-a="vis"]`;
        const hidden=await page.evaluate(async layerSlug=>({
          source:!!map.getSource('u-'+layerSlug),
          layers:['-f','-l','-c'].filter(suffix=>map.getLayer('u-'+layerSlug+suffix)).length,
          button:document.querySelector(`#layersUser .ulrow[data-slug="${layerSlug}"] [data-a="vis"]`)?.textContent,
          record:(await idbAll()).find(row=>row.slug===layerSlug),
          diagnostics:userLayerDiagnostics(),
        }),slug);
        assert.deepEqual({source:hidden.source,layers:hidden.layers,button:hidden.button,visible:hidden.record.visible},
          {source:false,layers:0,button:'SHOW',visible:false},
          'persisted hidden layer must stay unallocated after reload');
        const baseline={bind:hidden.diagnostics.bindTotal,unbind:hidden.diagnostics.unbindTotal};
        measurements.push(await robustnessSample(page,'user-layer-hidden-after-reload'));

        await page.locator(selector).click();
        await page.waitForFunction(layerSlug=>map.getSource('u-'+layerSlug)&&
          ['-f','-l','-c'].every(suffix=>map.getLayer('u-'+layerSlug+suffix)),slug);
        const firstShow=await page.evaluate(async layerSlug=>({diag:userLayerDiagnostics(),
          visible:(await idbAll()).find(row=>row.slug===layerSlug).visible,
          button:document.querySelector(`#layersUser .ulrow[data-slug="${layerSlug}"] [data-a="vis"]`).textContent,
        }),slug);
        assert.equal(firstShow.visible,true);assert.equal(firstShow.button,'HIDE');
        assert.equal(firstShow.diag.active[slug].handlers,4);
        assert.equal(firstShow.diag.bindTotal,baseline.bind+4);

        await page.locator(selector).click();
        await page.waitForFunction(layerSlug=>!map.getSource('u-'+layerSlug)&&
          ['-f','-l','-c'].every(suffix=>!map.getLayer('u-'+layerSlug+suffix)),slug);
        const hiddenAgain=await page.evaluate(async layerSlug=>({diag:userLayerDiagnostics(),
          visible:(await idbAll()).find(row=>row.slug===layerSlug).visible,
          button:document.querySelector(`#layersUser .ulrow[data-slug="${layerSlug}"] [data-a="vis"]`).textContent,
        }),slug);
        assert.equal(hiddenAgain.visible,false);assert.equal(hiddenAgain.button,'SHOW');
        assert.equal(hiddenAgain.diag.active[slug],undefined);
        assert.equal(hiddenAgain.diag.unbindTotal,baseline.unbind+4);
        measurements.push(await robustnessSample(page,'user-layer-hidden-after-teardown'));

        await page.locator(selector).click();
        await page.waitForFunction(layerSlug=>map.getSource('u-'+layerSlug)&&
          ['-f','-l','-c'].every(suffix=>map.getLayer('u-'+layerSlug+suffix)),slug);
        const secondShow=await page.evaluate(layerSlug=>{
          window.__userLayerFeatureCalls=0;
          const original=showUserFeature;
          showUserFeature=(...args)=>{window.__userLayerFeatureCalls++;return original(...args);};
          map.jumpTo({center:[-98.5,39.4],zoom:8});
          return userLayerDiagnostics();
        },slug);
        assert.equal(secondShow.active[slug].handlers,4);
        assert.equal(secondShow.bindTotal,baseline.bind+8);
        await page.waitForFunction(layerSlug=>map.queryRenderedFeatures({layers:['u-'+layerSlug+'-c']}).length>0,
          slug,{timeout:30_000});
        const point=await page.evaluate(()=>map.project([-98.5,39.4]));
        const canvas=await page.locator('.maplibregl-canvas').boundingBox();
        assert.ok(canvas,'map canvas unavailable for user-layer click');
        await page.mouse.click(canvas.x+point.x,canvas.y+point.y);
        await page.waitForFunction(()=>window.__userLayerFeatureCalls>0);
        assert.equal(await page.evaluate(()=>window.__userLayerFeatureCalls),1,
          'SHOW/HIDE/SHOW must leave exactly one delegated feature handler');
        measurements.push(await robustnessSample(page,'user-layer-second-show'));
        const lifecycleErrors=await page.evaluate(()=>({rejections:DBG_REJ,mapErrors:[...DBG_MAP_ERRORS]}));
        assert.equal(lifecycleErrors.rejections,0,'user-layer lifecycle: unhandled rejection');
        assert.deepEqual(lifecycleErrors.mapErrors,[],'user-layer lifecycle: MapLibre errors');
        assert.deepEqual(pageErrors,[],'user-layer lifecycle: browser exceptions');
      }
      await page.close();
    }
    for(const measurement of measurements){
      assert.ok(measurement.heap_mb!==null,'precise heap measurement unavailable');
      assert.ok(measurement.heap_mb<=BUDGETS.browser.heap_mb_max,
        `${measurement.label}: heap ${measurement.heap_mb} MB exceeds ${BUDGETS.browser.heap_mb_max} MB`);
      assert.ok(measurement.user_layer_json_mb<=BUDGETS.browser.user_layer_idb_mb_max,
        `${measurement.label}: user-layer storage exceeds budget`);
    }
    console.log('WS11 focused boot/user-layer robustness acceptance passed');
  } finally {
    if(context)await context.close().catch(()=>{});
    if(browser)await browser.close().catch(()=>{});
    if(server.exitCode===null)server.kill('SIGTERM');
  }
}

async function run() {
  const server = startServer();
  let browser;
  let utahFixtureDirectory;
  try {
    await waitForServer(server);
    const executablePath = browserExecutable();
    browser = await chromium.launch({
      headless: true,
      executablePath,
      args: [
        '--js-flags=--expose-gc', '--enable-precise-memory-info',
        '--use-angle=swiftshader', '--disable-dev-shm-usage',
      ],
    });
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const legacyRequests = [];
    const pageErrors = [];
    const requestFailures = [];
    const localHttpErrors = [];
    const mapErrors = [];
    const releaseCogRequests = [];
    const alaskaPmtilesRequests = [];
    const utahPmtilesRequests = [];
    const utahFixtureRequests = [];
    utahFixtureDirectory=fs.mkdtempSync(path.join(os.tmpdir(),'nwmm-ut-browser-'));
    const utahBrowserFixture=createUtahBrowserFixture(utahFixtureDirectory);
    // Browser-only release fixture: use the checked compatibility archive as
    // a stand-in PMTiles payload, but advertise NV active through the same
    // tiled-layer descriptor a gate-passed state will publish. Nothing on
    // disk is mutated. This proves release precedence/query integration even
    // while the real coverage dashboard honestly remains 0/49 released.
    const fixtureManifest = JSON.parse(fs.readFileSync(
      path.join(ROOT, 'site', 'data', 'manifest.json'), 'utf8'));
    Object.assign(fixtureManifest.national_baselines,utahBrowserFixture.entries);
    const fixtureCoverage = JSON.parse(fs.readFileSync(
      path.join(ROOT, 'site', 'data', 'coverage.json'), 'utf8'));
    const fixtureRuntime = JSON.parse(fs.readFileSync(
      path.join(ROOT, 'infra', 'state_runtime.json'), 'utf8')).states;
    // Prefer the checked-in split contract/artifacts after publication. The
    // explicit environment override supports a release-candidate directory;
    // the private fallback exists only for the pre-publication local gate.
    const publicEntry = fixtureManifest.national_baselines.alaska_state_claims;
    const privateEntries = '/private/tmp/nwmm-ak-refresh.oZqS5T/final-manifest-entries.json';
    const privateRoot = '/private/tmp/nwmm-ak-refresh.oZqS5T/split24-build2';
    const overrideManifest = process.env.NWMM_ALASKA_MANIFEST_FIXTURE;
    const overrideRoot = process.env.NWMM_ALASKA_ARTIFACT_DIR;
    const splitContract = entry => !!(entry && Number(entry.activation_zoom) === 8 &&
      entry.precision_overflow && Number(entry.precision_overflow.activation_zoom) === 19);
    const artifactAt = (root, file) => {
      const structured = path.join(root, file);
      return fs.existsSync(structured) ? structured : path.join(root, path.basename(file));
    };
    let alaskaEntry = publicEntry, alaskaArtifactRoot = path.join(ROOT, 'site');
    let alaskaFixtureOrigin = 'public';
    if (overrideManifest || overrideRoot) {
      assert.ok(overrideManifest && overrideRoot,
        'NWMM_ALASKA_MANIFEST_FIXTURE and NWMM_ALASKA_ARTIFACT_DIR must be set together');
      const document = JSON.parse(fs.readFileSync(overrideManifest, 'utf8'));
      alaskaEntry = (document.national_baselines || document).alaska_state_claims;
      alaskaArtifactRoot = overrideRoot;
      alaskaFixtureOrigin = 'environment override';
    } else {
      const publicFiles = splitContract(publicEntry) &&
        [publicEntry.file, publicEntry.precision_overflow.file]
          .map(file => artifactAt(alaskaArtifactRoot, file));
      if (!publicFiles || !publicFiles.every(file => fs.existsSync(file))) {
        assert.ok(fs.existsSync(privateEntries) && fs.existsSync(privateRoot),
          'no public split Alaska fixture and pre-publication private fixture is unavailable');
        alaskaEntry = JSON.parse(fs.readFileSync(privateEntries, 'utf8')).alaska_state_claims;
        alaskaArtifactRoot = privateRoot;
        alaskaFixtureOrigin = 'pre-publication private fallback';
      }
    }
    assert.ok(splitContract(alaskaEntry),
      `${alaskaFixtureOrigin} Alaska manifest must declare base z8 and precision z19 activation`);
    const alaskaFixtureFiles = new Map([
      [`/${alaskaEntry.file}`, artifactAt(alaskaArtifactRoot, alaskaEntry.file)],
      [`/${alaskaEntry.precision_overflow.file}`,
        artifactAt(alaskaArtifactRoot, alaskaEntry.precision_overflow.file)],
    ]);
    for (const file of alaskaFixtureFiles.values()) assert.ok(fs.existsSync(file),
      `${alaskaFixtureOrigin} Alaska browser fixture missing: ${file}`);
    fixtureManifest.national_baselines.alaska_state_claims = alaskaEntry;
    const nvActive = fixtureManifest.national_baselines.claims.by_mode.active.states.NV;
    fixtureManifest.tiled_layers = (fixtureManifest.tiled_layers||[]).filter(spec=>
      !((spec.state==='NV'&&spec.source_layer==='active'&&String(spec.kind||'').startsWith('federal_mlrs'))||
        (spec.state==='FL'&&spec.source_layer==='closed'&&String(spec.kind||'').startsWith('federal_mlrs'))));
    fixtureManifest.tiled_layers.push({
      id: 'nv-federal_mlrs-active', state: 'NV', regime: 'claim',
      kind: 'federal_mlrs-active', delivery: 'pmtiles',
      source_id: 'ws11-fixture-shared-federal-claims',
      url: fixtureManifest.national_baselines.claims.file,
      source_layer: 'active', n: nvActive, interactive: true,
      view_bounds: [[-120, 35, -114, 42]], activation_minzoom: 4,
      style_layers: [{id: 'nv-federal_mlrs-active', type: 'circle',
        paint: {'circle-color': '#2dd4bf', 'circle-radius': 3}}],
    });
    fixtureManifest.tiled_layers.push({
      id: 'fl-federal_mlrs-closed', state: 'FL', regime: 'claim',
      kind: 'federal_mlrs-closed', delivery: 'pmtiles',
      source_id: 'ws11-fixture-shared-federal-claims',
      url: fixtureManifest.national_baselines.claims.file,
      source_layer: 'closed', n: 0, interactive: true,
      view_bounds: [[-88, 24, -79, 32]], activation_minzoom: 4,
      style_layers: [{id: 'fl-federal_mlrs-closed', type: 'circle',
        paint: {'circle-color': '#8a6060', 'circle-radius': 3}}],
    });
    // Synthetic all-49 release stack: each state gets its own logical/context
    // archive URL so this acceptance catches eager source fan-out and PMTiles
    // cache retention. Only the selected, in-view state may allocate.
    for(const state of EXPECTED_STATES)fixtureManifest.tiled_layers.push({
      id: `${state.toLowerCase()}-stress-context`, state,
      regime: CLAIM_STATE_CODES.has(state)?'claim':'non_claim',
      kind: 'land-context', delivery: 'pmtiles',
      source_id: `ws11-fixture-context-${state.toLowerCase()}`,
      url: `${fixtureManifest.national_baselines.usmin.file}?ws11_state=${state}`,
      source_layer: 'usmin', n: fixtureManifest.national_baselines.usmin.states[state],
      availability: 'complete', complete: true, interactive: true,
      view_bounds: fixtureRuntime[state].query_envelopes, activation_minzoom: 4,
      style_layers: [{id: `${state.toLowerCase()}-stress-context`, type: 'circle',
        paint: {'circle-color': '#a78bfa', 'circle-radius': 3}}],
    });
    for (const [state, bounds] of [
      ['NV', [-120, 35, -114, 42]], ['FL', [-88, 24, -79, 32]],
    ]) fixtureManifest.tiled_layers.push({
      id: `${state.toLowerCase()}-aeromag`, state, regime: 'claim',
      kind: 'aeromag', delivery: 'cog',
      source_id: `ws11-raster-${state.toLowerCase()}-aeromag`,
      url: `/__fixture__/aeromag/${state.toLowerCase()}/{z}/{x}/{y}.png`,
      tile_url_template: `/__fixture__/aeromag/${state.toLowerCase()}/{z}/{x}/{y}.png`,
      tile_scheme: 'xyz', tile_size: 256, bounds, minzoom: 4, maxzoom: 12,
      cog: {url: `map-assets/releases/${state.toLowerCase()}/aeromag-fixture.tif`,
        sha256: 'a'.repeat(64), bytes: 4096},
      style_layers: [{id: `${state.toLowerCase()}-aeromag`, type: 'raster',
        paint: {'raster-opacity': 0.65}}],
    });
    const fixtureNv = fixtureCoverage.states.find(row => row.state === 'NV');
    fixtureNv.enabled = true;
    fixtureNv.gate_passed = true;
    fixtureNv.release = 'done';
    const fixtureFl = fixtureCoverage.states.find(row => row.state === 'FL');
    fixtureFl.enabled = true;
    fixtureFl.gate_passed = true;
    fixtureFl.release = 'done';
    for(const row of fixtureCoverage.states){
      row.enabled=true;row.gate_passed=true;row.release='done';
    }
    fixtureCoverage.summary.released = fixtureCoverage.states.filter(row=>row.enabled).length;
    fixtureCoverage.summary.gate_complete = fixtureCoverage.states.filter(row=>row.gate_passed).length;
    page.on('pageerror', error => pageErrors.push(String(error)));
    page.on('requestfailed', request => {
      const url=new URL(request.url()), error=request.failure()?.errorText||'failed';
      // Rapid lifecycle jumps deliberately obsolete some already-scheduled
      // deterministic basemap tiles. MapLibre cancels those with ERR_ABORTED;
      // this is not an external dependency or transport failure.
      if(error==='net::ERR_ABORTED'&&
          /(?:basemaps\.cartocdn\.com|server\.arcgisonline\.com|basemap\.nationalmap\.gov)$/.test(url.hostname))
        return;
      requestFailures.push(`${request.url()} — ${error}`);
    });
    page.on('response', response => {
      const url=new URL(response.url());
      if((url.hostname==='127.0.0.1'||url.hostname==='localhost')&&response.status()>=400&&
          !/\/data\/alerts\/(?:latest|ak_state_latest)\.json$/.test(url.pathname)&&url.pathname!=='/auth.json')
        localHttpErrors.push(`${response.status()} ${url.pathname}`);
    });
    page.on('request', request => {
      const pathname = new URL(request.url()).pathname;
      if (alaskaFixtureFiles.has(pathname))
        alaskaPmtilesRequests.push({pathname,range:request.headers().range||null});
      if (utahBrowserFixture.files.has(pathname))
        utahPmtilesRequests.push({pathname,range:request.headers().range||null});
      if (pathname.startsWith('/__fixture__/ut/'))
        utahFixtureRequests.push({pathname,range:request.headers().range||null});
      if (/\/__fixture__\/aeromag\//.test(pathname)) releaseCogRequests.push(pathname);
      if (/\/data\/(?:claims|sites|boundaries|geophys|faults|land-context|aml|trust-land)\//.test(pathname)||
          /\/data\/(?:geology|targets|openground|plss)\/(?:states?\/)?(?:al|ak|az|ar|ca|co|ct|de|fl|ga|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy)(?:[._/-]|$)/i.test(pathname))
        legacyRequests.push(pathname);
    });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if ((url.hostname === '127.0.0.1' || url.hostname === 'localhost') &&
          url.pathname === '/data/manifest.json')
        return route.fulfill({status: 200, contentType: 'application/json',
          body: JSON.stringify(fixtureManifest)});
      if ((url.hostname === '127.0.0.1' || url.hostname === 'localhost') &&
          url.pathname === '/data/coverage.json')
        return route.fulfill({status: 200, contentType: 'application/json',
          body: JSON.stringify(fixtureCoverage)});
      if ((url.hostname === '127.0.0.1' || url.hostname === 'localhost') &&
          alaskaFixtureFiles.has(url.pathname))
        return fulfillLocalRange(route, alaskaFixtureFiles.get(url.pathname));
      if ((url.hostname === '127.0.0.1' || url.hostname === 'localhost') &&
          utahBrowserFixture.files.has(url.pathname))
        return fulfillLocalRange(route, utahBrowserFixture.files.get(url.pathname));
      if ((url.hostname === '127.0.0.1' || url.hostname === 'localhost') &&
          url.pathname.startsWith('/__fixture__/aeromag/'))
        return route.fulfill({status: 200, contentType: 'image/png', body: DARK_PX});
      if (url.hostname === '127.0.0.1' || url.hostname === 'localhost') return route.continue();
      if (url.hostname === 'gis.blm.gov' &&
          url.pathname.endsWith('/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer/2/query')) {
        const where=url.searchParams.get('where')||'';
        const make=(state,meridian,x)=>({attributes:{
          PLSSID:`${state}${meridian}0120S0220E0`,
          FRSTDIVID:`${state}${meridian}0120S0220E0SN140`,FRSTDIVLAB:'14'},
          geometry:{rings:[[[x,38],[x+.1,38],[x+.1,38.1],[x,38.1],[x,38]]]}});
        const features=where.includes("PLSSID LIKE 'CA21")
          ? [make('CA','21',-120)] : where.includes("PLSSID LIKE 'CA22")
            ? [make('NV','27',-116)] : [make('CA','21',-120),make('NV','27',-116)];
        return route.fulfill({status:200,contentType:'application/json',
          body:JSON.stringify({features})});
      }
      if (url.hostname === 'gis.blm.gov' &&
          url.pathname.endsWith('/Mining_Claims/MiningClaims/MapServer/1/query')) {
        const where=url.searchParams.get('where')||'';
        const cursor=Number((where.match(/OBJECTID>(\d+)/)||[])[1]||0);
        const oid=cursor+1;
        return route.fulfill({status:200,contentType:'application/geo+json',body:JSON.stringify({
          type:'FeatureCollection',exceededTransferLimit:true,features:[{
            type:'Feature',properties:{OBJECTID:oid,CSE_NR:`NMC${oid}`,
              CSE_NAME:`Acceptance live claim ${oid}`,CSE_TYPE_NR:384101,
              CSE_DISP:'ACTIVE',RCRD_ACRS:20},
            geometry:{type:'Polygon',coordinates:[[[-117,38],[-116.99,38],
              [-116.99,38.01],[-117,38.01],[-117,38]]]}}
          ]})});
      }
      const deterministicRaster = (
        /(?:basemaps\.cartocdn\.com|server\.arcgisonline\.com|basemap\.nationalmap\.gov)$/.test(url.hostname) ||
        (url.hostname === 'mrdata.usgs.gov' &&
         url.pathname.startsWith('/mapcache/wmts/1.0.0/magnetic/'))
      );
      return deterministicRaster
        ? route.fulfill({status: 200, contentType: 'image/png', body: DARK_PX})
        : route.abort('blockedbyclient');
    });

    await page.goto(`${BASE}/?debug=1`, {waitUntil: 'domcontentloaded', timeout: 90_000});
    await page.waitForFunction(() => {
      try { return map.loaded() && document.querySelectorAll('.schip').length === 49; }
      catch (_) { return false; }
    }, null, {timeout: 90_000});
    await settle(page);

    const releaseVectorBoot=await page.evaluate(()=>({
      sources:Object.keys(map.getStyle().sources).filter(id=>id.startsWith('ws11-')),
      groups:Object.keys(WS11_VECTOR_GROUPS).length,
      protocolUrls:PMT_PROTOCOL&&PMT_PROTOCOL.tiles?[...PMT_PROTOCOL.tiles.keys()]:[],
      cacheShared:Object.values(WS11_VECTOR_GROUPS).every(group=>{
        const instance=PMT_PROTOCOL&&PMT_PROTOCOL.get(group.url);
        return !instance||instance.cache===PMT_CACHE;
      }),
    }));
    assert.deepEqual(releaseVectorBoot.sources,[],
      'below-threshold national boot must allocate no released vector source');
    assert.ok(releaseVectorBoot.groups>=49,
      'acceptance fixture must register an all-49 released-vector stress stack');
    assert.equal(releaseVectorBoot.cacheShared,true,
      'every registered release archive must use the one bounded PMTiles cache');

    const stateSurveyBoot=await page.evaluate(()=>({
      counts:Object.fromEntries(['AZ','CO','NV','UT'].map(state=>[
        state,STATE_SURVEY_LAYERS.filter(row=>row.state===state).length])),
      sources:STATE_SURVEY_LAYERS.filter(row=>map.getSource(row.source_id))
        .map(row=>row.source_id),
      enabled:STATE_SURVEY_LAYERS.filter(row=>S.stateSurvey[row.id]).map(row=>row.id),
      protocol:STATE_SURVEY_LAYERS.filter(row=>PMT_PROTOCOL.tiles.has(row.file))
        .map(row=>row.file),
      coEmbedded:STATE_SURVEY_LAYERS.filter(row=>row.state==='CO').every(row=>
        !!MAN.national_baselines[row.manifest_key].browser_descriptor),
      utEmbedded:STATE_SURVEY_LAYERS.filter(row=>row.state==='UT').every(row=>
        !!MAN.national_baselines[row.manifest_key].browser_descriptor),
    }));
    assert.deepEqual(stateSurveyBoot.counts,{AZ:3,CO:3,NV:3,UT:4},
      'published AZ/CO/NV plus synthetic UT atomic sets must compile generically');
    assert.deepEqual(stateSurveyBoot.sources,[],
      'boot must allocate zero optional state-survey PMTiles sources');
    assert.deepEqual(stateSurveyBoot.enabled,[]);
    assert.deepEqual(stateSurveyBoot.protocol,[],
      'boot must not retain optional state-survey protocol instances');
    assert.equal(stateSurveyBoot.coEmbedded,true,
      'Colorado must be driven by builder-emitted browser descriptors');
    assert.equal(stateSurveyBoot.utEmbedded,true,
      'Utah must be driven by builder-emitted browser descriptors');

    // Category-off released vectors must be metadata only at boot. Enabling
    // the category in its state footprint creates the PMTiles source/layer;
    // a state-off or category-off transition fully tears both down.
    assert.equal(await page.evaluate(()=>!!map.getSource('ws11-fixture-context-mi')),false,
      'released land-context PMTiles must remain unallocated while its category is off');
    await page.evaluate(()=>{
      setUiStates(['MI']);applyFilters();
      const row=document.querySelector('[data-layer="releaseContext"]');
      if(!row)throw new Error('released land-context toggle missing');
      row.click();map.jumpTo({center:[-85.5,44.5],zoom:7});
    });
    await settle(page);
    await waitForSourceFeatures(page,[['ws11-fixture-context-mi','usmin']]);
    const releasedVectorOn=await page.evaluate(()=>({
      source:!!map.getSource('ws11-fixture-context-mi'),
      layer:!!map.getLayer('ws11-mi-stress-context'),
      allReleaseSources:Object.keys(map.getStyle().sources).filter(id=>id.startsWith('ws11-')),
      features:map.querySourceFeatures('ws11-fixture-context-mi',{sourceLayer:'usmin'})
        .filter(feature=>(feature.properties||{}).st==='MI').length,
    }));
    assert.equal(releasedVectorOn.source,true);
    assert.equal(releasedVectorOn.layer,true);
    assert.deepEqual(releasedVectorOn.allReleaseSources,['ws11-fixture-context-mi'],
      'all-49 stress stack may allocate only the selected in-view state source');
    assert.ok(releasedVectorOn.features>0,'enabled in-view Michigan release must yield features');
    await page.evaluate(()=>document.querySelector('.schip[data-state="MI"]').click());
    await page.waitForFunction(()=>!map.getSource('ws11-fixture-context-mi')&&
      !map.getLayer('ws11-mi-stress-context'));
    const contextTeardown=await page.evaluate(()=>({
      fixture:PMT_PROTOCOL.tiles.has(
        MAN.national_baselines.usmin.file+'?ws11_state=MI'),
      baseline:PMT_PROTOCOL.tiles.has(MAN.national_baselines.usmin.file),
    }));
    assert.deepEqual(contextTeardown,{fixture:false,baseline:true},
      'teardown must evict the lazy URL but retain its persistent baseline');
    await page.evaluate(()=>{
      setUiStates(STATES);applyFilters();
      document.querySelector('[data-layer="releaseContext"]').click();
      map.jumpTo({center:[-98.5,39.4],zoom:3.4});
    });
    await settle(page);

    const plssContract=await page.evaluate(async()=>{
      const exact=await rowsToFeatures(
        ['name','state','plss_meridian','legal'],
        [['National PLSS fixture','CA','21','T12S R22E Sec 14']]);
      const ambiguous=await plssLookup(12,'S',22,'E',14,null,null);
      const commodityCo=await rowsToFeatures(
        ['commodity','legal'], [['Co','T12S R22E Sec 14']]);
      const wrongIdentity=await plssLookup(12,'S',22,'E',14,'CA','22');
      return {exact,ambiguous,commodityCo,wrongIdentity};
    });
    assert.equal(plssContract.exact.feats.length,1,
      'state+meridian PLSS legal must geocode through national CadNSDI');
    assert.equal(plssContract.exact.feats[0].properties._plss_state,'CA');
    assert.equal(plssContract.exact.feats[0].properties._plss_meridian,'21');
    assert.equal(plssContract.ambiguous.error,'ambiguous',
      'nationally ambiguous PLSS legal must not silently select a section');
    assert.equal(plssContract.commodityCo.feats.length,0,
      'commodity Co must not be interpreted as a Colorado state hint');
    assert.equal(plssContract.commodityCo.plssAmbiguous,1,
      'a state-less ambiguous legal remains explicit rather than false-geocoded');
    assert.equal(plssContract.wrongIdentity.error,'identity_mismatch',
      'CadNSDI response identity must match the requested state and meridian');

    // A live viewport query has a deliberate four-page interaction ceiling.
    // Force four non-empty pages and prove the browser calls the result
    // partial rather than presenting the capped row count as complete.
    const liveCapContract=await page.evaluate(async()=>{
      S.layers.live=false;applyFilters();
      map.jumpTo({center:[-117,38],zoom:11});
      await new Promise(resolve=>map.once('idle',resolve));
      clearTimeout(liveTimer);S.layers.live=true;
      await refreshLive();clearTimeout(liveTimer);
      const badge=document.getElementById('liveBadge');
      const result={text:badge.textContent,className:badge.className};
      S.layers.live=false;applyFilters();
      map.jumpTo({center:[-98.5,39.4],zoom:3.4});
      return result;
    });
    assert.match(liveCapContract.text,/LIVE PARTIAL.*page ceiling reached.*zoom in/i,
      'capped live MLRS viewport results must be explicitly incomplete');
    assert.equal(liveCapContract.className,'err');

    const contract = await page.evaluate(() => {
      const input=document.getElementById('search');input.value='zz-no-release-match';
      input.dispatchEvent(new Event('input',{bubbles:true}));
      const searchNotice=document.getElementById('results').textContent;
      input.value='';input.dispatchEvent(new Event('input',{bubbles:true}));
      return ({
      states: [...document.querySelectorAll('.schip')].map(node => node.textContent.trim()),
      coverage_rows: (COVERAGE && COVERAGE.states || []).length,
      coverage_summary: COVERAGE && COVERAGE.summary,
      national_sources: [
        ['national-mrds','mrds'],['national-usmin','usmin'],
        ['national-stategeo','stategeo'],['national-claims','claims'],
        ['national-geology','geology'],['national-faults','faults'],
        ['national-ardf','ardf'],
      ].map(([id,key]) => {
        const source=map.getStyle().sources[id], entry=(MAN.national_baselines||{})[key]||{};
        return [id,source&&source.type,source&&source.url,'pmtiles://'+entry.file];
      }),
      source_layer_contract: [
        ['national-mrds-c','national-mrds','mrds'],
        ['national-usmin-c','national-usmin','usmin'],
        ['stategeo-c','national-stategeo','stategeo'],
        ['claimsA-dot','national-claims','active'],
        ['claimsC-dot','national-claims','closed'],
        ['national-geology-fill','national-geology','geology'],
        ['national-fault-line','national-faults','faults'],
        ['national-ardf-c','national-ardf','ardf'],
      ].map(([id,source,sourceLayer])=>{
        const layer=map.getStyle().layers.find(item=>item.id===id);
        return [id,layer&&layer.source,layer&&layer['source-layer'],source,sourceLayer];
      }),
      old_sources: ['mrds','usmin','stategeo','claimsA','claimsC']
        .filter(id => map.getSource(id)),
      has_legacy_store: typeof stores !== 'undefined',
      claim_states: [...CLAIM_STATES].sort(),
      nonclaim_semantics: execQueryClaims({states:['MI']}),
      genuine_zero_semantics: execQueryClaims({states:['FL'],layer:'closed',system:'federal'}),
      unloaded_release_semantics: execQueryClaims({states:['NV'],layer:'active',system:'federal'}),
      search_notice: searchNotice,
      unfiltered_federal_semantics: execQueryClaims({layer:'active',system:'federal'}),
      missing_mode_semantics: execQueryClaims({states:['CA'],layer:'closed',system:'federal'}),
      alaska_state_semantics: execQueryClaims({states:['AK'],layer:'active',system:'alaska_state'}),
      alaska_active_inventory_complete: ['active','pending'].every(mode=>{
        const base=(MAN.national_baselines||{}).alaska_state_claims||{};
        const row=(base.source_id_inventory||{})[mode]||{};
        const count=Number((base.by_status||{})[mode]);
        return row.status==='complete_at_retrieval'&&
          Number(row.source_records)===count&&
          Number(row.maxzoom_unique_tiled_ids)===count&&
          row.disjoint_union_complete===true&&
          Number(row.base_records)+Number(row.precision_records)===count;
      }),
      alaska_boot:{contract:!!alaskaClaimContract(),
        baseSource:!!map.getSource('ak-state-claims'),
        precisionSource:!!map.getSource('ak-state-claims-precision'),
        baseProtocol:PMT_PROTOCOL.tiles.has(
          MAN.national_baselines.alaska_state_claims.file),
        precisionProtocol:PMT_PROTOCOL.tiles.has(
          MAN.national_baselines.alaska_state_claims.precision_overflow.file)},
      alaska_layer_text: document.querySelector('[data-layer="akClaimsA"]')?.parentElement?.textContent||'',
      alaska_federal_semantics: execQueryClaims({states:['AK'],layer:'active',system:'federal'}),
      alaska_system_outside_ak: execQueryClaims({states:['CA'],layer:'active',system:'alaska_state'}),
      state_chip_layout: (()=>{const box=document.getElementById('stateChips'), chips=[...box.children];
        const br=box.getBoundingClientRect();return {client:box.clientWidth,scroll:box.scrollWidth,
          inside:chips.every(ch=>{const r=ch.getBoundingClientRect();return r.left>=br.left-.5&&r.right<=br.right+.5;})};})(),
    });});
    assert.deepEqual(contract.states, EXPECTED_STATES, 'state chips must be exact WS11 scope/order');
    assert.equal(contract.states.includes('HI'), false, 'Hawaii must be absent');
    assert.equal(contract.coverage_rows, 49, 'coverage grid must render 49 states');
    assert.equal(contract.coverage_summary.states, 49, 'coverage summary must cover 49 states');
    for(const [id,type,url,expectedUrl] of contract.national_sources){
      assert.equal(type,'vector',`${id} must be a vector source`);
      assert.equal(url,expectedUrl,`${id} must use its advertised PMTiles artifact`);
    }
    assert.deepEqual(contract.alaska_boot,{contract:true,baseSource:false,
      precisionSource:false,baseProtocol:false,precisionProtocol:false},
      'national boot below z8 must allocate no Alaska claim archive/protocol');
    assert.deepEqual(alaskaPmtilesRequests,[],
      'national boot below z8 must make no Alaska claim PMTiles request');
    for(const [id,source,sourceLayer,expectedSource,expectedLayer] of contract.source_layer_contract){
      assert.equal(source,expectedSource,`${id} source mismatch`);
      assert.equal(sourceLayer,expectedLayer,`${id} source-layer mismatch`);
    }
    assert.deepEqual(contract.claim_states,
      ['AK','AR','AZ','CA','CO','FL','ID','LA','MS','MT','ND','NE','NM','NV','OR','SD','UT','WA','WY'],
      'claim-state regime must match exact BLM scope');
    assert.equal(contract.nonclaim_semantics.status, 'not_applicable');
    assert.equal(contract.nonclaim_semantics.count, null, 'non-claim N/A must not collapse to zero');
    assert.equal(contract.genuine_zero_semantics.status, 'measured');
    assert.equal(contract.genuine_zero_semantics.count, 0,
      'a published empty state/mode is a genuine tile-scoped zero');
    assert.equal(contract.genuine_zero_semantics.exact_count, 0,
      'descriptor n=0 must remain an exact zero rather than unknown or N/A');
    assert.equal(contract.unloaded_release_semantics.status,'measured',
      'manifest completion remains measured while feature tiles are lazy');
    assert.equal(contract.unloaded_release_semantics.exact_count,nvActive,
      'lazy feature loading must not erase the exact manifest count');
    assert.equal(contract.unloaded_release_semantics.count,null,
      'unloaded nonzero release must never look like a tile-scoped zero');
    assert.equal(contract.unloaded_release_semantics.tile_query_status,'not_loaded');
    assert.ok(contract.unloaded_release_semantics.unloaded_publications.length>0);
    assert.match(contract.search_notice,/released claim publication.*not searched/i,
      'search must disclose unloaded release publications instead of plain no-match');
    assert.equal(contract.unfiltered_federal_semantics.status, 'incomplete',
      'an unfiltered federal query must not imply all 19 claim states are published');
    assert.equal(contract.unfiltered_federal_semantics.exact_count, null,
      'a partial national federal publication has no exact national count');
    assert.ok(contract.unfiltered_federal_semantics.availability.federal.unknown_states.includes('AK'),
      'missing federal Alaska must remain explicit in an unfiltered query');
    assert.equal(contract.missing_mode_semantics.status, 'unknown');
    assert.equal(contract.missing_mode_semantics.count, null, 'missing CA closed snapshot must not collapse to zero');
    assert.equal(contract.missing_mode_semantics.by_system.federal.count, null,
      'an unpublished federal mode must not expose a misleading subsystem zero');
    assert.equal(contract.alaska_state_semantics.status,
      contract.alaska_active_inventory_complete?'measured':'incomplete',
      'Alaska source counts may be measured only after exact PMTiles ID reconciliation');
    assert.equal(contract.alaska_state_semantics.exact_count,
      contract.alaska_active_inventory_complete?
        contract.alaska_state_semantics.availability.alaska_state.known_published_count:null);
    if(!contract.alaska_active_inventory_complete)
      assert.match(contract.alaska_layer_text,/INCOMPLETE.*source row.*absent/is,
        'the normal Alaska layer UI must expose a partial baseline');
    assert.equal(contract.alaska_state_semantics.by_system.federal.status, 'not_requested');
    assert.equal(contract.alaska_state_semantics.by_system.federal.count, null);
    assert.equal(contract.alaska_federal_semantics.status, 'unknown');
    assert.equal(contract.alaska_federal_semantics.count, null,
      'missing federal Alaska MLRS must stay unknown despite the DNR archive');
    assert.equal(contract.alaska_system_outside_ak.status, 'not_applicable');
    assert.equal(contract.alaska_system_outside_ak.count, null,
      'the Alaska state system is N/A outside Alaska, never a zero');
    assert.ok(contract.state_chip_layout.scroll <= contract.state_chip_layout.client + 1,
      `state controls overflow: ${JSON.stringify(contract.state_chip_layout)}`);
    assert.equal(contract.state_chip_layout.inside, true, 'all state controls must be visible inside sidebar');
    assert.deepEqual(contract.old_sources, [], 'legacy whole-state map sources must be gone');
    assert.equal(contract.has_legacy_store, false, 'legacy browser row store must be gone');

    await page.waitForFunction(()=>{
      const button=document.getElementById('btnAlerts');return button&&button.style.display!== 'none';
    },null,{timeout:30_000});
    const alertContract=await page.evaluate(()=>{
      const pendingTitle=document.getElementById('btnAlerts').title;
      document.getElementById('btnAlerts').click();
      const pendingBody=document.getElementById('alertsBody').textContent;
      renderAlerts({generated:'2026-08-13',mode:'test',active_now:0,alerts:[]},[],{
        generated:'2026-08-13',active_now:1,alerts:[{kind:'RENT DUE',system_id:'alaska_state_claims',
          serial:'ADL 1',name:'Acceptance claim',rent_due:'2026-11-30',
          rent_received_grace_ends:'2026-12-30',abandonment_if_unpaid:'2026-12-31',
          labor_or_cash_due:'2026-09-01',labor_statement_due:'2026-12-01',
          sources:['https://dnr.alaska.gov/']} ]});
      const seasonalBody=document.getElementById('alertsBody').textContent;
      document.querySelector('#alertsBody .alrow').click();
      const detail=document.getElementById('detail').textContent;
      return {pendingTitle,pendingBody,seasonalBody,detail};
    });
    if(alertContract.pendingTitle){
      assert.match(alertContract.pendingTitle,/not published yet|pending/i);
      assert.match(alertContract.pendingBody,/ALASKA STATE WATCH PENDING/);
    }
    for(const date of ['2026-11-30','2026-12-30','2026-12-31','2026-09-01','2026-12-01'])
      assert.match(alertContract.seasonalBody,new RegExp(date),`Alaska watch must render ${date}`);
    assert.match(alertContract.detail,/ALASKA STATE SYSTEM/);

    // Every state switch must survive a real off/on round trip. This is the
    // baseline toggle acceptance; release-specific artifact toggles remain
    // guarded by coverage.enabled && coverage.gate_passed.
    for (const state of EXPECTED_STATES) {
      await page.evaluate(code => {
        const chip = [...document.querySelectorAll('.schip')]
          .find(node => node.textContent.trim() === code);
        chip.click();
        if (S.states[code] !== false || chip.classList.contains('on'))
          throw new Error(`${code} did not switch off`);
        chip.click();
        if (S.states[code] !== true || !chip.classList.contains('on'))
          throw new Error(`${code} did not switch on`);
      }, state);
    }
    await settle(page);

    // Released COGs are opt-in, viewport/zoom bounded, and fully torn down.
    // Two distant synthetic state descriptors prove national boot does not
    // fan out to every raster endpoint and a state-off transition frees GPU
    // and network resources rather than merely hiding the layer.
    await page.waitForSelector('#ws11-aeromag-tgl',{state:'attached',timeout:30_000});
    const releasedRasterBoot=await page.evaluate(()=>(
      ['ws11-raster-nv-aeromag','ws11-raster-fl-aeromag']
        .filter(id=>map.getSource(id))));
    assert.deepEqual(releasedRasterBoot,[],'released COG sources must be absent at boot');
    assert.equal(releaseCogRequests.length,0,'released COG endpoints must be untouched at boot');
    await page.evaluate(()=>{
      document.getElementById('ws11-aeromag-tgl').click();
      map.jumpTo({center:[-117,38.5],zoom:6});
    });
    await settle(page);
    const releasedRasterOn=await page.evaluate(()=>{
      const source=map.getStyle().sources['ws11-raster-nv-aeromag'];
      const layer=map.getStyle().layers.find(item=>item.id==='ws11-nv-aeromag');
      return {source,layer:layer&&{source:layer.source,minzoom:layer.minzoom,maxzoom:layer.maxzoom},
        flSource:!!map.getSource('ws11-raster-fl-aeromag')};
    });
    assert.equal(releasedRasterOn.source.type,'raster');
    assert.deepEqual(releasedRasterOn.source.bounds,[-120,35,-114,42]);
    assert.equal(releasedRasterOn.source.minzoom,4);
    assert.equal(releasedRasterOn.source.maxzoom,12);
    assert.deepEqual(releasedRasterOn.layer,
      {source:'ws11-raster-nv-aeromag',minzoom:4,maxzoom:12});
    assert.equal(releasedRasterOn.flSource,false,
      'off-viewport Florida COG must remain unallocated in Nevada');
    assert.ok(releaseCogRequests.some(path=>path.includes('/aeromag/nv/')),
      'enabled in-view Nevada COG must request tiles');
    assert.equal(releaseCogRequests.some(path=>path.includes('/aeromag/fl/')),false,
      'off-viewport Florida COG must make no request');
    await page.evaluate(()=>{
      document.querySelector('.schip[data-state="NV"]').click();
    });
    await page.waitForFunction(() => !map.getSource('ws11-raster-nv-aeromag')&&
      !map.getLayer('ws11-nv-aeromag'));
    await page.evaluate(()=>{
      document.querySelector('.schip[data-state="NV"]').click();
      document.getElementById('ws11-aeromag-tgl').click();
      map.jumpTo({center:[-98.5,39.4],zoom:3.4});
    });
    await settle(page);
    const releasedRasterOff=await page.evaluate(()=>(
      !map.getSource('ws11-raster-nv-aeromag')&&!map.getLayer('ws11-nv-aeromag')));
    assert.equal(releasedRasterOff,true,'released COG toggle-off must remove source and layer');

    await waitForSourceFeatures(page,[
      ['admin','states'],['national-mrds','mrds'],['national-usmin','usmin'],
      ['national-stategeo','stategeo'],['national-claims','active'],
    ]);
    const bootFeatureCounts=await page.evaluate(()=>({
      states:map.querySourceFeatures('admin',{sourceLayer:'states'}).length,
      mrds:map.querySourceFeatures('national-mrds',{sourceLayer:'mrds'}).length,
      usmin:map.querySourceFeatures('national-usmin',{sourceLayer:'usmin'}).length,
      stategeo:map.querySourceFeatures('national-stategeo',{sourceLayer:'stategeo'}).length,
      activeClaims:map.querySourceFeatures('national-claims',{sourceLayer:'active'}).length,
    }));
    for(const [source,n] of Object.entries(bootFeatureCounts))
      assert.ok(n>0,`national baseline ${source} must yield features, got ${n}`);
    const nationalResearch=await page.evaluate(()=>{
      const feature=map.querySourceFeatures('national-mrds',{sourceLayer:'mrds'})
        .find(item=>item.properties&&item.properties.nm);
      if(!feature)throw new Error('named MRDS feature unavailable for research-link check');
      showFeature({layer:{id:'national-mrds-c'},source:'national-mrds',
        properties:feature.properties,geometry:feature.geometry});
      return [...document.querySelectorAll('#detailInner .links a')].map(node=>({
        text:node.textContent,href:node.href,
      }));
    });
    for(const label of ['Chronicling America','SEC EDGAR','SEDAR+'])
      assert.ok(nationalResearch.some(link=>link.text.includes(label)),
        `national MRDS card must expose ${label} research`);
    assert.ok(nationalResearch.find(link=>link.text.includes('Chronicling America')).href.includes('q='),
      'Chronicling America research link must carry the feature name query');
    assert.ok(nationalResearch.find(link=>link.text.includes('SEC EDGAR')).href.includes('#/q='),
      'EDGAR research link must be name-prefilled');

    // The aeromagnetic survey index is itself tiled provenance. Exercise its
    // real checkbox and require data rather than merely inspecting the source.
    await page.waitForSelector('#gp-surv-tgl',{state:'attached',timeout:30_000});
    await page.evaluate(()=>{
      const toggle=document.getElementById('gp-surv-tgl');
      if(!toggle)throw new Error('aeromagnetic survey-index toggle missing');
      toggle.click();
    });
    await settle(page);
    await waitForSourceFeatures(page,[['gp-surveys','surveys']]);
    const geophysFeatures=await page.evaluate(() =>
      map.querySourceFeatures('gp-surveys',{sourceLayer:'surveys'}).length);
    assert.ok(geophysFeatures>0,'national aeromagnetic survey index must yield features');
    await page.evaluate(()=>document.getElementById('gp-surv-tgl').click());

    // Exercise the national aeromagnetic baseline as a real lazy raster
    // lifecycle. Its exact USGS WMTS path is deterministically stubbed above;
    // unrelated APIs remain blocked and therefore cannot be masked by CI.
    await page.waitForSelector('#gp-mag-tgl',{state:'attached',timeout:30_000});
    const magneticContract=await page.evaluate(async()=>{
      document.getElementById('gp-mag-tgl').click();
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      const source=map.getStyle().sources['gp-mag'];
      const layer=map.getStyle().layers.find(item=>item.id==='gp-mag');
      return {source_type:source&&source.type,tiles:source&&source.tiles,
        layer_source:layer&&layer.source};
    });
    assert.equal(magneticContract.source_type,'raster','national aeromag must use a raster source');
    assert.equal(magneticContract.layer_source,'gp-mag','national aeromag layer/source mismatch');
    assert.deepEqual(magneticContract.tiles,[
      'https://mrdata.usgs.gov/mapcache/wmts/1.0.0/magnetic/default/GoogleMapsCompatible/{z}/{y}/{x}.png'
    ],'national aeromag must retain the reviewed USGS WMTS identity');
    await settle(page);
    await page.evaluate(()=>document.getElementById('gp-mag-tgl').click());
    const magneticReleased=await page.evaluate(()=>(
      !map.getSource('gp-mag')&&!map.getLayer('gp-mag')));
    assert.equal(magneticReleased,true,'disabled aeromag must release its raster source and layer');
    const boot = await sample(page, 'all-49-baseline');

    // Exercise the densest compatibility-claim area with closed claims on.
    await page.evaluate(() => {
      const row=document.querySelector('[data-layer="claimsC"]');
      if(!row||!row.classList.contains('off'))throw new Error('closed-claim UI toggle missing or unexpectedly on');
      row.click();
      map.jumpTo({center: [-116.0, 39.2], zoom: 8});
    });
    await settle(page, 45_000);
    const nevadaFeatures=await page.evaluate(() => map.querySourceFeatures('national-claims',{sourceLayer:'closed'}).length);
    assert.ok(nevadaFeatures>0,'dense Nevada view must load closed-claim PMTiles features');
    await waitForSourceFeatures(page,[['ws11-fixture-shared-federal-claims','active']]);
    const releasedClaimContract=await page.evaluate(()=>{
      const source='ws11-fixture-shared-federal-claims';
      const releaseRows=loadedTileFeatures(source,'active')
        .filter(feature=>(feature.properties||{}).st==='NV');
      const queried=execQueryClaims({states:['NV'],layer:'active',system:'federal',limit:20});
      const structured=runQuery({states:['NV'],scope:'claims',near:null,km:25});
      const feature=releaseRows.find(row=>String((row.properties||{}).serial||'').length>=2);
      const flZero=execQueryClaims({states:['FL'],layer:'closed',system:'federal',limit:20});
      let search=null;
      if(feature){
        const serial=String(feature.properties.serial);
        const input=document.getElementById('search');
        input.value=serial;input.dispatchEvent(new Event('input',{bubbles:true}));
        const matches=[...document.querySelectorAll('#results .r[data-j]')]
          .filter(node=>['FED RELEASE','CLAIM','CLSD'].includes(node.querySelector('.tag')?.textContent));
        search={serial,count:matches.length,tags:matches.map(node=>node.querySelector('.tag')?.textContent)};
      }
      const layer=map.getStyle().layers.find(row=>row.id==='ws11-nv-federal_mlrs-active');
      const zeroLayer=map.getStyle().layers.find(row=>row.id==='ws11-fl-federal_mlrs-closed');
      const compatibility=map.getStyle().layers.find(row=>row.id==='claimsA-dot');
      return {release_count:releaseRows.length,queried,
        structured_first:structured.hits[0]&&{
          source:structured.hits[0].source,publication:structured.hits[0].publication},
        search,render_filter:layer&&layer.filter,
        fl_zero:{status:flZero.status,count:flZero.count,exact_count:flZero.exact_count},
        release_sources:[layer&&layer.source,zeroLayer?zeroLayer.source:null],
        compatibility_filter:compatibility&&compatibility.filter};
    });
    assert.ok(releasedClaimContract.release_count>0,'synthetic released NV source must yield active features');
    assert.equal(releasedClaimContract.queried.count,releasedClaimContract.release_count,
      'released state active rows must replace, not add to, compatibility rows');
    assert.equal(releasedClaimContract.queried.status,'measured');
    assert.equal(releasedClaimContract.queried.exact_count,nvActive,
      'descriptor n must drive the exact released-state count');
    assert.equal(releasedClaimContract.queried.by_system.federal.exact_count,nvActive);
    assert.ok(releasedClaimContract.queried.sample.length>0);
    assert.ok(releasedClaimContract.queried.sample.every(row=>row.publication==='state_release'),
      'query samples must identify the authoritative release publication');
    assert.equal(releasedClaimContract.structured_first.source,'ws11-fixture-shared-federal-claims',
      'runQuery must prefer the released state source');
    assert.equal(releasedClaimContract.structured_first.publication,'state_release');
    assert.ok(releasedClaimContract.search,'released claim search fixture needs a serial');
    assert.equal(releasedClaimContract.search.count,1,
      'search must not return compatibility and release copies of one serial');
    assert.deepEqual(releasedClaimContract.search.tags,['FED RELEASE']);
    assert.deepEqual(releasedClaimContract.render_filter,
      ['==',['get','st'],'NV'],'shared archives must be rendered through their descriptor state filter');
    assert.deepEqual(releasedClaimContract.fl_zero,
      {status:'measured',count:0,exact_count:0},
      'off-viewport declared zero must remain exact zero, not unknown');
    assert.deepEqual(releasedClaimContract.release_sources,
      ['ws11-fixture-shared-federal-claims',null],
      'off-viewport logical layers must not allocate even when an archive is shared');
    const releaseProtocol=await page.evaluate(()=>({
      shared:PMT_PROTOCOL.get(MAN.national_baselines.claims.file).cache===PMT_CACHE,
      max:PMT_CACHE.maxCacheEntries,entries:PMT_CACHE.cache.size,
      urls:PMT_PROTOCOL.tiles.size,
    }));
    assert.equal(releaseProtocol.shared,true,'shared release/baseline URL must reuse bounded cache');
    assert.equal(releaseProtocol.max,256);
    assert.ok(releaseProtocol.entries<=256,
      `global PMTiles directory cache exceeded bound: ${JSON.stringify(releaseProtocol)}`);
    assert.equal(JSON.stringify(releasedClaimContract.compatibility_filter).includes('"NV"'),false,
      'compatibility rendering must exclude a superseded released state/mode');
    const dense = await sample(page, 'nevada-claims-dense');

    // Exercise the newly published 49-state USGS geology/fault PMTiles as
    // rendered data, not merely as manifest entries or hidden sources.
    await page.waitForSelector('#nationalGeoTgl',{state:'attached',timeout:30_000});
    await page.waitForSelector('#nationalFaultTgl',{state:'attached',timeout:30_000});
    await page.evaluate(()=>{
      document.getElementById('nationalGeoTgl').click();
      document.getElementById('nationalFaultTgl').click();
    });
    await settle(page,45_000);
    await waitForSourceFeatures(page,[
      ['national-geology','geology'],['national-faults','faults'],
    ],45_000);
    const geologyContract=await page.evaluate(()=>{
      const required=['st','state','src','source_dataset','source_id','source_scale',
        'source_scale_status','source_ref','source_url'];
      const geology=map.querySourceFeatures('national-geology',{sourceLayer:'geology'});
      const faults=map.querySourceFeatures('national-faults',{sourceLayer:'faults'});
      const valid=feature=>Number.isFinite(+(feature.properties||{}).fid)&&
        required.every(key=>typeof (feature.properties||{})[key]==='string');
      return {geology:geology.length,faults:faults.length,
        geologySchema:geology.slice(0,100).every(valid),
        faultSchema:faults.slice(0,100).every(valid),
        geologyRendered:map.queryRenderedFeatures({layers:['national-geology-fill']}).length,
        faultsRendered:map.queryRenderedFeatures({layers:['national-fault-line']}).length};
    });
    assert.ok(geologyContract.geology>0&&geologyContract.geologyRendered>0,
      `Nevada geology must load and render: ${JSON.stringify(geologyContract)}`);
    assert.ok(geologyContract.faults>0&&geologyContract.faultsRendered>0,
      `Nevada faults must load and render: ${JSON.stringify(geologyContract)}`);
    assert.equal(geologyContract.geologySchema,true,
      'every sampled geology feature must expose source/scale provenance');
    assert.equal(geologyContract.faultSchema,true,
      'every sampled fault feature must expose source/scale provenance');
    const geology = await sample(page, 'nevada-geology-faults');
    await page.evaluate(()=>{
      document.getElementById('nationalGeoTgl').click();
      document.getElementById('nationalFaultTgl').click();
    });

    // Nevada-first state-survey baselines are distinct from the national
    // overview and from a WS11 release. They must allocate only after a real
    // opt-in Nevada view, expose exact source layers/provenance, then tear
    // their sources and protocol registrations down again.
    for(const id of ['nvDs249Tgl','nvOneGeologyTgl','nvDistrictsTgl'])
      await page.waitForSelector('#'+id,{state:'attached',timeout:30_000});
    assert.deepEqual(await page.evaluate(()=>[
      !!map.getSource('nv-ds249'),!!map.getSource('nv-onegeology'),
      !!map.getSource('nv-districts')]),[false,false,false],
      'Nevada state-survey archives must be lazy at boot/category-off');
    await page.evaluate(()=>{
      for(const id of ['nvDs249Tgl','nvOneGeologyTgl','nvDistrictsTgl'])
        document.getElementById(id).click();
    });
    await settle(page,60_000);
    await waitForSourceFeatures(page,[
      ['nv-ds249','nv_ds249_geology'],['nv-ds249','nv_ds249_faults'],
      ['nv-onegeology','nv_nbmg_onegeology_250k'],
      ['nv-districts','nv_nbmg_mining_districts'],
    ],60_000);
    const nvSurveyContract=await page.evaluate(()=>{
      const specs=[
        ['nv-ds249','nv_ds249_geology'],['nv-ds249','nv_ds249_faults'],
        ['nv-onegeology','nv_nbmg_onegeology_250k'],
        ['nv-districts','nv_nbmg_mining_districts'],
      ];
      const rows=specs.map(([source,layer])=>{
        const features=map.querySourceFeatures(source,{sourceLayer:layer});
        const valid=features.slice(0,100).every(feature=>{
          const p=feature.properties||{};
          return p.st==='NV'&&Number.isFinite(+p.fid)&&
            ['source_dataset','source_id','source_scale','source_scale_status',
             'source_ref','source_url','publication_id'].every(
               key=>typeof p[key]==='string'&&p[key].length>0);
        });
        return {source,layer,n:features.length,valid};
      });
      return {rows,rendered:{
        dsGeology:map.queryRenderedFeatures({layers:['nv-ds249-geology']}).length,
        dsFaults:map.queryRenderedFeatures({layers:['nv-ds249-faults']}).length,
        oneGeology:map.queryRenderedFeatures({layers:['nv-onegeology-fill']}).length,
        districts:map.queryRenderedFeatures({layers:['nv-districts-fill']}).length,
      }};
    });
    assert.ok(nvSurveyContract.rows.every(row=>row.n>0&&row.valid),
      `Nevada state-survey source/schema failed: ${JSON.stringify(nvSurveyContract)}`);
    assert.ok(Object.values(nvSurveyContract.rendered).every(value=>value>0),
      `Nevada state-survey layers must render: ${JSON.stringify(nvSurveyContract)}`);
    await sample(page,'nevada-state-survey-baselines');
    await page.evaluate(()=>{
      for(const id of ['nvDs249Tgl','nvOneGeologyTgl','nvDistrictsTgl'])
        document.getElementById(id).click();
    });
    await page.waitForFunction(()=>['nv-ds249','nv-onegeology','nv-districts']
      .every(id=>!map.getSource(id)));
    assert.deepEqual(await page.evaluate(()=>[
      MAN.national_baselines.nv_usgs_ds249.file,
      MAN.national_baselines.nv_nbmg_onegeology_250k.file,
      MAN.national_baselines.nv_nbmg_mining_districts.file,
    ].map(url=>PMT_PROTOCOL.tiles.has(url))),[false,false,false],
      'Nevada state-survey teardown must release all lazy protocol instances');

    // Arizona uses the same generic lifecycle with its reviewed compatibility
    // presentation. All three archives must stay absent until AZ is selected,
    // their category toggles are on, the viewport intersects, and z>=5.5.
    for(const id of ['azMap35Tgl','azDistrictsTgl','azCriticalTgl'])
      await page.waitForSelector('#'+id,{state:'attached',timeout:30_000});
    await page.evaluate(()=>{
      setUiStates(['AZ']);applyFilters();
      map.jumpTo({center:[-111.9,34.2],zoom:5.8});
      for(const id of ['azMap35Tgl','azDistrictsTgl','azCriticalTgl'])
        document.getElementById(id).click();
    });
    await settle(page,60_000);
    await waitForSourceFeatures(page,[
      ['az-map35','az_azgs_map35_geology'],
      ['az-map35','az_azgs_map35_faults'],
      ['az-districts','az_azgs_mining_districts'],
      ['az-critical-minerals','az_azgs_critical_minerals'],
    ],60_000);
    const azSurveyContract=await page.evaluate(()=>{
      const specs=[
        ['az-map35','az_azgs_map35_geology','az-map35-geology-fill'],
        ['az-map35','az_azgs_map35_faults','az-map35-faults-line'],
        ['az-districts','az_azgs_mining_districts','az-districts-fill'],
        ['az-critical-minerals','az_azgs_critical_minerals','az-critical-minerals-circle'],
      ];
      const required=['st','source_dataset','source_id','source_record_id',
        'source_scale','source_scale_status','source_ref','source_url','publication_id'];
      const rows=specs.map(([source,sourceLayer,layer])=>{
        const features=map.querySourceFeatures(source,{sourceLayer});
        const invalid=features.slice(0,100).find(feature=>{
          const p=feature.properties||{};
          return p.st!=='AZ'||!Number.isFinite(+p.fid)||
            !required.every(key=>typeof p[key]==='string');
        });
        return {source,sourceLayer,layer,n:features.length,
          valid:features.slice(0,100).every(feature=>{
            const p=feature.properties||{};
            return p.st==='AZ'&&Number.isFinite(+p.fid)&&
              required.every(key=>typeof p[key]==='string');
          }),invalid:invalid&&Object.fromEntries(['fid',...required].map(
            key=>[key,[typeof (invalid.properties||{})[key],(invalid.properties||{})[key]]])),
          filter:map.getStyle().layers.find(row=>row.id===layer)?.filter};
      });
      const styles=Object.fromEntries([
        'az-map35-geology-fill','az-map35-faults-line','az-districts-fill',
        'az-districts-line','az-critical-minerals-circle',
      ].map(id=>[id,map.getStyle().layers.find(row=>row.id===id)?.type]));
      const feature=map.querySourceFeatures('az-map35',{
        sourceLayer:'az_azgs_map35_geology'})[0];
      if(feature)showFeature({layer:{id:'az-map35-geology-fill'},
        source:'az-map35',properties:feature.properties,geometry:feature.geometry});
      return {rows,styles,detail:document.getElementById('detailInner').textContent};
    });
    assert.ok(azSurveyContract.rows.every(row=>row.n>0&&row.valid),
      `Arizona state-survey source/schema failed: ${JSON.stringify(azSurveyContract)}`);
    assert.deepEqual(azSurveyContract.styles,{
      'az-map35-geology-fill':'fill','az-map35-faults-line':'line',
      'az-districts-fill':'fill','az-districts-line':'line',
      'az-critical-minerals-circle':'circle',
    },'Arizona must expose the reviewed fill/line/fill/line/circle presentation');
    assert.ok(azSurveyContract.rows.every(row=>
      JSON.stringify(row.filter)===JSON.stringify(['==',['get','st'],'AZ'])),
      'every Arizona state-survey style layer needs an exact state filter');
    assert.match(azSurveyContract.detail,/AZ STATE-SURVEY BASELINE/);
    assert.match(azSurveyContract.detail,/Source scale|Scale provenance/);
    const arizonaSurvey = await sample(page,'arizona-state-survey-baselines');
    await page.evaluate(()=>{
      for(const id of ['azMap35Tgl','azDistrictsTgl','azCriticalTgl'])
        document.getElementById(id).click();
    });
    await page.waitForFunction(()=>['az-map35','az-districts','az-critical-minerals']
      .every(id=>!map.getSource(id)));
    assert.deepEqual(await page.evaluate(()=>[
      MAN.national_baselines.az_azgs_map35_2025.file,
      MAN.national_baselines.az_azgs_mining_districts.file,
      MAN.national_baselines.az_azgs_critical_minerals.file,
    ].map(url=>PMT_PROTOCOL.tiles.has(url))),[false,false,false],
      'Arizona category-off must release every lazy PMTiles protocol instance');

    // Colorado is the first state whose builder emits its presentation
    // descriptors. Exercise all five logical layers to prevent the generic
    // compiler from accidentally depending on an AZ/NV compatibility key.
    const coToggles=await page.evaluate(()=>STATE_SURVEY_LAYERS
      .filter(row=>row.state==='CO').map(row=>row.toggle_id));
    for(const id of coToggles)
      await page.waitForSelector('#'+id,{state:'attached',timeout:30_000});
    await page.evaluate(ids=>{
      setUiStates(['CO']);applyFilters();map.jumpTo({center:[-105.5,39],zoom:6.2});
      for(const id of ids)document.getElementById(id).click();
    },coToggles);
    await settle(page,60_000);
    const coSpecs=await page.evaluate(()=>STATE_SURVEY_LAYERS
      .filter(row=>row.state==='CO').flatMap(row=>row.layers.map(layer=>
        [row.source_id,layer.source_layer,layer.id])));
    await waitForSourceFeatures(page,coSpecs.map(row=>row.slice(0,2)),60_000);
    const coSurveyContract=await page.evaluate(specs=>specs.map(
      ([source,sourceLayer,layer])=>{
        const features=map.querySourceFeatures(source,{sourceLayer});
        const style=map.getStyle().layers.find(row=>row.id===layer);
        return {source,sourceLayer,layer,n:features.length,type:style&&style.type,
          filter:style&&style.filter,valid:features.slice(0,100).every(feature=>{
            const p=feature.properties||{};
            return p.st==='CO'&&Number.isFinite(+p.fid)&&
              ['source_dataset','source_id','source_scale','source_scale_status',
               'source_ref','source_url','publication_id'].every(
                 key=>typeof p[key]==='string');
          })};
      }),coSpecs);
    assert.equal(coSurveyContract.length,5,
      'Colorado embedded descriptors must compile five logical layers');
    assert.ok(coSurveyContract.every(row=>row.n>0&&row.valid),
      `Colorado state-survey source/schema failed: ${JSON.stringify(coSurveyContract)}`);
    assert.ok(coSurveyContract.every(row=>
      JSON.stringify(row.filter)===JSON.stringify(['==',['get','st'],'CO'])),
      'every embedded Colorado layer needs an exact state filter');
    const coloradoSurvey = await sample(page,'colorado-state-survey-baselines');
    await page.evaluate(ids=>ids.forEach(id=>document.getElementById(id).click()),coToggles);
    await page.waitForFunction(()=>STATE_SURVEY_LAYERS.filter(row=>row.state==='CO')
      .every(row=>!map.getSource(row.source_id)&&!PMT_PROTOCOL.tiles.has(row.file)));

    // Utah's four-entry atomic fixture is builder-descriptor-only. Exercise
    // its split z5/z6 activation, exact schema/state filters, popup/search,
    // Range delivery, and both category-off and state-off teardown paths.
    const utToggles=await page.evaluate(()=>STATE_SURVEY_LAYERS
      .filter(row=>row.state==='UT').map(row=>row.toggle_id));
    assert.equal(utToggles.length,4,'Utah atomic group must expose four toggles');
    for(const id of utToggles)
      await page.waitForSelector('#'+id,{state:'attached',timeout:30_000});
    await page.evaluate(ids=>{
      setUiStates(['UT']);applyFilters();
      map.jumpTo({center:[-111.9,39.3],zoom:4.7});
      ids.forEach(id=>document.getElementById(id).click());
    },utToggles);
    await settle(page,45_000);
    assert.deepEqual(utahPmtilesRequests,[],
      'Utah fixture must receive no request below the first z5 activation');
    assert.deepEqual(await page.evaluate(()=>({
      sources:STATE_SURVEY_LAYERS.filter(row=>row.state==='UT'&&map.getSource(row.source_id))
        .map(row=>row.source_id),
      protocols:STATE_SURVEY_LAYERS.filter(row=>row.state==='UT'&&
        PMT_PROTOCOL.tiles.has(row.file)).map(row=>row.file),
    })),{sources:[],protocols:[]},
    'below z5 Utah must allocate neither sources nor protocol instances');

    await page.evaluate(()=>map.jumpTo({center:[-111.9,39.3],zoom:5.25}));
    await page.waitForFunction(()=>STATE_SURVEY_LAYERS.filter(row=>row.state==='UT')
      .filter(row=>row.activation_minzoom<=5.25).every(row=>map.getSource(row.source_id)));
    await settle(page,45_000);
    await waitForSourceFeatures(page,[
      ['state-survey-ut-ugs-map179dm-500k','ut_ugs_map179dm_geology'],
      ['state-survey-ut-ugs-map179dm-500k','ut_ugs_map179dm_structures'],
      ['state-survey-ut-ugs-ds7-quaternary-faults','ut_ugs_ds7_quaternary_faults'],
      ['state-survey-ut-ugs-ofr695-mining-districts','ut_ugs_ofr695_mining_districts'],
    ],45_000);
    const utZ5=await page.evaluate(()=>({
      sources:STATE_SURVEY_LAYERS.filter(row=>row.state==='UT'&&map.getSource(row.source_id))
        .map(row=>row.source_id).sort(),
      protocols:STATE_SURVEY_LAYERS.filter(row=>row.state==='UT'&&
        PMT_PROTOCOL.tiles.has(row.file)).map(row=>row.file).sort(),
      structuresRendered:map.queryRenderedFeatures({
        layers:['ut_ugs_map179dm_structures_baseline']}).length,
      filters:STATE_SURVEY_LAYERS.filter(row=>row.state==='UT'&&
        row.activation_minzoom<=5.25).flatMap(row=>row.layers.map(layer=>
          map.getStyle().layers.find(style=>style.id===layer.id)?.filter)),
    }));
    assert.deepEqual(utZ5.sources,[
      'state-survey-ut-ugs-ds7-quaternary-faults',
      'state-survey-ut-ugs-map179dm-500k',
      'state-survey-ut-ugs-ofr695-mining-districts',
    ],'z5 must allocate exactly Utah geology/fault/district sources');
    assert.equal(utZ5.protocols.length,3,'z5 must retain exactly three Utah protocols');
    assert.equal(utZ5.structuresRendered,0,
      'Map 179DM structures must remain hidden below their independent z6 minimum');
    assert.ok(utZ5.filters.every(filter=>
      JSON.stringify(filter)===JSON.stringify(['==',['get','st'],'UT'])),
    `z5 Utah filters changed: ${JSON.stringify(utZ5.filters)}`);
    const z5Paths=new Set(utahPmtilesRequests.map(request=>request.pathname));
    assert.ok(z5Paths.has('/__fixture__/ut/ugs-map179dm-500k.pmtiles'));
    assert.ok(z5Paths.has('/__fixture__/ut/ugs-ds7-quaternary-faults.pmtiles'));
    assert.ok(z5Paths.has('/__fixture__/ut/ugs-ofr695-mining-districts.pmtiles'));
    assert.equal(z5Paths.has('/__fixture__/ut/ugs-ofr757-umos.pmtiles'),false,
      'UMOS archive must receive no request below z6');

    await page.evaluate(()=>map.jumpTo({center:[-111.9,39.3],zoom:6.2}));
    await page.waitForFunction(()=>STATE_SURVEY_LAYERS.filter(row=>row.state==='UT')
      .every(row=>map.getSource(row.source_id)));
    await settle(page,45_000);
    const utSpecs=await page.evaluate(()=>STATE_SURVEY_LAYERS
      .filter(row=>row.state==='UT').flatMap(row=>row.layers.map(layer=>
        [row.source_id,layer.source_layer,layer.id])));
    await waitForSourceFeatures(page,utSpecs.map(row=>row.slice(0,2)),45_000);
    const utContract=await page.evaluate(specs=>{
      const rows=STATE_SURVEY_LAYERS.filter(row=>row.state==='UT').flatMap(
        descriptor=>descriptor.layers.map(layer=>{
          const features=map.querySourceFeatures(descriptor.source_id,
            {sourceLayer:layer.source_layer});
          const style=map.getStyle().layers.find(row=>row.id===layer.id);
          const ids=[...new Set(features.map(feature=>+feature.properties.fid))].sort((a,b)=>a-b);
          return {source:descriptor.source_id,sourceLayer:layer.source_layer,
            layer:layer.id,n:features.length,ids,type:style&&style.type,
            minzoom:style&&style.minzoom,filter:style&&style.filter,
            required:layer.required_properties,
            title:features[0]&&stateSurveyFeatureTitle(layer,features[0].properties),
            valid:features.length>0&&features.every(feature=>{
              const p=feature.properties||{};
              return p.st==='UT'&&Number.isFinite(+p.fid)&&
                layer.required_properties.every(key=>key==='fid'
                  ?Number.isFinite(+p[key]):typeof p[key]==='string'&&p[key].length>0);
            }),rendered:map.queryRenderedFeatures({layers:[layer.id]})
              .filter(feature=>(feature.properties||{}).st==='UT').length};
        }));
      const umos=map.querySourceFeatures('state-survey-ut-ugs-ofr757-umos',
        {sourceLayer:'ut_ugs_ofr757_umos'})[0];
      if(umos)showFeature({layer:{id:'ut_ugs_ofr757_umos_baseline'},
        source:'state-survey-ut-ugs-ofr757-umos',properties:umos.properties,
        geometry:umos.geometry});
      const popup=document.getElementById('detailInner').textContent;
      const input=document.getElementById('search');
      input.value='Acceptance UMOS Gold Prospect';
      input.dispatchEvent(new Event('input',{bubbles:true}));
      const search=[...document.querySelectorAll('#results .r[data-j]')].map(node=>({
        tag:node.querySelector('.tag')?.textContent,
        name:node.querySelector('.nm')?.textContent,
        state:node.querySelector('.sb')?.textContent}));
      return {rows,popup,search,
        sources:STATE_SURVEY_LAYERS.filter(row=>row.state==='UT'&&
          map.getSource(row.source_id)).length,
        protocols:STATE_SURVEY_LAYERS.filter(row=>row.state==='UT'&&
          PMT_PROTOCOL.tiles.has(row.file)).length};
    },utSpecs);
    const expectedUt={
      ut_ugs_map179dm_geology:{id:101,type:'fill',minzoom:5,
        required:['fid','st','source_dataset','source_id','source_record_id',
          'source_scale','source_scale_status','source_ref','source_url','publication_id',
          'map_unit','unit_name','unit_age'],title:'Acceptance Jurassic sandstone'},
      ut_ugs_map179dm_structures:{id:202,type:'line',minzoom:6,
        required:['fid','st','source_dataset','source_id','source_record_id',
          'source_scale','source_scale_status','source_ref','source_url','publication_id',
          'feature_type','feature_subtype','location_modifier'],title:'Acceptance normal fault'},
      ut_ugs_ds7_quaternary_faults:{id:303,type:'line',minzoom:5,
        required:['fid','st','source_dataset','source_id','source_record_id',
          'source_scale','source_scale_status','source_ref','source_url','publication_id',
          'fault_age','mapped_scale','mapping_constraint'],title:'Mapped structure'},
      ut_ugs_ofr695_mining_districts:{id:404,type:'fill',minzoom:5,
        required:['fid','st','source_dataset','source_id','source_record_id',
          'source_scale','source_scale_status','source_ref','source_url','publication_id',
          'district_name','boundary_status'],title:'Acceptance Tintic Mining District'},
      ut_ugs_ofr757_umos:{id:505,type:'circle',minzoom:6,
        required:['fid','st','source_dataset','source_id','source_record_id',
          'source_scale','source_scale_status','source_ref','source_url','publication_id',
          'site_name','commodity','occurrence_scope'],title:'Acceptance UMOS Gold Prospect'},
    };
    assert.equal(utContract.rows.length,5,
      'Utah four archives must compile into five logical layers');
    for(const row of utContract.rows){
      const expected=expectedUt[row.sourceLayer];
      assert.ok(expected,`unexpected Utah source layer ${row.sourceLayer}`);
      assert.equal(row.valid,true,`Utah required properties failed: ${JSON.stringify(row)}`);
      assert.deepEqual(row.ids,[expected.id],`Utah tile query IDs changed for ${row.sourceLayer}`);
      assert.equal(row.type,expected.type);assert.equal(row.minzoom,expected.minzoom);
      assert.deepEqual(row.filter,['==',['get','st'],'UT']);
      assert.deepEqual(row.required,expected.required);
      assert.equal(row.title,expected.title);
      assert.ok(row.rendered>0,`Utah layer did not render: ${JSON.stringify(row)}`);
    }
    assert.equal(utContract.sources,4);assert.equal(utContract.protocols,4);
    assert.match(utContract.popup,/Acceptance UMOS Gold Prospect/);
    assert.match(utContract.popup,/UT STATE-SURVEY BASELINE/);
    assert.match(utContract.popup,/Gold/);assert.match(utContract.popup,/prospect/);
    assert.match(utContract.popup,/UGS OFR-757/);
    assert.ok(utContract.search.some(row=>row.tag==='UT SURVEY'&&
      row.name==='Acceptance UMOS Gold Prospect'&&row.state==='UT'),
    `loaded tile-scoped Utah search failed: ${JSON.stringify(utContract.search)}`);
    assert.ok(utahPmtilesRequests.some(
      request=>request.pathname.endsWith('/ugs-ofr757-umos.pmtiles')),
    'z6 must request the independently activated UMOS archive');
    assert.ok(utahFixtureRequests.length>0&&utahFixtureRequests.every(request=>
      request.pathname.endsWith('.pmtiles')&&/^bytes=\d+-\d*$/.test(request.range||'')),
    `Utah fixture must be PMTiles Range-only: ${JSON.stringify(utahFixtureRequests)}`);
    const utahSurvey=await sample(page,'utah-state-survey-baselines');

    await page.evaluate(ids=>ids.forEach(id=>document.getElementById(id).click()),utToggles);
    await page.waitForFunction(()=>STATE_SURVEY_LAYERS.filter(row=>row.state==='UT')
      .every(row=>!map.getSource(row.source_id)&&!PMT_PROTOCOL.tiles.has(row.file)));
    await page.evaluate(ids=>ids.forEach(id=>document.getElementById(id).click()),utToggles);
    await page.waitForFunction(()=>STATE_SURVEY_LAYERS.filter(row=>row.state==='UT')
      .every(row=>map.getSource(row.source_id)));
    await page.evaluate(()=>document.querySelectorAll('.schip')[STATES.indexOf('UT')].click());
    await page.waitForFunction(()=>STATE_SURVEY_LAYERS.filter(row=>row.state==='UT')
      .every(row=>!map.getSource(row.source_id)&&!PMT_PROTOCOL.tiles.has(row.file)));

    // Alaska is a two-stage lazy polygon delivery. Merely selecting Alaska
    // and entering its footprint below z8 must not allocate either archive.
    await page.evaluate(() => {
      setUiStates(['AK']);applyFilters();
      map.jumpTo({center:[-150.0,64.0],zoom:7.4});
    });
    await settle(page,45_000);
    const alaskaBelowBase=await page.evaluate(()=>({
      base:!!map.getSource('ak-state-claims'),
      precision:!!map.getSource('ak-state-claims-precision'),
      baseProtocol:PMT_PROTOCOL.tiles.has(alaskaClaimContract().base.file),
      precisionProtocol:PMT_PROTOCOL.tiles.has(alaskaClaimContract().precision.file),
    }));
    assert.deepEqual(alaskaBelowBase,{base:false,precision:false,
      baseProtocol:false,precisionProtocol:false},
      'Alaska sources/protocols must remain absent below manifest activation z8');
    assert.deepEqual(alaskaPmtilesRequests,[],
      'Alaska PMTiles must receive no request below base activation z8');

    // At z8 only the ordinary z13 archive may enter the style. Precision
    // remains entirely unallocated until its independent z19 activation.
    await page.evaluate(()=>map.jumpTo({center:[-150.0,64.0],zoom:8.25}));
    await page.waitForFunction(()=>!!map.getSource('ak-state-claims'));
    await settle(page,60_000);
    await waitForSourceFeatures(page,[['ak-state-claims','active']],60_000);
    await waitForSourceFeatures(page,[['national-ardf','ardf']],60_000);
    const baseRequestCount=alaskaPmtilesRequests.filter(
      request=>request.pathname.endsWith('/ak-state.pmtiles')).length;
    assert.ok(baseRequestCount>0,'z8 Alaska view must request the ordinary archive');
    assert.equal(alaskaPmtilesRequests.some(
      request=>request.pathname.endsWith('/ak-state-precision.pmtiles')),false,
      'z8 Alaska view must not request the z19 precision archive');
    assert.ok(alaskaPmtilesRequests.every(request=>/^bytes=\d+-\d*$/.test(request.range||'')),
      'every Alaska PMTiles browser request must use a byte range');
    const alaskaBaseContract=await page.evaluate(()=>{
      const ardf=map.querySourceFeatures('national-ardf',{sourceLayer:'ardf'});
      const stateClaims=map.querySourceFeatures('ak-state-claims',{sourceLayer:'active'});
      const layers=['akStateA-fill','akStateA-line','akStateP-fill','akStateP-line']
        .map(id=>{const layer=map.getLayer(id);return [id,layer&&layer.minzoom,layer&&layer.source];});
      return {ardf:ardf.length,stateClaims:stateClaims.length,layers,
        precisionSource:!!map.getSource('ak-state-claims-precision'),
        precisionProtocol:PMT_PROTOCOL.tiles.has(alaskaClaimContract().precision.file),
        ardfSchema:ardf.slice(0,100).every(f=>Number.isInteger(+f.properties.group)&&
          +f.properties.group>=0&&+f.properties.group<=5&&[0,1].includes(+f.properties.ex)),
        claimSchema:stateClaims.slice(0,100).every(f=>
          ['st','system','source_oid','serial','status','source_status','acres','part','url','lon','lat']
            .every(key=>Object.hasOwn(f.properties,key))&&
          ['Polygon','MultiPolygon'].includes(f.geometry.type)&&
          Number.isFinite(+f.properties.lon)&&Number.isFinite(+f.properties.lat))};
    });
    assert.ok(alaskaBaseContract.ardf>0,'Alaska view must load ARDF PMTiles features');
    assert.ok(alaskaBaseContract.stateClaims>0,'z8 Alaska view must load ordinary DNR polygons');
    assert.equal(alaskaBaseContract.ardfSchema,true,
      'ARDF features must carry normalized group/ex filters');
    assert.equal(alaskaBaseContract.claimSchema,true,
      'ordinary DNR rows must remain polygons with the exact shared property schema');
    assert.deepEqual(alaskaBaseContract.layers,[
      ['akStateA-fill',8,'ak-state-claims'],['akStateA-line',8,'ak-state-claims'],
      ['akStateP-fill',8,'ak-state-claims'],['akStateP-line',8,'ak-state-claims']],
      'ordinary Alaska fill/line layers must consume manifest activation z8');
    assert.equal(alaskaBaseContract.precisionSource,false);
    assert.equal(alaskaBaseContract.precisionProtocol,false);

    // The existing active toggle owns both archives and must release the
    // ordinary source and shared protocol entry when switched off.
    await page.evaluate(()=>document.querySelector('[data-layer="akClaimsA"]').click());
    await page.waitForFunction(()=>!map.getSource('ak-state-claims')&&
      !PMT_PROTOCOL.tiles.has(alaskaClaimContract().base.file));
    const baseOff=await page.evaluate(()=>({
      layers:AK_CLAIM_LAYER_IDS.filter(id=>map.getLayer(id)),
      source:!!map.getSource('ak-state-claims'),
      protocol:PMT_PROTOCOL.tiles.has(alaskaClaimContract().base.file)}));
    assert.deepEqual(baseOff,{layers:[],source:false,protocol:false},
      'active toggle off must tear down ordinary Alaska layers/source/protocol');
    await page.evaluate(()=>document.querySelector('[data-layer="akClaimsA"]').click());
    await page.waitForFunction(()=>!!map.getSource('ak-state-claims'));
    await settle(page,60_000);
    await waitForSourceFeatures(page,[['ak-state-claims','active']],60_000);

    // Exercise commodity filters and search against the independent ARDF
    // occurrence baseline while Alaska is selected.
    const ardfRenderedBefore=await page.evaluate(() =>
      map.queryRenderedFeatures({layers:['national-ardf-c']}).length);
    assert.ok(ardfRenderedBefore>0,'Alaska view must render ARDF before filtering');
    await page.evaluate(()=>document.querySelectorAll('#chips .chip.on').forEach(chip=>chip.click()));
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() =>
      map.queryRenderedFeatures({layers:['national-ardf-c']}).length),0,
      'ARDF must honor commodity-class controls');
    await page.evaluate(()=>document.querySelectorAll('#chips .chip.off').forEach(chip=>chip.click()));
    const ardfSearch=await page.evaluate(()=>{
      const f=map.querySourceFeatures('national-ardf',{sourceLayer:'ardf'})
        .find(row=>String((row.properties||{}).id||'').length>=2);
      if(!f)return null;
      const q=String(f.properties.id),input=document.getElementById('search');
      input.value=q;input.dispatchEvent(new Event('input',{bubbles:true}));
      return {q,found:[...document.querySelectorAll('#results .r[data-j]')]
        .some(node=>node.querySelector('.tag')?.textContent==='ARDF')};
    });
    assert.ok(ardfSearch&&ardfSearch.found,
      `ARDF search result missing: ${JSON.stringify(ardfSearch)}`);

    // The known unchanged overflow polygon is invisible in the base archive
    // by design. At z19 the precision archive appears, with real polygon
    // geometry, shared filters, canonical query status, search, and popup.
    await page.evaluate(()=>map.jumpTo({center:[-141.8557656,59.99210855],zoom:19}));
    await page.waitForFunction(()=>!!map.getSource('ak-state-claims-precision'));
    await settle(page,60_000);
    await waitForSourceFeatures(page,[['ak-state-claims-precision','active_precision']],60_000);
    const precisionContract=await page.evaluate(()=>{
      const precision=map.querySourceFeatures('ak-state-claims-precision',
        {sourceLayer:'active_precision'}).find(f=>+f.properties.source_oid===4387);
      const inBase=map.querySourceFeatures('ak-state-claims',{sourceLayer:'active'})
        .some(f=>+f.properties.source_oid===4387);
      if(!precision)return {missing:true};
      const fake={layer:{id:'akStateAPrecision-fill'},source:'ak-state-claims-precision',
        properties:precision.properties,geometry:precision.geometry};
      const tooltip=tipText(fake);showFeature(fake);
      const input=document.getElementById('search');input.value='ADL 728072';
      input.dispatchEvent(new Event('input',{bubbles:true}));
      const searchRow=[...document.querySelectorAll('#results .r[data-j]')]
        .find(node=>node.querySelector('.tag')?.textContent==='AK DNR PRECISION');
      const query=execQueryClaims({states:['AK'],layer:'active',system:'alaska_state',
        name_contains:'ADL 728072',limit:20});
      const layers=['akStateAPrecision-fill','akStateAPrecision-line']
        .map(id=>{const layer=map.getStyle().layers.find(row=>row.id===id);return [id,layer&&layer.minzoom,
          layer&&layer.source,layer&&layer['source-layer'],layer&&layer.filter];});
      return {missing:false,inBase,properties:precision.properties,
        geometry:precision.geometry.type,tooltip,
        detail:document.getElementById('detail').textContent,
        searchFound:!!searchRow,query,layers,
        protocols:{base:PMT_PROTOCOL.tiles.has(alaskaClaimContract().base.file),
          precision:PMT_PROTOCOL.tiles.has(alaskaClaimContract().precision.file)}};
    });
    assert.equal(precisionContract.missing,false,
      'z19 must decode known active precision source polygon OBJECTID 4387');
    assert.equal(precisionContract.inBase,false,
      'precision overflow OBJECTID must be disjoint from the ordinary archive');
    assert.ok(['Polygon','MultiPolygon'].includes(precisionContract.geometry),
      `precision feature regressed from polygon geometry: ${precisionContract.geometry}`);
    for(const key of ['st','system','source_oid','serial','status','source_status','acres','part','url','lon','lat'])
      assert.ok(Object.hasOwn(precisionContract.properties,key),
        `precision feature missing shared property ${key}`);
    assert.match(precisionContract.tooltip,/AK DNR ACTIVE source polygon/);
    assert.match(precisionContract.detail,/SEPARATE FROM FEDERAL MLRS/);
    assert.match(precisionContract.detail,/lossless z19 precision-overflow archive/);
    assert.equal(precisionContract.searchFound,true,
      'precision overflow serial must be searchable from the loaded z19 tile');
    assert.equal(precisionContract.query.status,'measured');
    assert.equal(precisionContract.query.exact_count,39269+51,
      'active query exact count must use combined main+precision inventory');
    assert.ok(precisionContract.query.loaded_tile_count>0,
      'precision serial query must not report a false zero');
    assert.equal(new Set(precisionContract.query.sample.map(row=>String(row.source_oid))).size,
      precisionContract.query.sample.length,
      'repeated Alaska serials must retain distinct source identities without tile duplicates');
    assert.ok(precisionContract.query.sample.some(row=>+row.source_oid===4387&&
      row.precision_overflow===true),
      'combined query must include the z19 overflow source polygon with explicit provenance');
    assert.deepEqual(Object.keys(
      precisionContract.query.by_system.alaska_state.status_counts).sort(),
      ['active','closed','pending'],
      'precision source-layer names must canonicalize to shared status keys');
    assert.equal(precisionContract.query.by_system.alaska_state.status_counts.active,
      precisionContract.query.loaded_tile_count);
    assert.deepEqual(precisionContract.layers,[
      ['akStateAPrecision-fill',19,'ak-state-claims-precision','active_precision',
        ['==',['get','st'],'AK']],
      ['akStateAPrecision-line',19,'ak-state-claims-precision','active_precision',
        ['==',['get','st'],'AK']]],
      'precision fill/line must activate exactly at z19 with AK-only filters');
    assert.deepEqual(precisionContract.protocols,{base:true,precision:true});
    const precisionRequestCount=alaskaPmtilesRequests.filter(
      request=>request.pathname.endsWith('/ak-state-precision.pmtiles')).length;
    assert.ok(precisionRequestCount>0,'z19 must issue precision PMTiles range requests');
    assert.ok(alaskaPmtilesRequests.slice(baseRequestCount).every(
      request=>/^bytes=\d+-\d*$/.test(request.range||'')),
      'z19 main/precision requests must remain Range-only');

    // Closed uses the same two archives under the existing closed toggle.
    // Switching modes must swap real style layers and canonicalize
    // closed_precision without retaining active presentation layers.
    await page.evaluate(()=>{
      document.querySelector('[data-layer="akClaimsA"]').click();
      document.querySelector('[data-layer="akClaimsC"]').click();
      map.jumpTo({center:[-155.7198403,59.8429239],zoom:19});
    });
    await page.waitForFunction(()=>!!map.getSource('ak-state-claims-precision'));
    await settle(page,60_000);
    await waitForSourceFeatures(page,[['ak-state-claims-precision','closed_precision']],60_000);
    const closedContract=await page.evaluate(()=>{
      const feature=map.querySourceFeatures('ak-state-claims-precision',
        {sourceLayer:'closed_precision'}).find(f=>+f.properties.source_oid===2862);
      const query=execQueryClaims({states:['AK'],layer:'closed',system:'alaska_state',
        name_contains:feature&&feature.properties.serial,limit:20});
      return {found:!!feature,geometry:feature&&feature.geometry.type,query,
        activeLayers:['akStateA-fill','akStateA-line','akStateP-fill','akStateP-line',
          'akStateAPrecision-fill','akStateAPrecision-line'].filter(id=>map.getLayer(id)),
        closedLayers:['akStateC-fill','akStateC-line','akStateCPrecision-fill',
          'akStateCPrecision-line'].filter(id=>map.getLayer(id))};
    });
    assert.equal(closedContract.found,true,
      'closed toggle must expose known closed precision source polygon OBJECTID 2862');
    assert.ok(['Polygon','MultiPolygon'].includes(closedContract.geometry));
    assert.deepEqual(closedContract.activeLayers,[]);
    assert.equal(closedContract.closedLayers.length,4,
      'closed toggle must own main and precision fill/line layers');
    assert.equal(closedContract.query.status,'measured');
    assert.equal(closedContract.query.exact_count,79480,
      'closed exact count must combine 79,462 base + 18 precision polygons');
    assert.ok(closedContract.query.loaded_tile_count>0,
      'closed precision serial query must not report a false zero');
    assert.equal(new Set(closedContract.query.sample.map(row=>String(row.source_oid))).size,
      closedContract.query.sample.length,
      'closed repeated serials must retain distinct source identities without tile duplicates');
    assert.ok(closedContract.query.sample.some(row=>+row.source_oid===2862&&
      row.precision_overflow===true),
      'closed combined query must include precision OBJECTID 2862');
    assert.equal(closedContract.query.by_system.alaska_state.status_counts.closed,
      closedContract.query.loaded_tile_count);

    // Final teardown proves both sources and non-persistent protocol objects
    // leave the shared cache lifecycle. Re-enable active, then descend through
    // z19 and z8 to prove each activation boundary independently.
    await page.evaluate(()=>document.querySelector('[data-layer="akClaimsC"]').click());
    await page.waitForFunction(()=>!map.getSource('ak-state-claims')&&
      !map.getSource('ak-state-claims-precision')&&
      !PMT_PROTOCOL.tiles.has(alaskaClaimContract().base.file)&&
      !PMT_PROTOCOL.tiles.has(alaskaClaimContract().precision.file));
    await page.evaluate(()=>document.querySelector('[data-layer="akClaimsA"]').click());
    await page.waitForFunction(()=>map.getSource('ak-state-claims')&&
      map.getSource('ak-state-claims-precision'));
    await settle(page,60_000);
    await page.evaluate(()=>map.jumpTo({zoom:18}));
    await page.waitForFunction(()=>map.getSource('ak-state-claims')&&
      !map.getSource('ak-state-claims-precision')&&
      PMT_PROTOCOL.tiles.has(alaskaClaimContract().base.file)&&
      !PMT_PROTOCOL.tiles.has(alaskaClaimContract().precision.file));
    await page.evaluate(()=>map.jumpTo({zoom:7.5}));
    await page.waitForFunction(()=>!map.getSource('ak-state-claims')&&
      !PMT_PROTOCOL.tiles.has(alaskaClaimContract().base.file));
    const alaska = await sample(page,'alaska-lossless-split-lifecycle');

    assert.deepEqual(legacyRequests, [], 'browser requested legacy statewide claims/sites JSON');
    assert.deepEqual(pageErrors, [], `browser exceptions: ${pageErrors.join(' | ')}`);
    mapErrors.push(...await page.evaluate(()=>typeof DBG_MAP_ERRORS==='undefined'?[]:DBG_MAP_ERRORS));
    assert.deepEqual(mapErrors, [], `MapLibre errors: ${mapErrors.join(' | ')}`);
    assert.deepEqual(localHttpErrors, [], `local HTTP errors: ${localHttpErrors.join(' | ')}`);
    assert.deepEqual(requestFailures, [], `unexpected external/network requests: ${requestFailures.join(' | ')}`);
    for (const result of [boot, dense, geology, arizonaSurvey, coloradoSurvey,
      utahSurvey, alaska]) {
      assert.equal(result.rejections, 0, `${result.label}: unhandled promise rejection`);
      assert.ok(result.heap_mb !== null, `${result.label}: precise heap measurement unavailable`);
      assert.ok(result.heap_mb <= BUDGETS.browser.heap_mb_max,
        `${result.label}: heap ${result.heap_mb} MB exceeds ${BUDGETS.browser.heap_mb_max} MB`);
      assert.ok(result.origin_storage_mb <= BUDGETS.browser.bulk_origin_storage_mb_max,
        `${result.label}: origin storage ${result.origin_storage_mb} MB exceeds ` +
        `${BUDGETS.browser.bulk_origin_storage_mb_max} MB`);
    }
    console.log('WS11 browser acceptance passed');
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (server.exitCode === null) server.kill('SIGTERM');
    if(utahFixtureDirectory)
      fs.rmSync(utahFixtureDirectory,{recursive:true,force:true});
  }
}

(process.env.NWMM_ACCEPTANCE_FOCUS==='robustness'?runRobustness():run()).catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
