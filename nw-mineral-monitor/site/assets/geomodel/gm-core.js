/* gm-core.js — geomodel core for the browser: the JS twin of
   pipelines/geomodel/model.py.  Same object kinds, same JSON project layout
   (typed-array blobs as {"@f64": base64}), same conventions:
   - coordinates projected (WGS84/UTM metres), Z = elevation
   - Grid2D node-registered, values row-major SOUTH row first, x fastest
   - Mesh = flat xyz + flat 0-based triangles; LineSet = vertices + segments +
     parts; BlockModel attributes i-fastest then j then k.
   Also: UTM math (identical Snyder series to the map), a tiny event bus and
   the IndexedDB project store ('nwmm-geomodel' — whitelisted in the map's
   storage guard). */

export const SCHEMA = 'nwmm-geomodel/1';
export const VERSION = '1.0.0';

/* ----------------------------------------------------------- typed blobs */
const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
export function b64encode(bytes){
  let out='', i=0; const n=bytes.length;
  for(; i+2<n; i+=3){
    const v=(bytes[i]<<16)|(bytes[i+1]<<8)|bytes[i+2];
    out+=B64[v>>18]+B64[(v>>12)&63]+B64[(v>>6)&63]+B64[v&63];
  }
  if(i<n){
    const v=(bytes[i]<<16)|((i+1<n?bytes[i+1]:0)<<8);
    out+=B64[v>>18]+B64[(v>>12)&63]+(i+1<n?B64[(v>>6)&63]:'=')+'=';
  }
  return out;
}
export function b64decode(str){
  const bin=atob(str); const out=new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++)out[i]=bin.charCodeAt(i);
  return out;
}
const LE = new Uint8Array(new Uint16Array([1]).buffer)[0]===1;
function bytesOf(arr){
  // little-endian bytes of a typed array (host is LE everywhere that matters)
  const u8=new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength);
  if(LE) return u8;
  const dv=new DataView(arr.buffer, arr.byteOffset, arr.byteLength), bpe=arr.BYTES_PER_ELEMENT, out=new Uint8Array(arr.byteLength);
  for(let i=0;i<arr.length;i++)for(let b=0;b<bpe;b++)out[i*bpe+b]=dv.getUint8(i*bpe+(bpe-1-b));
  return out;
}
let RAW=false;   // packObject(): keep typed arrays raw for structured-clone transfer to workers
export function encodeArray(arr, kind='f64'){
  const C={f64:Float64Array,f32:Float32Array,u32:Uint32Array,i32:Int32Array}[kind];
  const ta = arr instanceof C ? arr : C.from(arr, v=>v==null?NaN:v);
  if(RAW) return ta;
  return {['@'+kind]: b64encode(bytesOf(ta))};
}
/** toJSON() with typed arrays left raw (cheap postMessage); unpack with KINDS[kind].fromJSON. */
export function packObject(o){ RAW=true; try{ return o.toJSON(); } finally{ RAW=false; } }
export function unpackObject(d){ const K=KINDS[d.kind]; if(!K) throw new Error('unknown kind '+d.kind); return K.fromJSON(d); }
export function decodeArray(obj){
  if(Array.isArray(obj) || ArrayBuffer.isView(obj)) return obj;
  for(const [k,C] of [['@f64',Float64Array],['@f32',Float32Array],['@u32',Uint32Array],['@i32',Int32Array]]){
    if(obj && obj[k]!=null){
      const bytes=b64decode(obj[k]);
      const buf=bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset+bytes.byteLength);
      if(!LE){ const bpe=C.BYTES_PER_ELEMENT, u=new Uint8Array(buf); for(let i=0;i<u.length;i+=bpe)u.subarray(i,i+bpe).reverse(); }
      return new C(buf);
    }
  }
  throw new Error('unknown array blob');
}
export const isNum = v => typeof v==='number' && v===v;
export function f64(seq){ if(seq instanceof Float64Array) return seq; const a=new Float64Array(seq.length); for(let i=0;i<seq.length;i++){const v=seq[i]; a[i]=(v==null||v==='')?NaN:+v;} return a; }
export function u32(seq){ return seq instanceof Uint32Array? seq : Uint32Array.from(seq); }
function flat3(seq){ if(!seq) return new Float64Array(0); if(seq instanceof Float64Array) return seq; if(seq.length && Array.isArray(seq[0])){ const a=new Float64Array(seq.length*3); for(let i=0;i<seq.length;i++){a[3*i]=seq[i][0];a[3*i+1]=seq[i][1];a[3*i+2]=seq[i].length>2?seq[i][2]:0;} return a;} return f64(seq); }
function xyzBounds(flat){
  let mn=[Infinity,Infinity,Infinity], mx=[-Infinity,-Infinity,-Infinity], any=false;
  for(let k=0;k+2<flat.length;k+=3)for(let a=0;a<3;a++){const v=flat[k+a]; if(v!==v)continue; any=true; if(v<mn[a])mn[a]=v; if(v>mx[a])mx[a]=v;}
  return any? [mn[0],mn[1],mn[2],mx[0],mx[1],mx[2]] : null;
}
export function nowISO(){ return new Date().toISOString().replace(/\.\d+Z$/,'Z'); }
let SEQ=0;
export function uid(kind){ SEQ++; return `${kind}-${SEQ}-${(Date.now()&0xffffff).toString(16)}${Math.floor(Math.random()*4096).toString(16)}`; }

/* --------------------------------------------------------------- objects */
export class ModelObject{
  constructor(o={}){
    this.kind=this.constructor.kind;
    this.id=o.id||uid(this.kind); this.name=o.name||''; this.color=o.color?o.color.slice(0,3):[160,160,160];
    this.visible=o.visible!==false; this.opacity=o.opacity==null?1:o.opacity; this.group=o.group||'';
    this.provenance=Object.assign({},o.provenance||{}); this.metadata=Object.assign({},o.metadata||{});
  }
  head(){ return {id:this.id,kind:this.kind,name:this.name,color:this.color,visible:this.visible,opacity:this.opacity,group:this.group,provenance:this.provenance,metadata:this.metadata}; }
  bounds(){ return null; }
  warn(msg){ (this.metadata.warnings=this.metadata.warnings||[]).push(msg); }
}

export class Grid2D extends ModelObject{
  static kind='grid2d';
  constructor(o){
    super(o);
    this.nx=o.nx|0; this.ny=o.ny|0; this.x0=+o.x0; this.y0=+o.y0; this.dx=+o.dx; this.dy=+o.dy;
    this.rotation=+(o.rotation||0); this.units=o.units||'m'; this.role=o.role||'surface';
    this.values = o.values? f64(o.values) : new Float64Array(this.nx*this.ny).fill(NaN);
    if(this.values.length!==this.nx*this.ny) throw new Error(`Grid2D values ${this.values.length} != ${this.nx*this.ny}`);
  }
  nodeXY(i,j){
    if(this.rotation){ const r=this.rotation*Math.PI/180, c=Math.cos(r), s=Math.sin(r), u=i*this.dx, v=j*this.dy; return [this.x0+u*c-v*s, this.y0+u*s+v*c]; }
    return [this.x0+i*this.dx, this.y0+j*this.dy];
  }
  get(i,j){ return this.values[j*this.nx+i]; }
  set(i,j,v){ this.values[j*this.nx+i]=v==null?NaN:v; }
  get xmax(){ return this.x0+(this.nx-1)*this.dx; }
  get ymax(){ return this.y0+(this.ny-1)*this.dy; }
  zrange(){ let mn=Infinity,mx=-Infinity; for(const v of this.values){ if(v!==v)continue; if(v<mn)mn=v; if(v>mx)mx=v;} return mn===Infinity?[NaN,NaN]:[mn,mx]; }
  bounds(){ const cs=[this.nodeXY(0,0),this.nodeXY(this.nx-1,0),this.nodeXY(0,this.ny-1),this.nodeXY(this.nx-1,this.ny-1)]; const z=this.zrange(); return [Math.min(...cs.map(c=>c[0])),Math.min(...cs.map(c=>c[1])),z[0],Math.max(...cs.map(c=>c[0])),Math.max(...cs.map(c=>c[1])),z[1]]; }
  sample(x,y){
    let u,v;
    if(this.rotation){ const r=-this.rotation*Math.PI/180, c=Math.cos(r), s=Math.sin(r), px=x-this.x0, py=y-this.y0; u=px*c-py*s; v=px*s+py*c; }
    else { u=x-this.x0; v=y-this.y0; }
    const fi=u/this.dx, fj=v/this.dy;
    if(fi<-1e-9||fj<-1e-9||fi>this.nx-1+1e-9||fj>this.ny-1+1e-9) return NaN;
    const i0=this.nx>1?Math.min(Math.max(Math.floor(fi),0),this.nx-2):0, j0=this.ny>1?Math.min(Math.max(Math.floor(fj),0),this.ny-2):0;
    const tx=this.nx>1?Math.min(Math.max(fi-i0,0),1):0, ty=this.ny>1?Math.min(Math.max(fj-j0,0),1):0;
    const i1=Math.min(i0+1,this.nx-1), j1=Math.min(j0+1,this.ny-1);
    const v00=this.get(i0,j0), v10=this.get(i1,j0), v01=this.get(i0,j1), v11=this.get(i1,j1);
    if(v00!==v00||v10!==v10||v01!==v01||v11!==v11){
      const c=[[0,0,v00],[1,0,v10],[0,1,v01],[1,1,v11]].filter(q=>q[2]===q[2]);
      if(!c.length) return NaN; c.sort((a,b)=>(Math.abs(tx-a[0])+Math.abs(ty-a[1]))-(Math.abs(tx-b[0])+Math.abs(ty-b[1]))); return c[0][2];
    }
    return v00*(1-tx)*(1-ty)+v10*tx*(1-ty)+v01*(1-tx)*ty+v11*tx*ty;
  }
  copyEmpty(fill=NaN){ return new Grid2D({nx:this.nx,ny:this.ny,x0:this.x0,y0:this.y0,dx:this.dx,dy:this.dy,rotation:this.rotation,units:this.units,role:this.role,name:this.name,color:this.color,values:new Float64Array(this.nx*this.ny).fill(fill)}); }
  toMesh(stride=1,name){
    stride=Math.max(1,stride|0);
    const idx=new Map(), verts=[], tris=[];
    for(let j=0;j<this.ny;j+=stride)for(let i=0;i<this.nx;i+=stride){ const z=this.get(i,j); if(z!==z)continue; const [x,y]=this.nodeXY(i,j); idx.set(j*this.nx+i, verts.length/3); verts.push(x,y,z); }
    const js=[],is=[]; for(let j=0;j<this.ny;j+=stride)js.push(j); for(let i=0;i<this.nx;i+=stride)is.push(i);
    for(let a=0;a<js.length-1;a++)for(let b=0;b<is.length-1;b++){
      const p=idx.get(js[a]*this.nx+is[b]), q=idx.get(js[a]*this.nx+is[b+1]), r=idx.get(js[a+1]*this.nx+is[b]), s=idx.get(js[a+1]*this.nx+is[b+1]);
      if(p==null||q==null||r==null||s==null)continue; tris.push(p,q,s,p,s,r);
    }
    const m=new Mesh({vertices:Float64Array.from(verts),triangles:Uint32Array.from(tris),name:name||this.name,color:this.color,provenance:Object.assign({},this.provenance)});
    m.metadata.from_grid=this.id; return m;
  }
  toJSON(){ return Object.assign(this.head(),{nx:this.nx,ny:this.ny,x0:this.x0,y0:this.y0,dx:this.dx,dy:this.dy,rotation:this.rotation,units:this.units,role:this.role,values:encodeArray(this.values,this.role==='property'?'f32':'f64')}); }
  static fromJSON(d){ return new Grid2D(Object.assign({},d,{values:f64(decodeArray(d.values))})); }
}

export class Mesh extends ModelObject{
  static kind='mesh';
  constructor(o={}){
    super(o);
    this.vertices=flat3(o.vertices); this.triangles=u32(o.triangles||[]); this.role=o.role||'surface';
    this.attributes={}; for(const [k,v] of Object.entries(o.attributes||{})) this.attributes[k]={location:v.location||'vertices', values:v.values instanceof Float32Array?v.values:Float32Array.from(v.values,x=>x==null?NaN:x)};
  }
  get nVertices(){ return this.vertices.length/3; }
  get nTriangles(){ return this.triangles.length/3; }
  vertex(i){ return [this.vertices[3*i],this.vertices[3*i+1],this.vertices[3*i+2]]; }
  bounds(){ return xyzBounds(this.vertices); }
  toJSON(){ const attrs={}; for(const [k,v] of Object.entries(this.attributes)) attrs[k]={location:v.location,values:encodeArray(v.values,'f32')}; return Object.assign(this.head(),{role:this.role,vertices:encodeArray(this.vertices,'f64'),triangles:encodeArray(this.triangles,'u32'),attributes:attrs}); }
  static fromJSON(d){ const attrs={}; for(const [k,v] of Object.entries(d.attributes||{})) attrs[k]={location:v.location,values:Float32Array.from(decodeArray(v.values))}; return new Mesh(Object.assign({},d,{vertices:f64(decodeArray(d.vertices)),triangles:u32(decodeArray(d.triangles)),attributes:attrs})); }
}

export class LineSet extends ModelObject{
  static kind='lineset';
  constructor(o={}){
    super(o);
    this.vertices=flat3(o.vertices); this.segments=u32(o.segments||[]); this.parts=(o.parts||[]).map(p=>Array.from(p)); this.features=(o.features||[]).map(f=>Object.assign({},f)); this.role=o.role||'lines';
    if(!this.parts.length && this.segments.length) this.parts=this._partsFromSegments();
    if(!this.segments.length && this.parts.length) this.segments=this._segmentsFromParts();
    while(this.features.length<this.parts.length) this.features.push({});
  }
  _segmentsFromParts(){ const s=[]; for(const p of this.parts)for(let k=0;k<p.length-1;k++)s.push(p[k],p[k+1]); return Uint32Array.from(s); }
  _partsFromSegments(){
    const nxt=new Map(), hasPrev=new Set();
    for(let k=0;k+1<this.segments.length;k+=2){ const a=this.segments[k],b=this.segments[k+1]; if(!nxt.has(a))nxt.set(a,[]); nxt.get(a).push(b); hasPrev.add(b); }
    const parts=[], seen=new Set(); const starts=[...nxt.keys()].filter(a=>!hasPrev.has(a)).concat([...nxt.keys()]);
    for(const a of starts){ if(seen.has(a))continue; const chain=[a]; seen.add(a); let cur=a; while(nxt.get(cur)&&nxt.get(cur).length&&!seen.has(nxt.get(cur)[0])){ cur=nxt.get(cur).shift(); chain.push(cur); seen.add(cur);} parts.push(chain); }
    return parts;
  }
  addPolyline(xyz, feature={}){
    const base=this.nVertices, nv=new Float64Array(this.vertices.length+xyz.length*3); nv.set(this.vertices);
    for(let i=0;i<xyz.length;i++){ nv[this.vertices.length+3*i]=+xyz[i][0]; nv[this.vertices.length+3*i+1]=+xyz[i][1]; nv[this.vertices.length+3*i+2]=xyz[i].length>2?+xyz[i][2]:0; }
    this.vertices=nv;
    const idx=[]; for(let i=0;i<xyz.length;i++)idx.push(base+i);
    const ns=new Uint32Array(this.segments.length+2*(idx.length-1)); ns.set(this.segments); for(let k=0;k<idx.length-1;k++){ ns[this.segments.length+2*k]=idx[k]; ns[this.segments.length+2*k+1]=idx[k+1]; }
    this.segments=ns; this.parts.push(idx); this.features.push(Object.assign({},feature)); return this.parts.length-1;
  }
  removePart(k){
    const keep=this.parts.filter((_,i)=>i!==k), feats=this.features.filter((_,i)=>i!==k);
    const xyz=keep.map(p=>p.map(i=>this.vertex(i)));
    this.vertices=new Float64Array(0); this.segments=new Uint32Array(0); this.parts=[]; this.features=[];
    xyz.forEach((p,i)=>this.addPolyline(p,feats[i]));
  }
  get nVertices(){ return this.vertices.length/3; }
  vertex(i){ return [this.vertices[3*i],this.vertices[3*i+1],this.vertices[3*i+2]]; }
  partXYZ(k){ return this.parts[k].map(i=>this.vertex(i)); }
  length(k){ const parts=k==null?this.parts:[this.parts[k]]; let t=0; for(const p of parts)for(let a=0;a<p.length-1;a++){const u=this.vertex(p[a]),v=this.vertex(p[a+1]); t+=Math.hypot(v[0]-u[0],v[1]-u[1],v[2]-u[2]);} return t; }
  bounds(){ return xyzBounds(this.vertices); }
  toJSON(){ return Object.assign(this.head(),{role:this.role,vertices:encodeArray(this.vertices,'f64'),segments:encodeArray(this.segments,'u32'),parts:this.parts,features:this.features}); }
  static fromJSON(d){ return new LineSet(Object.assign({},d,{vertices:f64(decodeArray(d.vertices)),segments:u32(decodeArray(d.segments))})); }
}

export class PointSet extends ModelObject{
  static kind='points';
  constructor(o={}){
    super(o); this.xyz=flat3(o.xyz); this.role=o.role||'points'; this.attributes={};
    for(const [k,v] of Object.entries(o.attributes||{})) this.attributes[k]=Array.isArray(v)||ArrayBuffer.isView(v)?Array.from(v):[];
  }
  get n(){ return this.xyz.length/3; }
  point(i){ return [this.xyz[3*i],this.xyz[3*i+1],this.xyz[3*i+2]]; }
  add(x,y,z,attrs={}){
    const nv=new Float64Array(this.xyz.length+3); nv.set(this.xyz); nv[this.xyz.length]=+x; nv[this.xyz.length+1]=+y; nv[this.xyz.length+2]=+z; this.xyz=nv;
    const n=this.n; const keys=Object.keys(this.attributes).concat(Object.keys(attrs).filter(k=>!(k in this.attributes)));
    for(const k of keys){ const col=this.attributes[k]=this.attributes[k]||[]; while(col.length<n-1)col.push(null); col.push(attrs[k]===undefined?null:attrs[k]); }
    return n-1;
  }
  /** Drop one row (or several) from xyz and from every attribute column —
      z_original included — so `n` (xyz.length/3) and the columns stay in
      step.  Columns shorter than n are padded with null first, the way
      add() pads, so nothing shifts onto the wrong row.  Returns the number
      of rows actually removed. */
  removeRow(i){ return this.removeRows([i]); }
  removeRows(indices){
    const n=this.n, drop=new Uint8Array(n); let k=0;
    for(const v of indices||[]){ const j=+v; if(j>=0&&j<n&&j===Math.floor(j)&&!drop[j]){ drop[j]=1; k++; } }
    if(!k) return 0;
    const nv=new Float64Array((n-k)*3); let w=0;
    for(let i=0;i<n;i++){ if(drop[i])continue; nv[3*w]=this.xyz[3*i]; nv[3*w+1]=this.xyz[3*i+1]; nv[3*w+2]=this.xyz[3*i+2]; w++; }
    this.xyz=nv;
    for(const key of Object.keys(this.attributes)){ let col=this.attributes[key]; if(!Array.isArray(col))col=Array.from(col||[]); if(col.length>n)col=col.slice(0,n); while(col.length<n)col.push(null); this.attributes[key]=col.filter((_,i)=>!drop[i]); }
    return k;
  }
  numeric(name){ const col=this.attributes[name]||[], out=new Float64Array(this.n).fill(NaN); for(let i=0;i<Math.min(col.length,this.n);i++){ const v=col[i]; if(v==null||v==='')continue; const f=+v; if(f===f)out[i]=f; } return out; }
  isNumeric(name){ let seen=false; for(const v of this.attributes[name]||[]){ if(v==null||v==='')continue; if(typeof v==='boolean')return false; const f=+v; if(f!==f)return false; seen=true; } return seen; }
  bounds(){ return xyzBounds(this.xyz); }
  toJSON(){ const attrs={}; for(const [k,col] of Object.entries(this.attributes)) attrs[k]= this.isNumeric(k)? {type:'number',values:encodeArray(f64(col.map(v=>v==null||v===''?NaN:v)),'f64')} : {type:'text',values:col.map(v=>v==null?null:String(v))}; return Object.assign(this.head(),{role:this.role,xyz:encodeArray(this.xyz,'f64'),attributes:attrs}); }
  static fromJSON(d){ const attrs={}; for(const [k,v] of Object.entries(d.attributes||{})) attrs[k]=v.type==='number'? Array.from(decodeArray(v.values),x=>x!==x?null:x) : v.values; return new PointSet(Object.assign({},d,{xyz:f64(decodeArray(d.xyz)),attributes:attrs})); }
}

export class BlockModel extends ModelObject{
  static kind='blockmodel';
  constructor(o){
    super(o); this.origin=o.origin.map(Number); this.blockSize=(o.block_size||o.blockSize).map(Number); this.count=o.count.map(v=>v|0); this.azimuth=+(o.azimuth||0); this.attributes={};
    for(const [k,v] of Object.entries(o.attributes||{})) this.addAttribute(k, v.values!==undefined? v.values : v, v.type||'number');
  }
  get n(){ return this.count[0]*this.count[1]*this.count[2]; }
  addAttribute(name, values, type='number'){ const vals=type==='number'? (values instanceof Float32Array||values instanceof Float64Array? values : Float32Array.from(values,v=>v==null?NaN:+v)) : Array.from(values); if(vals.length!==this.n) throw new Error(`attribute ${name}: ${vals.length} values for ${this.n} blocks`); this.attributes[name]={type,values:vals}; }
  index(i,j,k){ return i+this.count[0]*(j+this.count[1]*k); }
  ijk(idx){ const nx=this.count[0], ny=this.count[1]; return [idx%nx, Math.floor(idx/nx)%ny, Math.floor(idx/(nx*ny))]; }
  centroid(i,j,k){ const [ox,oy,oz]=this.origin, u=(i+.5)*this.blockSize[0], v=(j+.5)*this.blockSize[1], z=oz+(k+.5)*this.blockSize[2]; if(this.azimuth){ const r=this.azimuth*Math.PI/180, c=Math.cos(r), s=Math.sin(r); return [ox+u*c+v*s, oy-u*s+v*c, z]; } return [ox+u, oy+v, z]; }
  bounds(){ const xs=[],ys=[]; for(const i of [0,this.count[0]])for(const j of [0,this.count[1]]){ const c=this.centroid(i-.5,j-.5,0); xs.push(c[0]); ys.push(c[1]); } return [Math.min(...xs),Math.min(...ys),this.origin[2],Math.max(...xs),Math.max(...ys),this.origin[2]+this.count[2]*this.blockSize[2]]; }
  toJSON(){ const attrs={}; for(const [k,a] of Object.entries(this.attributes)) attrs[k]= a.type==='number'? {type:'number',values:encodeArray(a.values,'f32')} : {type:a.type,values:Array.from(a.values)}; return Object.assign(this.head(),{origin:this.origin,block_size:this.blockSize,count:this.count,azimuth:this.azimuth,attributes:attrs}); }
  static fromJSON(d){ const attrs={}; for(const [k,a] of Object.entries(d.attributes||{})) attrs[k]={type:a.type||'number', values: (a.type||'number')==='number'? Float32Array.from(decodeArray(a.values)) : a.values}; return new BlockModel(Object.assign({},d,{attributes:attrs})); }
}

function lerpAngle(a,b,t){ const d=((b-a+Math.PI)%(2*Math.PI)+2*Math.PI)%(2*Math.PI)-Math.PI; return a+d*t; }
function minCurvature(L,az0,dip0,az1,dip1){
  const i0=Math.PI/2-dip0, i1=Math.PI/2-dip1;
  let cdl=Math.cos(i1-i0)-Math.sin(i0)*Math.sin(i1)*(1-Math.cos(az1-az0)); cdl=Math.max(-1,Math.min(1,cdl));
  const dl=Math.acos(cdl), rf=dl<1e-9?1:2/dl*Math.tan(dl/2);
  const dn=L/2*(Math.sin(i0)*Math.cos(az0)+Math.sin(i1)*Math.cos(az1))*rf, de=L/2*(Math.sin(i0)*Math.sin(az0)+Math.sin(i1)*Math.sin(az1))*rf, dv=L/2*(Math.cos(i0)+Math.cos(i1))*rf;
  return [de,dn,-dv];
}
export class Drillholes extends ModelObject{
  static kind='drillholes';
  constructor(o={}){ super(o); this.collars=(o.collars||[]).map(c=>Object.assign({},c)); this.surveys=(o.surveys||[]).map(s=>Object.assign({},s)); this.intervals={}; for(const [k,v] of Object.entries(o.intervals||{})) this.intervals[k]=v.map(r=>Object.assign({},r)); this._traces=null; }
  holes(){ return this.collars.map(c=>c.hole); }
  desurvey(step){
    const traces={};
    for(const c of this.collars){
      const hole=c.hole; let svy=this.surveys.filter(s=>s.hole===hole).sort((a,b)=>+a.depth-+b.depth);
      let dmax=c.depth; if(dmax==null||dmax===''){ const ds=[]; for(const t of Object.values(this.intervals))for(const r of t)if(r.hole===hole)ds.push(+r.to); dmax=ds.length?Math.max(...ds):(svy.length?+svy[svy.length-1].depth:0); }
      dmax=+dmax; if(!svy.length)svy=[{depth:0,azimuth:0,dip:90}];
      if(+svy[0].depth>0)svy=[Object.assign({},svy[0],{depth:0})].concat(svy);
      if(+svy[svy.length-1].depth<dmax)svy=svy.concat([Object.assign({},svy[svy.length-1],{depth:dmax})]);
      let x=+c.x,y=+c.y,z=+c.z; const pts=[[0,x,y,z]];
      for(let a=0;a<svy.length-1;a++){
        const d0=+svy[a].depth,d1=+svy[a+1].depth; if(d1<=d0)continue;
        const az0=+svy[a].azimuth*Math.PI/180,dp0=+svy[a].dip*Math.PI/180,az1=+svy[a+1].azimuth*Math.PI/180,dp1=+svy[a+1].dip*Math.PI/180;
        const nsub=step?Math.max(1,Math.ceil((d1-d0)/step)):1, seg=(d1-d0)/nsub;
        for(let q=0;q<nsub;q++){ const ta=q/nsub,tb=(q+1)/nsub; const [dx,dy,dz]=minCurvature(seg,lerpAngle(az0,az1,ta),dp0+(dp1-dp0)*ta,lerpAngle(az0,az1,tb),dp0+(dp1-dp0)*tb); x+=dx;y+=dy;z+=dz; pts.push([d0+seg*(q+1),x,y,z]); }
      }
      traces[hole]=pts;
    }
    this._traces=traces; return traces;
  }
  locate(hole,depth,traces){ traces=traces||this._traces||this.desurvey(); const tr=traces[hole]; if(!tr)return null; if(depth<=tr[0][0])return tr[0].slice(1); for(let a=0;a<tr.length-1;a++){ const d0=tr[a][0],d1=tr[a+1][0]; if(d0<=depth&&depth<=d1){ const t=d1===d0?0:(depth-d0)/(d1-d0); return [1,2,3].map(b=>tr[a][b]+(tr[a+1][b]-tr[a][b])*t); } } return tr[tr.length-1].slice(1); }
  intervalPoints(table,column){ const traces=this.desurvey(); const ps=new PointSet({name:`${table} ${column}`,role:'samples'}); for(const r of this.intervals[table]||[]){ const f=+r.from,t=+r.to,v=+r[column]; if(!(f===f&&t===t&&v===v))continue; const p=this.locate(r.hole,(f+t)/2,traces); if(!p)continue; const attrs={hole:r.hole,from:f,to:t,length:t-f}; attrs[column]=v; ps.add(p[0],p[1],p[2],attrs);} return ps; }
  tracesLineSet(step){ const traces=this.desurvey(step); const ls=new LineSet({name:this.name+' traces',role:'drillhole-traces',color:this.color}); for(const [hole,pts] of Object.entries(traces)) ls.addPolyline(pts.map(p=>[p[1],p[2],p[3]]),{hole}); return ls; }
  bounds(){ const tr=this.desurvey(); const flat=[]; for(const pts of Object.values(tr))for(const p of pts)flat.push(p[1],p[2],p[3]); return xyzBounds(flat); }
  toJSON(){ return Object.assign(this.head(),{collars:this.collars,surveys:this.surveys,intervals:this.intervals}); }
  static fromJSON(d){ return new Drillholes(d); }
}

function solve3(M,v){
  const det=m=>m[0][0]*(m[1][1]*m[2][2]-m[1][2]*m[2][1])-m[0][1]*(m[1][0]*m[2][2]-m[1][2]*m[2][0])+m[0][2]*(m[1][0]*m[2][1]-m[1][1]*m[2][0]);
  const D=det(M); if(Math.abs(D)<1e-12) throw new Error('singular control-point system');
  return [0,1,2].map(col=>{ const m=M.map(r=>r.slice()); for(let r=0;r<3;r++)m[r][col]=v[r]; return det(m)/D; });
}
export class ImagePlane extends ModelObject{
  static kind='imageplane';
  constructor(o){ super(o); this.image=o.image; this.width=o.width|0; this.height=o.height|0; this.plane=o.plane||'plan'; this.p1=o.p1?o.p1.slice():null; this.p2=o.p2?o.p2.slice():null; this.zTop=o.z_top!=null?o.z_top:(o.zTop!=null?o.zTop:null); this.zBottom=o.z_bottom!=null?o.z_bottom:(o.zBottom!=null?o.zBottom:null); this.control=(o.control||[]).map(c=>c.slice()); this.elevation=o.elevation!=null?o.elevation:null; }
  affine(){
    const cp=this.control; if(cp.length<2) throw new Error('need >= 2 control points');
    if(cp.length===2){ const [px0,py0,X0,Y0]=cp[0],[px1,py1,X1,Y1]=cp[1]; const dpx=px1-px0,dpy=py1-py0,dX=X1-X0,dY=Y1-Y0,den=dpx*dpx+dpy*dpy; if(!den)throw new Error('coincident control points'); const ca=(dX*dpx-dY*dpy)/den, cb=(dY*dpx+dX*dpy)/den; const a=ca,b=cb,d=cb,e=-ca; return [a,b,X0-a*px0-b*py0,d,e,Y0-d*px0-e*py0]; }
    let sxx=0,syy=0,sxy=0,sx=0,sy=0,n=0; const bx=[0,0,0],by=[0,0,0];
    for(const [px,py,X,Y] of cp){ sxx+=px*px;syy+=py*py;sxy+=px*py;sx+=px;sy+=py;n++; bx[0]+=px*X;bx[1]+=py*X;bx[2]+=X; by[0]+=px*Y;by[1]+=py*Y;by[2]+=Y; }
    const M=[[sxx,sxy,sx],[sxy,syy,sy],[sx,sy,n]]; return [...solve3(M,bx),...solve3(M,by)];
  }
  pixelToWorld(px,py){
    if(this.plane==='section'){ const u=px/this.width, v=py/this.height; return [this.p1[0]+(this.p2[0]-this.p1[0])*u, this.p1[1]+(this.p2[1]-this.p1[1])*u, this.zTop+(this.zBottom-this.zTop)*v]; }
    const [a,b,c,d,e,f]=this.affine(); return [a*px+b*py+c, d*px+e*py+f, this.elevation!=null?this.elevation:NaN];
  }
  worldToPixel(x,y){ const [a,b,c,d,e,f]=this.affine(); const det=a*e-b*d; if(Math.abs(det)<1e-12)return null; const X=x-c,Y=y-f; return [(e*X-b*Y)/det, (-d*X+a*Y)/det]; }
  corners(){ const w=this.width,h=this.height; return [this.pixelToWorld(0,0),this.pixelToWorld(w,0),this.pixelToWorld(w,h),this.pixelToWorld(0,h)]; }
  bounds(){ const flat=[]; for(const c of this.corners())flat.push(...c); return xyzBounds(flat); }
  toJSON(){ return Object.assign(this.head(),{image:this.image,width:this.width,height:this.height,plane:this.plane,p1:this.p1,p2:this.p2,z_top:this.zTop,z_bottom:this.zBottom,control:this.control,elevation:this.elevation}); }
  static fromJSON(d){ return new ImagePlane(d); }
}

export class StratModel extends ModelObject{
  static kind='stratmodel';
  constructor(o={}){ super(o); this.units=(o.units||[]).map(u=>Object.assign({},u)); this.topography=o.topography||null; }
  toJSON(){ return Object.assign(this.head(),{units:this.units,topography:this.topography}); }
  static fromJSON(d){ return new StratModel(d); }
}

export class Section extends ModelObject{
  static kind='section';
  constructor(o={}){ super(o); this.start=o.start?o.start.slice():null; this.end=o.end?o.end.slice():null; this.zMin=o.z_min!=null?o.z_min:(o.zMin!=null?o.zMin:null); this.zMax=o.z_max!=null?o.z_max:(o.zMax!=null?o.zMax:null); this.point=o.point?o.point.slice():null; this.normal=o.normal?o.normal.slice():null; }
  plane(){ if(this.point&&this.normal){ const n=this.normal, ln=Math.hypot(...n)||1; return [this.point.slice(),n.map(c=>c/ln)]; } const [x0,y0]=this.start,[x1,y1]=this.end, dx=x1-x0,dy=y1-y0, ln=Math.hypot(dx,dy)||1; return [[x0,y0,0],[-dy/ln,dx/ln,0]]; }
  bounds(){ if(this.start&&this.end) return [Math.min(this.start[0],this.end[0]),Math.min(this.start[1],this.end[1]),this.zMin==null?NaN:this.zMin,Math.max(this.start[0],this.end[0]),Math.max(this.start[1],this.end[1]),this.zMax==null?NaN:this.zMax]; return null; }
  toJSON(){ return Object.assign(this.head(),{start:this.start,end:this.end,z_min:this.zMin,z_max:this.zMax,point:this.point,normal:this.normal}); }
  static fromJSON(d){ return new Section(d); }
}

export const KINDS={grid2d:Grid2D,mesh:Mesh,lineset:LineSet,points:PointSet,blockmodel:BlockModel,drillholes:Drillholes,imageplane:ImagePlane,stratmodel:StratModel,section:Section};

export class Project{
  constructor(o={}){ this.schema=SCHEMA; this.name=o.name||'model'; this.crs=Object.assign({kind:'local',units:'m'},o.crs||{}); this.origin=o.origin?o.origin.slice():null; this.site=Object.assign({},o.site||{}); this.metadata=Object.assign({},o.metadata||{}); this.created=o.created||nowISO(); this.modified=o.modified||this.created; this.objects=[]; this.listeners=new Set(); }
  add(obj){ this.objects.push(obj); this.touch(); this.emit('add',obj); return obj; }
  remove(obj){ const i=this.objects.indexOf(obj); if(i>=0){ this.objects.splice(i,1); this.touch(); this.emit('remove',obj);} }
  get(id){ return this.objects.find(o=>o.id===id)||null; }
  byKind(kind){ return this.objects.filter(o=>o.kind===kind); }
  touch(){ this.modified=nowISO(); }
  on(fn){ this.listeners.add(fn); return ()=>this.listeners.delete(fn); }
  emit(type,obj){ for(const fn of this.listeners) try{ fn(type,obj); }catch(e){ console.warn(e); } }
  bounds(){ const bs=this.objects.map(o=>{ try{ return o.bounds(); }catch(e){ return null; } }).filter(b=>b&&b.every(v=>v===v)); if(!bs.length)return null; return [Math.min(...bs.map(b=>b[0])),Math.min(...bs.map(b=>b[1])),Math.min(...bs.map(b=>b[2])),Math.max(...bs.map(b=>b[3])),Math.max(...bs.map(b=>b[4])),Math.max(...bs.map(b=>b[5]))]; }
  ensureOrigin(){ if(!this.origin){ const b=this.bounds(); if(b) this.origin=[Math.round((b[0]+b[3])/200)*100, Math.round((b[1]+b[4])/200)*100, 0]; } return this.origin; }
  toJSON(){ this.ensureOrigin(); return {schema:this.schema,name:this.name,crs:this.crs,origin:this.origin,site:this.site,metadata:this.metadata,created:this.created,modified:this.modified,generator:'nw-mineral-monitor model3d',objects:this.objects.map(o=>o.toJSON())}; }
  serialize(){ return JSON.stringify(this.toJSON(), (k,v)=> typeof v==='number' && !isFinite(v) ? null : v); }
  static fromJSON(d){ if(d.schema!==SCHEMA) throw new Error(`not a ${SCHEMA} project`); const p=new Project(d); for(const od of d.objects||[]){ const K=KINDS[od.kind]; if(!K)continue; try{ p.objects.push(K.fromJSON(od)); }catch(e){ console.warn('skip object',od.kind,od.name,e); } } return p; }
  static parse(text){ return Project.fromJSON(JSON.parse(text)); }
}

/* ------------------------------------------------------------------- UTM */
const A=6378137, F=1/298.257223563, E2=F*(2-F), EP2=E2/(1-E2), K0=0.9996;
const md=p=>A*((1-E2/4-3*E2*E2/64-5*E2**3/256)*p-(3*E2/8+3*E2*E2/32+45*E2**3/1024)*Math.sin(2*p)+(15*E2*E2/256+45*E2**3/1024)*Math.sin(4*p)-(35*E2**3/3072)*Math.sin(6*p));
export const utm={
  zone(lon,lat){ return {zone:Math.max(1,Math.min(60,Math.floor((lon+180)/6)+1)), north:lat>=0}; },
  epsg(zone,north){ return (north?32600:32700)+zone; },
  fwd(lon,lat,zone,north){
    const lon0=(zone*6-183)*Math.PI/180, phi=lat*Math.PI/180, lam=lon*Math.PI/180, sp=Math.sin(phi), cp=Math.cos(phi), tp=Math.tan(phi);
    const n=A/Math.sqrt(1-E2*sp*sp), t=tp*tp, c=EP2*cp*cp, a=(lam-lon0)*cp;
    const e=K0*n*(a+(1-t+c)*a**3/6+(5-18*t+t*t+72*c-58*EP2)*a**5/120)+500000;
    let nn=K0*(md(phi)+n*tp*(a*a/2+(5-t+9*c+4*c*c)*a**4/24+(61-58*t+t*t+600*c-330*EP2)*a**6/720)); if(!north)nn+=1e7; return [e,nn];
  },
  inv(e,nn,zone,north){
    const lon0=(zone*6-183)*Math.PI/180, x=e-500000, y=nn-(north?0:1e7), mu=(y/K0)/(A*(1-E2/4-3*E2*E2/64-5*E2**3/256)), e1=(1-Math.sqrt(1-E2))/(1+Math.sqrt(1-E2));
    const p1=mu+(3*e1/2-27*e1**3/32)*Math.sin(2*mu)+(21*e1*e1/16-55*e1**4/32)*Math.sin(4*mu)+(151*e1**3/96)*Math.sin(6*mu)+(1097*e1**4/512)*Math.sin(8*mu);
    const sp=Math.sin(p1), cp=Math.cos(p1), tp=Math.tan(p1), c1=EP2*cp*cp, t1=tp*tp, n1=A/Math.sqrt(1-E2*sp*sp), r1=A*(1-E2)/Math.pow(1-E2*sp*sp,1.5), d=x/(n1*K0);
    const phi=p1-(n1*tp/r1)*(d*d/2-(5+3*t1+10*c1-4*c1*c1-9*EP2)*d**4/24+(61+90*t1+298*c1+45*t1*t1-252*EP2-3*c1*c1)*d**6/720);
    const lam=lon0+(d-(1+2*t1+c1)*d**3/6+(5-2*c1+28*t1-3*c1*c1+8*EP2+24*t1*t1)*d**5/120)/cp;
    return [lam*180/Math.PI, phi*180/Math.PI];
  },
  wkt(zone,north){ return `PROJCS["WGS_1984_UTM_Zone_${zone}${north?'N':'S'}",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",${north?'0':'10000000'}.0],PARAMETER["Central_Meridian",${zone*6-183}.0],PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]`; },
  crs(zone,north){ return {kind:'utm',zone,north,epsg:(north?32600:32700)+zone,units:'m'}; },
  looksLonLat(x,y){ return Math.abs(x)<=180 && Math.abs(y)<=90; }
};

/* --------------------------------------------------------------- storage */
const DB_NAME='nwmm-geomodel';
let dbP=null;
function openDB(){
  if(dbP) return dbP;
  dbP=new Promise((res,rej)=>{
    if(!('indexedDB' in globalThis)) return rej(new Error('no IndexedDB'));
    const rq=indexedDB.open(DB_NAME,1);
    rq.onupgradeneeded=()=>{ const db=rq.result; if(!db.objectStoreNames.contains('projects'))db.createObjectStore('projects',{keyPath:'id'}); if(!db.objectStoreNames.contains('handoff'))db.createObjectStore('handoff',{keyPath:'key'}); };
    rq.onsuccess=()=>res(rq.result); rq.onerror=()=>rej(rq.error);
  });
  return dbP;
}
function tx(store,mode,fn){ return openDB().then(db=>new Promise((res,rej)=>{ const t=db.transaction(store,mode), s=t.objectStore(store); const rq=fn(s); t.oncomplete=()=>res(rq&&rq.result); t.onerror=()=>rej(t.error); })); }
export const store={
  async saveProject(p){ const rec={id:p.site&&p.site.key?p.site.key:slug(p.name), name:p.name, modified:p.modified, site:p.site, json:p.serialize()}; await tx('projects','readwrite',s=>s.put(rec)); return rec.id; },
  async loadProject(id){ const rec=await tx('projects','readonly',s=>s.get(id)); return rec? Project.parse(rec.json) : null; },
  async listProjects(){ const recs=await tx('projects','readonly',s=>s.getAll()); return (recs||[]).map(r=>({id:r.id,name:r.name,modified:r.modified,site:r.site,bytes:r.json.length})).sort((a,b)=>b.modified.localeCompare(a.modified)); },
  async deleteProject(id){ await tx('projects','readwrite',s=>s.delete(id)); },
  async putHandoff(key,data){ await tx('handoff','readwrite',s=>s.put({key,data,at:nowISO()})); },
  async takeHandoff(key){ const rec=await tx('handoff','readonly',s=>s.get(key)); if(rec) await tx('handoff','readwrite',s=>s.delete(key)).catch(()=>{}); return rec?rec.data:null; },
  async putUserLayer(rec){ // into the map's own 'nwmm-userlayers' DB so SEND TO MAP shows up under MY DATA
    const db=await new Promise((res,rej)=>{ const rq=indexedDB.open('nwmm-userlayers',1); rq.onupgradeneeded=()=>rq.result.createObjectStore('layers',{keyPath:'slug'}); rq.onsuccess=()=>res(rq.result); rq.onerror=()=>rej(rq.error); });
    await new Promise((res,rej)=>{ const t=db.transaction('layers','readwrite'); t.objectStore('layers').put(rec); t.oncomplete=res; t.onerror=()=>rej(t.error); });
  }
};
export function slug(s){ return String(s||'model').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'')||'model'; }

/* ------------------------------------------------------------------ misc */
export const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
export const fmt=(n,d=1)=> (n==null||n!==n)?'—': (Math.abs(n)>=1000? Math.round(n).toLocaleString('en-US') : (+n).toFixed(d));
export function hexColor(c){ return '#'+c.map(v=>Math.max(0,Math.min(255,Math.round(v))).toString(16).padStart(2,'0')).join(''); }
export function parseHex(h){ const m=/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(h); return m?[parseInt(m[1],16),parseInt(m[2],16),parseInt(m[3],16)]:[160,160,160]; }
export const COLORMAPS={
  viridis:[[68,1,84],[72,40,120],[62,74,137],[49,104,142],[38,130,142],[31,158,137],[53,183,121],[109,205,89],[180,222,44],[253,231,37]],
  magma:[[0,0,4],[28,16,68],[79,18,123],[129,37,129],[181,54,122],[229,80,100],[251,135,97],[254,194,135],[252,253,191],[252,253,191]],
  turbo:[[48,18,59],[70,107,227],[40,176,228],[43,223,161],[115,251,82],[200,248,48],[252,203,42],[247,132,24],[206,58,8],[122,4,3]],
  grey:[[20,20,20],[240,240,240]],
  terrain:[[40,90,60],[90,140,70],[160,180,90],[200,180,120],[170,130,90],[200,200,200],[255,255,255]],
  rdbu:[[178,24,43],[239,138,98],[253,219,199],[247,247,247],[209,229,240],[103,169,207],[33,102,172]],
  geology:[[222,184,135],[205,133,63],[160,160,200],[120,170,120],[200,120,120],[190,190,100],[100,150,190],[170,120,170]],
};
export function colormap(name,t){ const cm=COLORMAPS[name]||COLORMAPS.viridis; if(t!==t)return [0,0,0]; t=Math.max(0,Math.min(1,t)); const x=t*(cm.length-1), i=Math.min(cm.length-2,Math.floor(x)), f=x-i; return [0,1,2].map(k=>cm[i][k]+(cm[i+1][k]-cm[i][k])*f); }
export function downloadBlob(blob,filename){ const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=filename; document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),30000); }
