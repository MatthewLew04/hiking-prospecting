/* gm-render.js — three.js rendering of geomodel objects.
   Scene frame: three X = east - ox, three Y = elevation - oz (vertical
   exaggeration is a scale on the root group), three Z = -(north - oy).
   Every model object becomes one THREE.Group under root; `Renderer.sync(obj)`
   rebuilds it from the object's current state + display options. */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/OrbitControls.js';
import * as GM from './gm-core.js';
import * as E from './gm-engine.js';

const DEG = Math.PI / 180;

/* How sure we are of a working, and how that has to look.  A model built from
   old prose is a digitising bridge, not new evidence, so an element read off a
   sentence must never be drawn the way a surveyed one is: surveyed solid,
   described dashed, assumed/sketched dotted.  `gm-viewer` reuses these for the
   viewport banner and the legend so the three surfaces cannot drift apart.
   `dash` is [dash, gap] in SCREEN PIXELS (it used to be world metres, which
   went sub-pixel as soon as the camera pulled back): the renderer converts it
   to metres from metres-per-pixel every time the zoom drifts by more than
   10 % (`Renderer.updateDashes`), and the viewer classifies the pattern with
   `dash[0] > 5 → dashed, else dotted`. */
export const CONF_CLASSES = [
  { key: 'surveyed', label: 'surveyed', hint: 'traced off a georeferenced plan', dash: null },
  { key: 'described', label: 'described', hint: 'parsed from a written description', dash: [8, 5] },
  { key: 'assumed', label: 'assumed', hint: 'supplied in answer to a gap', dash: [2, 4] },
];
/* Label priority for decluttering: a point with a cited grade outranks a bare
   name.  Grade columns are au / ag / cu / pb / zn or au_ozt, ag_opt, ... */
const GRADE_COL = /^(au|ag|cu|pb|zn)(_|$)/i;
/* Layers that are backdrop rather than data: when one of these is drawn
   see-through, a pick must reach the working or the mine behind it. */
const CONTEXT_MESH_ROLES = new Set(['geology', 'unit', 'contact', 'surface']);
function isContextObj(o) { return !!o && (o.kind === 'grid2d' || o.kind === 'imageplane' || (o.kind === 'mesh' && CONTEXT_MESH_ROLES.has(o.role))); }
function materialOpacity(o) { const m = Array.isArray(o.material) ? o.material[0] : o.material; return m && m.opacity != null ? m.opacity : 1; }
export function confClass(f) {
  const c = String((f && f.confidence) || 'described').toLowerCase();
  if (c === 'surveyed' || c === 'traced') return 'surveyed';
  if (c === 'assumed' || c === 'sketched') return 'assumed';
  return 'described';
}
/** `{surveyed, described, assumed}` counts over every working in a project.
    Stopes are meshes carrying role 'stope' rather than 'workings', and they are
    exactly the elements most often placed from an answer, so leaving them out
    would under-report the assumed count the banner exists to declare. */
export function confidenceTally(project) {
  const t = { surveyed: 0, described: 0, assumed: 0 };
  for (const o of (project ? project.objects : [])) {
    if (o.kind === 'lineset' && o.role === 'workings') for (const f of o.features) t[confClass(f)]++;
    else if (o.kind === 'mesh' && (o.role === 'workings' || o.role === 'stope')) t[confClass(o.metadata)]++;
  }
  return t;
}

export const CATEGORY_PALETTE = [[104, 176, 255], [244, 162, 97], [138, 201, 38], [231, 111, 81], [187, 148, 255], [42, 196, 179], [255, 209, 102], [148, 163, 184], [214, 93, 177], [125, 211, 252]];

/* Which ramp a layer gets when the user has not chosen one.  The default
   depends on what is being coloured (a draped property grid is not read like
   a topographic surface), and it is applied in three places: the builders
   below, the inspector's colormap dropdown and the legend the grade is read
   off.  They must name the same ramp — a legend describing a ramp the
   geometry never used is a wrong number, not a cosmetic slip — so the
   fallback lives here and nowhere else. */
export function defaultColormap(obj, d = {}, categorical = false) {
  if (d.colormap) return d.colormap;
  if (categorical) return 'geology';
  switch (obj.kind) {
    case 'grid2d': { const mode = d.mode || (obj.role === 'property' ? 'draped' : 'surface'); return (mode === 'draped' || mode === 'flat') ? 'turbo' : 'terrain'; }
    case 'blockmodel': case 'drillholes': return 'turbo';
    case 'points': return (obj.role === 'structural' || obj.role === 'trend') && d.attribute === 'dip' ? 'turbo' : 'viridis';
    default: return 'viridis';
  }
}

export class Renderer {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.origin = opts.origin || [0, 0, 0];
    this.ve = opts.ve || 1.5;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false, preserveDrawingBuffer: true, logarithmicDepthBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x0b0e13, 1);
    this.renderer.localClippingEnabled = true;
    this.scene = new THREE.Scene();
    this.scene.fog = null;
    this.camera = new THREE.PerspectiveCamera(50, 1, 1, 2e6);
    this.projection = 'persp'; this.orthoHeight = 2000;
    this.camera.position.set(1500, 1200, 1500);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true; this.controls.dampingFactor = 0.12;
    this.controls.screenSpacePanning = true;
    this.controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
    this.root = new THREE.Group(); this.root.scale.set(1, this.ve, 1); this.scene.add(this.root);
    this.helpers = new THREE.Group(); this.scene.add(this.helpers);
    this.overlay = new THREE.Group(); this.root.add(this.overlay);     // tool feedback (in model units)
    const hemi = new THREE.HemisphereLight(0xdfe8f0, 0x2a2f36, 0.9); this.scene.add(hemi);
    const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(-0.6, 1, 0.4).multiplyScalar(5000); this.scene.add(sun);
    const sun2 = new THREE.DirectionalLight(0x8fb3ff, 0.35); sun2.position.set(0.8, 0.6, -0.7).multiplyScalar(5000); this.scene.add(sun2);
    this.layers = new Map();      // obj.id -> {obj, group, display}
    this.raycaster = new THREE.Raycaster();
    this.raycaster.params.Points.threshold = 6;
    this.raycaster.params.Line.threshold = 4;
    this.clip = { active: false, plane: null, side: 1, planes: [] };
    this.needs = true;
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas.parentElement);
    this.resize();
    this.controls.addEventListener('change', () => { this.needs = true; });
    this._pointTexture = null;
    this.onRender = null;
    this.labelsDecluttered = true; this.labelsDirty = true; this._declutterAt = 0; this._declutterCam = null; this._dashMpp = 0;
    const loop = () => {
      // OrbitControls.update() reports whether damping actually moved the
      // camera; an idle scene must not re-render every frame forever.  The
      // first frame renders because `needs` starts true.
      if (this.controls.update()) this.needs = true;
      if (this.needs) this.updateDashes();
      this.declutterLabels();           // throttled inside; only after the camera moved
      if (this.needs) { this.needs = false; this.renderer.render(this.scene, this.camera); if (this.onRender) this.onRender(); }
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }
  resize() {
    const el = this.canvas.parentElement; const w = el.clientWidth || 800, h = el.clientHeight || 600;
    this.renderer.setSize(w, h, false);
    if (this.camera.isOrthographicCamera) { const a = w / Math.max(1, h), hh = this.orthoHeight || 2000; this.camera.left = -hh * a / 2; this.camera.right = hh * a / 2; this.camera.top = hh / 2; this.camera.bottom = -hh / 2; }
    else this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix(); this.needs = true;
  }
  /** Orthographic is what Leapfrog recommends for modelling: perspective
      skews the interpretation, and the scale bar is only meaningful without
      foreshortening. */
  setProjection(kind) {
    kind = kind === 'ortho' ? 'ortho' : 'persp';
    if (kind === this.projection) return kind;
    const t = this.controls.target.clone(), pos = this.camera.position.clone(), up = this.camera.up.clone();
    const dist = Math.max(1, pos.distanceTo(t));
    const el = this.canvas.parentElement, w = el.clientWidth || 800, hgt = el.clientHeight || 600, aspect = w / Math.max(1, hgt);
    let cam;
    if (kind === 'ortho') {
      const fov = this.camera.fov || 50;
      this.orthoHeight = 2 * Math.tan(fov * DEG / 2) * dist;
      cam = new THREE.OrthographicCamera(-this.orthoHeight * aspect / 2, this.orthoHeight * aspect / 2, this.orthoHeight / 2, -this.orthoHeight / 2, -dist * 50, dist * 50);
    } else {
      cam = new THREE.PerspectiveCamera(50, aspect, Math.max(0.5, dist / 5000), dist * 200);
    }
    cam.position.copy(pos); cam.up.copy(up); cam.zoom = 1; cam.updateProjectionMatrix();
    this.camera = cam; this.controls.object = cam; this.controls.target.copy(t); this.controls.update();
    this.projection = kind; this.resize(); this.updateDashes(true); this.invalidate();
    return kind;
  }
  /** metres per device pixel at the orbit target — drives the scale bar */
  metresPerPixel() {
    const el = this.canvas.parentElement, hgt = el.clientHeight || 600;
    if (this.camera.isOrthographicCamera) return (this.camera.top - this.camera.bottom) / this.camera.zoom / hgt;
    const d = this.camera.position.distanceTo(this.controls.target);
    return 2 * Math.tan((this.camera.fov || 50) * DEG / 2) * d / hgt;
  }
  invalidate() { this.needs = true; }
  setVE(ve) { this.ve = ve; this.root.scale.set(1, ve, 1); this.applyClipping(); this.updateDashes(true); this.labelsDirty = true; this.invalidate(); }
  /** Dash patterns are specified in screen pixels (CONF_CLASSES) but drawn in
      metres, so they are rescaled whenever metres-per-pixel drifts by more
      than 10 %: a described adit stays visibly dashed from any distance. */
  updateDashes(force = false) {
    const mpp = this.metresPerPixel(); if (!(mpp > 0)) return;
    if (!force && this._dashMpp && Math.abs(mpp - this._dashMpp) <= 0.1 * this._dashMpp) return;
    this._dashMpp = mpp; let changed = false;
    for (const L of this.layers.values()) L.group.traverse(o => {
      const m = o.material; if (!m || !m.isLineDashedMaterial || !o.userData.confidence) return;
      const px = m.userData.dashPx || (CONF_CLASSES.find(c => c.key === o.userData.confidence) || {}).dash; if (!px) return;
      m.dashSize = px[0] * mpp; m.gapSize = px[1] * mpp; changed = true;
    });
    if (changed) this.needs = true;
  }
  /** A LineDashedMaterial for a confidence class, dashed in pixels at the
      current zoom (`updateDashes` keeps it there). */
  dashedMaterial(cls, extra = {}) {
    const mpp = this.metresPerPixel() || 1;
    const m = new THREE.LineDashedMaterial(Object.assign({ vertexColors: true, dashSize: cls.dash[0] * mpp, gapSize: cls.dash[1] * mpp }, extra));
    m.userData.dashPx = cls.dash.slice(); return m;
  }
  /** Greedy label decluttering: project every label sprite to the screen,
      keep the important ones first (site > graded / workings > rest, nearer
      first) and hide any whose box overlaps one already placed.  Throttled
      to ~4 Hz and skipped while the camera has not moved; above 400 labels
      it stands down and says so in `labelsDecluttered`. */
  declutterLabels(force = false) {
    const now = performance.now();
    if (!force && now - this._declutterAt < 250) return;
    const cam = this.camera; cam.updateMatrixWorld();
    const key = cam.matrixWorld.elements.join(',') + '|' + cam.projectionMatrix.elements.join(',');
    if (!force && !this.labelsDirty && key === this._declutterCam) return;
    this._declutterAt = now; this._declutterCam = key; this.labelsDirty = false;
    const sprites = [];
    for (const L of this.layers.values()) { if (!L.group.visible) continue; L.group.traverse(o => { if (o.isSprite && o.userData.label) sprites.push(o); }); }
    let changed = false;
    if (sprites.length > 400) { this.labelsDecluttered = false; for (const s of sprites) if (!s.visible) { s.visible = true; changed = true; } if (changed) this.needs = true; return; }
    this.labelsDecluttered = true;
    const el = this.canvas.parentElement, w = el.clientWidth || 800, h = el.clientHeight || 600;
    // sizeAttenuation:false sprites are `scale × depth` wide in view space
    // under perspective (so a fixed pixel size), and `scale` world units in ortho
    const ortho = cam.isOrthographicCamera;
    const pxPerScale = ortho ? cam.zoom * h / Math.max(1e-9, cam.top - cam.bottom) : (h / 2) / Math.tan((cam.fov || 50) * DEG / 2);
    const v = new THREE.Vector3(); const items = [];
    for (const s of sprites) {
      s.updateWorldMatrix(true, false); v.setFromMatrixPosition(s.matrixWorld);
      const dist = v.distanceTo(cam.position);
      v.applyMatrix4(cam.matrixWorldInverse);
      if (!ortho && v.z >= 0) { items.push({ s, off: true }); continue; }
      v.applyMatrix4(cam.projectionMatrix);
      const sx = (v.x + 1) / 2 * w, sy = (1 - v.y) / 2 * h, sw = s.scale.x * pxPerScale, sh = s.scale.y * pxPerScale;
      const x0 = sx - sw * s.center.x, x1 = x0 + sw, y1 = sy + sh * s.center.y, y0 = y1 - sh;
      if (x1 < 0 || y1 < 0 || x0 > w || y0 > h) { items.push({ s, off: true }); continue; }
      items.push({ s, off: false, pr: s.userData.priority || 1, dist, box: [x0, y0, x1, y1] });
    }
    items.sort((a, b) => ((b.pr || 0) - (a.pr || 0)) || ((a.dist || 0) - (b.dist || 0)));
    const placed = [];
    for (const it of items) {
      let vis = true;
      if (!it.off) { for (const b of placed) if (it.box[0] < b[2] && it.box[2] > b[0] && it.box[1] < b[3] && it.box[3] > b[1]) { vis = false; break; } if (vis) placed.push(it.box); }
      if (it.s.visible !== vis) { it.s.visible = vis; changed = true; }
    }
    if (changed) this.needs = true;
  }
  /* world <-> scene */
  toScene(x, y, z) { return new THREE.Vector3(x - this.origin[0], (z === z ? z : 0) - this.origin[2], -(y - this.origin[1])); }
  toSceneArr(x, y, z) { return [x - this.origin[0], (z === z ? z : 0) - this.origin[2], -(y - this.origin[1])]; }
  fromScene(v) { return [v.x + this.origin[0], -v.z + this.origin[1], v.y / this.ve + this.origin[2]]; }   // v in WORLD (exaggerated) scene coords
  /* camera */
  fitTo(bounds, pad = 1.15) {
    if (!bounds) return;
    const c = this.toScene((bounds[0] + bounds[3]) / 2, (bounds[1] + bounds[4]) / 2, (bounds[2] + bounds[5]) / 2); c.y *= this.ve;
    const size = Math.max(bounds[3] - bounds[0], bounds[4] - bounds[1], (bounds[5] - bounds[2]) * this.ve, 10);
    const fov = this.camera.fov || 50;
    const dist = size * pad / Math.tan(fov * DEG / 2) * 0.6;
    const dir = new THREE.Vector3(0.55, 0.55, 0.65).normalize();
    this.camera.position.copy(c).addScaledVector(dir, dist);
    this.controls.target.copy(c);
    if (this.camera.isOrthographicCamera) { this.orthoHeight = size * pad; this.camera.zoom = 1; this.camera.near = -dist * 50; this.camera.far = dist * 50; this.resize(); }
    else { this.camera.near = Math.max(0.5, dist / 5000); this.camera.far = dist * 50; }
    this.camera.updateProjectionMatrix();
    this.controls.update(); this.invalidate();
  }
  viewFrom(dirName) {
    const t = this.controls.target.clone(), d = this.camera.position.distanceTo(t);
    // `top` is a true plan: the camera sits on +Y looking down with north up
    // the screen (three -Z is north, so up = -Z).  `below` looks up from
    // underneath — the only way to see the underside of a vein sheet.
    const dirs = { top: [0, 1, 0], below: [0, -1, 0], north: [0, 0.15, -1], south: [0, 0.15, 1], east: [1, 0.15, 0], west: [-1, 0.15, 0], iso: [0.55, 0.55, 0.65] };
    const v = new THREE.Vector3(...(dirs[dirName] || dirs.iso)).normalize();
    this.controls.maxPolarAngle = Math.PI;
    this.camera.position.copy(t).addScaledVector(v, d);
    if (dirName === 'top') this.camera.up.set(0, 0, -1); else if (dirName === 'below') this.camera.up.set(0, 0, 1); else this.camera.up.set(0, 1, 0);
    this.controls.update(); this.invalidate();
  }
  /** Camera state for saved scenes: everything needed to come back to
      exactly this view. */
  getView() { return { position: this.camera.position.toArray(), target: this.controls.target.toArray(), up: this.camera.up.toArray(), projection: this.projection, ve: this.ve, zoom: this.camera.zoom, orthoHeight: this.orthoHeight }; }
  setView(v) {
    if (!v) return;
    if (v.projection && v.projection !== this.projection) this.setProjection(v.projection);
    if (v.ve) this.setVE(v.ve);
    if (v.position) this.camera.position.fromArray(v.position); if (v.target) this.controls.target.fromArray(v.target); if (v.up) this.camera.up.fromArray(v.up);
    if (v.orthoHeight && this.camera.isOrthographicCamera) { this.orthoHeight = v.orthoHeight; this.resize(); }
    if (v.zoom) { this.camera.zoom = v.zoom; this.camera.updateProjectionMatrix(); }
    this.controls.update(); this.invalidate();
  }
  /** Azimuth (clockwise from north) and plunge (positive = looking down) of
      the view direction, for the status bar and the polarity check. */
  viewAzPlunge() { const c = this.camera.getWorldDirection(new THREE.Vector3()); const az = (Math.atan2(c.x, -c.z) * 180 / Math.PI % 360 + 360) % 360; const plunge = Math.asin(Math.max(-1, Math.min(1, -c.y))) * 180 / Math.PI; return { az, plunge }; }
  /** Highlight one part of a lineset (or one point) with an overlay that is
      independent of the tool overlay, so hovering a feature row never wipes a
      half-drawn trace.  highlight(null) clears it. */
  highlight(obj, index = null) {
    if (!this.hl) { this.hl = new THREE.Group(); this.root.add(this.hl); }
    for (const c of [...this.hl.children]) { this.hl.remove(c); disposeGroup(c); }
    if (obj && obj.kind === 'lineset' && index != null && index < obj.parts.length) {
      const pos = []; for (const p of obj.partXYZ(index)) pos.push(...this.toSceneArr(p[0], p[1], p[2]));
      const l = new THREE.Line(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)), new THREE.LineBasicMaterial({ color: 0xffffff, depthTest: false, transparent: true, opacity: 0.95 }));
      l.renderOrder = 998; l.userData.clippable = false; this.hl.add(l);
      const pts = obj.partXYZ(index); for (const p of [pts[0], pts[pts.length - 1]]) { const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: this.pointTexture(), color: 0xffffff, depthTest: false, sizeAttenuation: false })); sp.scale.set(8 / 300, 8 / 300, 1); const s = this.toSceneArr(p[0], p[1], p[2]); sp.position.set(s[0], s[1], s[2]); sp.renderOrder = 999; sp.userData.clippable = false; this.hl.add(sp); }
    } else if (obj && obj.kind === 'points' && index != null && index < obj.n) {
      const p = obj.point(index); const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: this.pointTexture(), color: 0xffffff, depthTest: false, sizeAttenuation: false })); sp.scale.set(16 / 300, 16 / 300, 1); const s = this.toSceneArr(p[0], p[1], p[2]); sp.position.set(s[0], s[1], s[2]); sp.renderOrder = 999; sp.userData.clippable = false; this.hl.add(sp);
    } else if (obj && obj.bounds && index == null) {
      const b = obj.bounds(); if (b) { const box = new THREE.Box3(new THREE.Vector3(...this.toSceneArr(b[0], b[4], b[2] === b[2] ? b[2] : 0)), new THREE.Vector3(...this.toSceneArr(b[3], b[1], b[5] === b[5] ? b[5] : 0))); const hb = new THREE.Box3Helper(box, 0xffffff); hb.material.depthTest = false; hb.material.transparent = true; hb.material.opacity = 0.7; hb.userData.clippable = false; this.hl.add(hb); }
    }
    this.invalidate();
  }
  /* picking */
  pick(clientX, clientY, filter = null) {
    const r = this.canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - r.left) / r.width) * 2 - 1, -((clientY - r.top) / r.height) * 2 + 1);
    // Pixel-consistent tolerances: a mine dot or a hairline is a few pixels
    // wide whatever the zoom, so the raycaster's world-unit thresholds follow
    // metres-per-pixel at the orbit target.  Geometry is built in metres under
    // the exaggerated root (scale 1, ve, 1) and three tests points and lines in
    // the object's own frame divided by the object's own scale (1), so no `ve`
    // correction applies horizontally.
    const tol = 6 * this.metresPerPixel();
    if (tol > 0) { this.raycaster.params.Points.threshold = tol; this.raycaster.params.Line.threshold = tol; }
    this.raycaster.setFromCamera(ndc, this.camera);
    const targets = [];
    for (const L of this.layers.values()) { if (!L.group.visible) continue; if (filter && !filter(L.obj)) continue; L.group.traverse(o => { if (o.isMesh || o.isPoints || o.isLine || o.isLineSegments) targets.push(o); }); }
    const hits = this.raycaster.intersectObjects(targets, false);
    const planes = this.clip.active ? this.clip.planes : [];
    let first = null;
    for (const h of hits) {
      if (planes.length && h.object.userData.clippable !== false && planes.some(pl => pl.distanceToPoint(h.point) < -1e-6)) continue;
      const layer = h.object.userData.layerId ? this.layers.get(h.object.userData.layerId) : null;
      const res = { hit: h, obj: layer ? layer.obj : null, world: this.fromScene(h.point), index: h.index, instanceId: h.instanceId, faceIndex: h.faceIndex, object: h.object };
      const context = layer && isContextObj(layer.obj);
      // A see-through backdrop (draped geology at 0.4, terrain in see-through
      // mode, a faint scan) must not swallow the working or the mine visible
      // behind it; an opaque backdrop still hides what is under it.
      if (!first) { first = res; if (!context || materialOpacity(h.object) >= 0.6) return res; continue; }
      if (!context) return res;
      if (materialOpacity(h.object) >= 0.6) return first;
    }
    return first;
  }
  pickPlane(clientX, clientY, planeSceneWorld) {   // intersect ray with a THREE.Plane (scene/world coords)
    const r = this.canvas.getBoundingClientRect();
    this.raycaster.setFromCamera(new THREE.Vector2(((clientX - r.left) / r.width) * 2 - 1, -((clientY - r.top) / r.height) * 2 + 1), this.camera);
    const p = new THREE.Vector3(); return this.raycaster.ray.intersectPlane(planeSceneWorld, p) ? this.fromScene(p) : null;
  }
  /* clipping: plane given in WORLD model coords (point, normal).  With a
     `thickness` (metres) the cut becomes a slab: two planes facing each other
     ± thickness/2 about the section keep only the slice between them. */
  setClip(point, normal, side = 1, active = true, thickness = null) {
    if (!active || !point) { this.clip = { active: false, plane: null, side: 1, planes: [], thickness: null }; this.applyClipping(); return; }
    this.clip.active = true; this.clip.side = side; this.clip.worldPoint = point.slice(); this.clip.worldNormal = normal.slice(); this.clip.thickness = thickness > 0 ? +thickness : null;
    this.applyClipping();
  }
  /** A THREE.Plane in scene (exaggerated) coordinates through world point p
      with world normal n; `flip` keeps the other side. */
  scenePlane(p, n, flip = false) {
    // three points of the plane in model coords -> scene (exaggerated) coords
    const { u, v } = E.planeBasis(p, n);
    const P0 = this.toScene(p[0], p[1], p[2]), P1 = this.toScene(p[0] + u[0] * 100, p[1] + u[1] * 100, p[2] + u[2] * 100), P2 = this.toScene(p[0] + v[0] * 100, p[1] + v[1] * 100, p[2] + v[2] * 100);
    for (const P of [P0, P1, P2]) P.y *= this.ve;
    const nrm = new THREE.Vector3().subVectors(P1, P0).cross(new THREE.Vector3().subVectors(P2, P0)).normalize();
    const want = this.toScene(p[0] + n[0] * 100, p[1] + n[1] * 100, p[2] + n[2] * 100); want.y *= this.ve; want.sub(P0);
    if (nrm.dot(want) < 0) nrm.negate();
    if (flip) nrm.negate();
    return new THREE.Plane().setFromNormalAndCoplanarPoint(nrm, P0);
  }
  applyClipping() {
    let planes = [];
    if (this.clip.active && this.clip.worldPoint) {
      const p = this.clip.worldPoint, n = this.clip.worldNormal, t = this.clip.thickness;
      if (t) {
        const nl = Math.hypot(n[0], n[1], n[2]) || 1; const hh = t / 2 / nl;
        const off = s => [p[0] + n[0] * s, p[1] + n[1] * s, p[2] + n[2] * s];
        // +n through p − n·h keeps n·(x−p) ≥ −h; −n through p + n·h keeps n·(x−p) ≤ h
        const a = this.scenePlane(off(-hh), n, false), b = this.scenePlane(off(hh), n, true);
        this.clip.plane = this.clip.side < 0 ? b : a; planes = [a, b];
      } else { const plane = this.scenePlane(p, n, this.clip.side < 0); this.clip.plane = plane; planes = [plane]; }
    } else this.clip.plane = null;
    this.clip.planes = planes;
    for (const L of this.layers.values()) L.group.traverse(o => { if (o.material && o.userData.clippable !== false) { const mats = Array.isArray(o.material) ? o.material : [o.material]; for (const m of mats) { m.clippingPlanes = planes; m.clipShadows = false; m.needsUpdate = true; } } });
    this.invalidate();
  }
  /* layer management */
  remove(id) { const L = this.layers.get(id); if (!L) return; this.root.remove(L.group); disposeGroup(L.group); this.layers.delete(id); this.invalidate(); }
  setVisible(id, v) { const L = this.layers.get(id); if (L) { L.group.visible = v; this.invalidate(); } }
  /** Layer opacity multiplies each material's own base opacity (stamped at
      build time), so a described tube built translucent stays fainter than a
      surveyed one at every position of the inspector slider. */
  setOpacity(id, a) { const L = this.layers.get(id); if (!L) return; L.opacity = a; L.group.traverse(o => { if (o.material) { const mats = Array.isArray(o.material) ? o.material : [o.material]; for (const m of mats) applyOpacity(m, a); } }); this.invalidate(); }
  sync(obj, display = {}) {
    this.remove(obj.id);
    const L = { obj, group: new THREE.Group(), display: Object.assign({}, display), buildError: null };
    L.group.name = obj.name; L.group.visible = obj.visible !== false;
    // a layer that fails to draw says so on its row instead of vanishing
    try { this.build(obj, L); L.buildError = null; } catch (e) { console.warn('[render]', obj.kind, obj.name, e); L.buildError = (e && e.message) || String(e); }
    L.empty = !L.group.children.length;
    L.group.traverse(o => { o.userData.layerId = obj.id; if (o.material) { const mats = Array.isArray(o.material) ? o.material : [o.material]; for (const m of mats) stampOpacity(m); } });
    this.root.add(L.group); this.layers.set(obj.id, L);
    if (obj.opacity != null && obj.opacity < 1) this.setOpacity(obj.id, obj.opacity);
    if (this.clip.planes.length) this.applyClipping();
    this.labelsDirty = true;
    this.invalidate();
    return L;
  }
  build(obj, L) {
    const d = L.display;
    switch (obj.kind) {
      case 'grid2d': return this.buildGrid(obj, L);
      case 'mesh': return this.buildMesh(obj, L);
      case 'lineset': return this.buildLines(obj, L);
      case 'points': return (obj.role === 'structural' || obj.role === 'trend') ? this.buildStructural(obj, L) : this.buildPoints(obj, L);
      case 'blockmodel': return this.buildBlocks(obj, L);
      case 'drillholes': return this.buildDrillholes(obj, L);
      case 'imageplane': return this.buildImage(obj, L);
      case 'section': return this.buildSection(obj, L);
      default: return null;
    }
  }
  /* --- geometry helpers --- */
  geomFromMesh(mesh, colors = null) {
    const n = mesh.nVertices, pos = new Float32Array(n * 3);
    const V = mesh.vertices, ox = this.origin[0], oy = this.origin[1], oz = this.origin[2];
    for (let i = 0; i < n; i++) { const z = V[3 * i + 2]; pos[3 * i] = V[3 * i] - ox; pos[3 * i + 1] = (z === z ? z : 0) - oz; pos[3 * i + 2] = -(V[3 * i + 1] - oy); }
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setIndex(new THREE.BufferAttribute(mesh.triangles.length > 65535 * 3 || n > 65535 ? new Uint32Array(mesh.triangles) : new Uint16Array(mesh.triangles), 1));
    if (colors) g.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    g.computeVertexNormals(); g.computeBoundingSphere(); return g;
  }
  colorsForAttribute(values, cmap, range) {
    const n = values.length, col = new Float32Array(n * 3); let lo = range ? range[0] : Infinity, hi = range ? range[1] : -Infinity;
    if (!range) for (const v of values) { if (v !== v) continue; if (v < lo) lo = v; if (v > hi) hi = v; }
    for (let i = 0; i < n; i++) { const v = values[i]; const c = v !== v ? [70, 70, 70] : GM.colormap(cmap, hi > lo ? (v - lo) / (hi - lo) : 0.5); col[3 * i] = c[0] / 255; col[3 * i + 1] = c[1] / 255; col[3 * i + 2] = c[2] / 255; }
    return { colors: col, range: [lo, hi] };
  }
  material(color, opts = {}) {
    const m = new THREE.MeshStandardMaterial({ color: new THREE.Color(color[0] / 255, color[1] / 255, color[2] / 255), roughness: 0.85, metalness: 0.05, side: THREE.DoubleSide, flatShading: !!opts.flat, vertexColors: !!opts.vertexColors, transparent: !!opts.transparent, opacity: opts.opacity == null ? 1 : opts.opacity, wireframe: !!opts.wireframe, map: opts.map || null, clippingPlanes: this.clip.planes });
    if (opts.map) m.color.set(0xffffff);
    if (opts.emissive) m.emissive = new THREE.Color(opts.emissive);
    return m;
  }
  /* --- builders --- */
  buildGrid(g, L) {
    const d = L.display; const mode = d.mode || (g.role === 'property' ? 'draped' : 'surface');
    let mesh;
    if (mode === 'draped' || mode === 'flat') {
      // property grid: colour by value, heights from topography (draped) or a constant
      const topo = d.topo; const stride = Math.max(1, Math.ceil(Math.sqrt(g.nx * g.ny / 4e5)));
      const heights = new GM.Grid2D({ nx: g.nx, ny: g.ny, x0: g.x0, y0: g.y0, dx: g.dx, dy: g.dy, rotation: g.rotation, name: g.name });
      const lift = d.lift == null ? 2 : d.lift;
      for (let j = 0; j < g.ny; j++) for (let i = 0; i < g.nx; i++) { const v = g.get(i, j); if (v !== v && !d.fillNodata) { heights.set(i, j, NaN); continue; } let z; if (mode === 'draped' && topo) { const [x, y] = g.nodeXY(i, j); z = topo.sample(x, y); if (z !== z) z = d.elevation == null ? 0 : d.elevation; } else z = d.elevation == null ? 0 : d.elevation; heights.set(i, j, z + lift); }
      mesh = heights.toMesh(stride);
      const vals = new Float32Array(mesh.nVertices); // colour attribute per vertex (rebuild mapping)
      let k = 0; for (let j = 0; j < g.ny; j += stride) for (let i = 0; i < g.nx; i += stride) { if (heights.get(i, j) !== heights.get(i, j)) continue; vals[k++] = g.get(i, j); }
      const { colors, range } = this.colorsForAttribute(vals.subarray(0, mesh.nVertices), defaultColormap(g, d), d.range);
      L.range = range;
      const geo = this.geomFromMesh(mesh, colors);
      const m = new THREE.Mesh(geo, this.material([255, 255, 255], { vertexColors: true, transparent: true, opacity: d.opacity == null ? 0.85 : d.opacity }));
      m.material.userData.alwaysTransparent = true; L.group.add(m);
      return;
    }
    const stride = Math.max(1, Math.ceil(Math.sqrt(g.nx * g.ny / 1.2e6)));
    mesh = g.toMesh(stride);
    let colors = null;
    if (d.colorBy === 'elevation' || (g.role === 'topography' && !d.texture && d.colorBy !== 'flat')) { const zs = new Float32Array(mesh.nVertices); for (let i = 0; i < zs.length; i++) zs[i] = mesh.vertices[3 * i + 2]; const r = this.colorsForAttribute(zs, defaultColormap(g, d), d.range); colors = r.colors; L.range = r.range; }
    const geo = this.geomFromMesh(mesh, colors);
    const mat = this.material(g.color, { vertexColors: !!colors, map: d.texture || null, wireframe: !!d.wireframe });
    if (d.texture) { // planar UVs from the grid bounds (texture covers grid bbox exactly)
      const b = g.bounds(); const uv = new Float32Array(mesh.nVertices * 2);
      for (let i = 0; i < mesh.nVertices; i++) { uv[2 * i] = (mesh.vertices[3 * i] - b[0]) / (b[3] - b[0] || 1); uv[2 * i + 1] = (mesh.vertices[3 * i + 1] - b[1]) / (b[4] - b[1] || 1); }
      geo.setAttribute('uv', new THREE.BufferAttribute(uv, 2)); mat.roughness = 0.95;
    }
    const m = new THREE.Mesh(geo, mat); m.userData.kind = 'grid2d'; L.group.add(m);
    if (d.wire) { const w = new THREE.LineSegments(new THREE.WireframeGeometry(geo), new THREE.LineBasicMaterial({ color: 0x334455, transparent: true, opacity: 0.35 })); L.group.add(w); }
  }
  buildMesh(mesh, L) {
    const d = L.display; let colors = null;
    if (d.attribute && mesh.attributes[d.attribute] && mesh.attributes[d.attribute].location === 'vertices') { const r = this.colorsForAttribute(mesh.attributes[d.attribute].values, defaultColormap(mesh, d), d.range); colors = r.colors; L.range = r.range; }
    const geo = this.geomFromMesh(mesh, colors);
    const mat = this.material(mesh.color, { vertexColors: !!colors, wireframe: !!d.wireframe, flat: mesh.role === 'stope' || mesh.role === 'unit' });
    const m = new THREE.Mesh(geo, mat); m.userData.kind = 'mesh'; L.group.add(m);
    if (d.edges) { const e = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 25), new THREE.LineBasicMaterial({ color: 0x111111, transparent: true, opacity: 0.5 })); L.group.add(e); }
    if (d.labels) { const b = mesh.bounds(); if (b) { const sp = this.labelSprite(mesh.name, [(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2], 1); L.group.add(sp); L.labelled = 1; L.labelTotal = 1; } }
  }
  /** One label sprite at a world point, tagged for the declutter pass. */
  labelSprite(text, p, priority = 1, lift = 0) {
    const sp = makeTextSprite(String(text).slice(0, 48)); const s = this.toSceneArr(p[0], p[1], p[2]);
    sp.position.set(s[0], s[1] + lift / this.ve, s[2]); sp.userData.clippable = false; sp.userData.label = true; sp.userData.priority = priority; sp.userData.text = String(text).slice(0, 48);
    return sp;
  }
  /** Labels for a lineset: one per part at its arc-length midpoint, capped at
      200.  A working that is not surveyed says so in the label itself, so a
      name floating over a described adit cannot read as a surveyed one. */
  addLineLabels(ls, L, d) {
    const field = d.labelField || 'name'; const cap = 200; const isW = ls.role === 'workings'; let placed = 0;
    for (let k = 0; k < ls.parts.length && placed < cap; k++) {
      const f = ls.features[k] || {}; const cc = isW ? confClass(f) : 'surveyed';
      let txt = field === 'confidence' ? (isW ? (CONF_CLASSES.find(c => c.key === cc) || {}).label || cc : '') : (f[field] == null ? '' : String(f[field]));
      if (!txt) continue;
      if (isW && cc !== 'surveyed' && field !== 'confidence') txt += ` (${cc})`;
      const pts = ls.partXYZ(k); if (!pts.length) continue;
      const sp = this.labelSprite(txt, polylineMidpoint(pts), isW ? 2 : 1, 8); sp.userData.partIndex = k; L.group.add(sp); placed++;
    }
    L.labelled = placed; L.labelTotal = ls.parts.length;
  }
  buildLines(ls, L) {
    const d = L.display; const ox = this.origin[0], oy = this.origin[1], oz = this.origin[2];
    const byType = ls.role === 'workings';
    const tubes = d.tubes != null ? d.tubes : (ls.role === 'workings' || ls.role === 'drillhole-traces');
    if (d.labels) this.addLineLabels(ls, L, d);
    if (tubes && ls.parts.length <= 4000) {
      // A tube cannot be dashed, so confidence rides on solidity instead: a
      // surveyed working is a solid tube, a described or assumed one is
      // translucent and carries its dashed centreline (added below).
      const TUBE_OPACITY = { surveyed: 1, described: 0.72, assumed: 0.5 };
      const mats = new Map(); // color+confidence key -> {positions, indices}
      for (let k = 0; k < ls.parts.length; k++) {
        const f = ls.features[k] || {}; const color = byType ? ((E.WORKING_TYPES[f.type] || E.WORKING_TYPES.unknown).color) : ls.color;
        const radius = Math.max(0.3, (f.width_m || d.radius || 1.5) / 2) * (d.tubeScale || 1);
        const conf = byType ? confClass(f) : 'surveyed';
        const key = color.join(',') + '|' + conf; if (!mats.has(key)) mats.set(key, { color, conf, pos: [], idx: [] });
        const acc = mats.get(key); const pts = ls.partXYZ(k).map(p => [p[0] - ox, p[2] - oz, -(p[1] - oy)]);
        appendTube(acc, pts, radius, 8, k);
      }
      for (const acc of mats.values()) {
        const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.Float32BufferAttribute(acc.pos, 3)); geo.setIndex(acc.idx); geo.computeVertexNormals();
        const op = TUBE_OPACITY[acc.conf] == null ? 1 : TUBE_OPACITY[acc.conf];
        // the layer's own opacity is applied on top by sync() -> setOpacity()
        const m = new THREE.Mesh(geo, this.material(acc.color, { flat: false, emissive: byType ? 0x221100 : 0x000000, transparent: op < 1, opacity: op }));
        m.userData.partIndex = acc.partIndex; m.userData.faceRanges = acc.faceRanges || []; m.userData.confidence = acc.conf; m.userData.kind = 'tubes'; L.group.add(m);
      }
      if (byType) for (const cls of CONF_CLASSES) {
        if (!cls.dash || !ls.features.some(f => confClass(f) === cls.key)) continue;
        const cseg = this.linesGeometry(ls, null, false, (k, f) => confClass(f) === cls.key);
        if (!cseg.segPart.length) continue;
        // depth test off so the dash reads through its own (translucent) tube
        const cm = new THREE.LineSegments(cseg.geo, this.dashedMaterial(cls, { depthTest: false }));
        cm.renderOrder = 5; cm.userData.segPart = cseg.segPart; cm.userData.confidence = cls.key; L.group.add(cm);
      }
      // also thin lines for picking part indices
      const seg = this.linesGeometry(ls, ls.role === 'workings' ? null : ls.color, true); const lm = new THREE.LineSegments(seg.geo, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.0, depthWrite: false })); lm.userData.segPart = seg.segPart; lm.userData.pickOnly = true; lm.visible = true; L.group.add(lm);
      return;
    }
    const base = ls.role === 'workings' ? null : (d.color || ls.color);
    // Workings carry a per-feature confidence, and a line drawn from a written
    // description must not be readable as a survey.  Surveyed draws solid,
    // described dashed, anything assumed dotted — the same convention the SVG
    // plan/section sheets use.
    if (ls.role === 'workings' && CONF_CLASSES.some(c => c.key !== 'surveyed' && ls.features.some(f => confClass(f) === c.key))) {
      for (const cls of CONF_CLASSES) {
        const seg = this.linesGeometry(ls, base, false, (k, f) => confClass(f) === cls.key);
        if (!seg.segPart.length) continue;
        const m = new THREE.LineSegments(seg.geo, cls.dash ? this.dashedMaterial(cls) : new THREE.LineBasicMaterial({ vertexColors: true, linewidth: 1 }));
        m.userData.segPart = seg.segPart; m.userData.confidence = cls.key; L.group.add(m);
      }
      return;
    }
    const seg = this.linesGeometry(ls, base);
    const m = new THREE.LineSegments(seg.geo, new THREE.LineBasicMaterial({ vertexColors: true, linewidth: 1 })); m.userData.segPart = seg.segPart; L.group.add(m);
  }
  linesGeometry(ls, color, lift = false, keep = null) {
    const ox = this.origin[0], oy = this.origin[1], oz = this.origin[2];
    const partOfVertex = new Int32Array(ls.nVertices).fill(-1); ls.parts.forEach((p, k) => { for (const i of p) partOfVertex[i] = k; });
    // distance travelled along each part, so a dash pattern runs continuously
    // down a polyline instead of restarting at every vertex.
    const distOfVertex = new Float32Array(ls.nVertices); const V = ls.vertices;
    for (const part of ls.parts) {
      let run = 0;
      for (let i = 0; i < part.length; i++) {
        const v = part[i];
        if (i) { const u = part[i - 1]; run += Math.hypot(V[3 * v] - V[3 * u], V[3 * v + 1] - V[3 * u + 1], V[3 * v + 2] - V[3 * u + 2]); }
        distOfVertex[v] = run;
      }
    }
    const all = ls.segments.length / 2; const idx = [];
    for (let s = 0; s < all; s++) { const k = partOfVertex[ls.segments[2 * s]]; if (!keep || keep(k, k >= 0 ? (ls.features[k] || {}) : {})) idx.push(s); }
    const nseg = idx.length;
    const pos = new Float32Array(nseg * 6), col = new Float32Array(nseg * 6), dist = new Float32Array(nseg * 2), segPart = new Int32Array(nseg).fill(-1);
    for (let n = 0; n < nseg; n++) {
      const s = idx[n]; const a = ls.segments[2 * s], b = ls.segments[2 * s + 1]; const k = partOfVertex[a]; segPart[n] = k;
      const f = k >= 0 ? (ls.features[k] || {}) : {}; const c = color || (E.WORKING_TYPES[f.type] || E.WORKING_TYPES.unknown).color;
      for (const [q, i] of [[0, a], [1, b]]) { pos[6 * n + 3 * q] = V[3 * i] - ox; pos[6 * n + 3 * q + 1] = V[3 * i + 2] - oz; pos[6 * n + 3 * q + 2] = -(V[3 * i + 1] - oy); col[6 * n + 3 * q] = c[0] / 255; col[6 * n + 3 * q + 1] = c[1] / 255; col[6 * n + 3 * q + 2] = c[2] / 255; dist[2 * n + q] = distOfVertex[i]; }
    }
    const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.BufferAttribute(pos, 3)); geo.setAttribute('color', new THREE.BufferAttribute(col, 3)); geo.setAttribute('lineDistance', new THREE.BufferAttribute(dist, 1)); geo.computeBoundingSphere();
    return { geo, segPart };
  }
  pointTexture() {
    if (this._pointTexture) return this._pointTexture;
    const c = document.createElement('canvas'); c.width = c.height = 64; const x = c.getContext('2d');
    x.beginPath(); x.arc(32, 32, 26, 0, Math.PI * 2); x.fillStyle = '#fff'; x.fill(); x.lineWidth = 6; x.strokeStyle = 'rgba(0,0,0,.55)'; x.stroke();
    this._pointTexture = new THREE.CanvasTexture(c); return this._pointTexture;
  }
  buildPoints(ps, L) {
    const d = L.display; const n = ps.n; const ox = this.origin[0], oy = this.origin[1], oz = this.origin[2];
    const pos = new Float32Array(n * 3), col = new Float32Array(n * 3);
    let vals = null, range = null;
    if (d.attribute && ps.isNumeric(d.attribute)) { vals = ps.numeric(d.attribute); const r = this.colorsForAttribute(vals, defaultColormap(ps, d), d.range); col.set(r.colors); range = r.range; L.range = range; }
    for (let i = 0; i < n; i++) {
      const z = ps.xyz[3 * i + 2]; pos[3 * i] = ps.xyz[3 * i] - ox; pos[3 * i + 1] = (z === z ? z : 0) - oz + (d.lift || 0); pos[3 * i + 2] = -(ps.xyz[3 * i + 1] - oy);
      if (!vals) { let c = ps.color; if (ps.role === 'claims' && ps.attributes.status) c = String(ps.attributes.status[i]).toUpperCase() === 'ACTIVE' ? [255, 80, 80] : [120, 120, 160]; if (ps.attributes.is_site && +ps.attributes.is_site[i] === 1) c = [45, 212, 191]; col[3 * i] = c[0] / 255; col[3 * i + 1] = c[1] / 255; col[3 * i + 2] = c[2] / 255; }
    }
    const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.BufferAttribute(pos, 3)); geo.setAttribute('color', new THREE.BufferAttribute(col, 3)); geo.computeBoundingSphere();
    const size = d.size || (ps.role === 'mines' ? 14 : ps.role === 'claims' ? 8 : 10);
    const mat = new THREE.PointsMaterial({ size, sizeAttenuation: false, vertexColors: true, map: this.pointTexture(), alphaTest: 0.4, transparent: true, depthWrite: false, clippingPlanes: this.clip.planes });
    const pts = new THREE.Points(geo, mat); pts.userData.kind = 'points'; L.group.add(pts);
    if (d.labels) this.addLabels(ps, L, d.labelField || 'name');
  }
  /* Planar structural measurements as oriented discs, and structural-trend
     ellipsoids as discs scaled by local trend strength.  A disc lies in the
     measured plane; the tick runs down dip; `sides = 3` gives Leapfrog's
     triangle glyph whose apex points down dip. */
  buildStructural(ps, L) {
    const d = L.display, n = ps.n;
    if (!n) return;
    const dips = ps.attributes.dip || [], azs = ps.attributes.dip_azimuth || [], pols = ps.attributes.polarity || [];
    const isTrend = ps.role === 'trend';
    const strength = isTrend && ps.attributes.strength ? ps.numeric('strength') : null;
    const bb = ps.bounds();
    const span = bb ? Math.max(bb[3] - bb[0], bb[4] - bb[1], 50) : 1000;
    const R0 = d.radius || Math.max(8, span * (isTrend ? 0.03 : 0.014));
    const sides = Math.max(3, Math.round(d.sides || 16));
    const by = d.attribute || null;
    let cols = null;
    if (by && by !== 'polarity' && ps.isNumeric(by)) { const r = this.colorsForAttribute(ps.numeric(by), defaultColormap(ps, d), d.range); cols = r.colors; L.range = r.range; }
    else if (by === 'polarity') { cols = new Float32Array(n * 3); for (let i = 0; i < n; i++) { const up = (pols[i] == null ? 1 : +pols[i]) >= 0; cols[3 * i] = up ? 0.41 : 0.85; cols[3 * i + 1] = up ? 0.69 : 0.45; cols[3 * i + 2] = up ? 1 : 0.18; } L.range = null; L.categories = ['right way up', 'overturned']; L.categoryColors = [[104, 176, 255], [217, 112, 45]]; }
    else if (by && ps.attributes[by]) {
      const col = ps.attributes[by]; const cats = [...new Set(col.filter(v => v != null && v !== '').map(String))].sort();
      const pal = CATEGORY_PALETTE; cols = new Float32Array(n * 3);
      for (let i = 0; i < n; i++) { const v = col[i]; const k = (v == null || v === '') ? -1 : cats.indexOf(String(v)); const c = k < 0 ? [110, 118, 128] : pal[k % pal.length]; cols[3 * i] = c[0] / 255; cols[3 * i + 1] = c[1] / 255; cols[3 * i + 2] = c[2] / 255; }
      L.categories = cats; L.categoryColors = cats.map((_, k) => pal[k % pal.length]); L.range = null;
    }
    let smin = Infinity, smax = -Infinity;
    if (strength) for (const v of strength) { if (v !== v) continue; if (v < smin) smin = v; if (v > smax) smax = v; }
    const pos = [], col = [], idx = [], tick = [], tcol = [];
    const base = ps.color.map(c => c / 255);
    let vbase = 0, drawn = 0;
    const cap = d.maxGlyphs || 6000;
    const stride = Math.max(1, Math.ceil(n / cap));
    for (let i = 0; i < n; i += stride) {
      const dip = +dips[i], az = +azs[i];
      if (dip !== dip || az !== az) continue;
      const x = ps.xyz[3 * i], y = ps.xyz[3 * i + 1], z = ps.xyz[3 * i + 2];
      if (x !== x || y !== y) continue;
      const a = az * DEG, dd = dip * DEG, cd = Math.cos(dd), sd = Math.sin(dd);
      const S = [-Math.cos(a), Math.sin(a), 0];                       // strike
      const D = [Math.sin(a) * cd, Math.cos(a) * cd, -sd];            // down dip
      const P = [Math.sin(a) * sd, Math.cos(a) * sd, cd];             // pole (up)
      let R = R0;
      if (strength && smax > smin) R = R0 * (0.35 + 0.65 * ((strength[i] - smin) / (smax - smin)));
      const c = cols ? [cols[3 * i], cols[3 * i + 1], cols[3 * i + 2]] : base;
      const ctr = this.toSceneArr(x, y, z);
      pos.push(ctr[0], ctr[1], ctr[2]); col.push(c[0], c[1], c[2]);
      const c0 = vbase; vbase++;
      for (let k = 0; k < sides; k++) {
        const t = (k / sides) * 2 * Math.PI + (sides === 3 ? Math.PI / 2 : 0);
        const ct = Math.cos(t), st = Math.sin(t);
        const px = x + R * (ct * S[0] + st * D[0]), py = y + R * (ct * S[1] + st * D[1]), pz = z + R * (ct * S[2] + st * D[2]);
        const v = this.toSceneArr(px, py, pz);
        pos.push(v[0], v[1], v[2]); col.push(c[0], c[1], c[2]); vbase++;
      }
      for (let k = 0; k < sides; k++) idx.push(c0, c0 + 1 + k, c0 + 1 + ((k + 1) % sides));
      if (d.tick !== false && !isTrend) {
        const e = this.toSceneArr(x + R * D[0] * 1.35, y + R * D[1] * 1.35, z + R * D[2] * 1.35);
        tick.push(ctr[0], ctr[1], ctr[2], e[0], e[1], e[2]);
        for (let q = 0; q < 2; q++) tcol.push(c[0] * 0.6, c[1] * 0.6, c[2] * 0.6);
        const pol = (pols[i] == null ? 1 : +pols[i]) >= 0 ? 1 : -1;
        const u = this.toSceneArr(x + R * P[0] * 0.55 * pol, y + R * P[1] * 0.55 * pol, z + R * P[2] * 0.55 * pol);
        tick.push(ctr[0], ctr[1], ctr[2], u[0], u[1], u[2]);
        for (let q = 0; q < 2; q++) { if (pol > 0) tcol.push(0.55, 0.83, 1); else tcol.push(1, 0.62, 0.28); }
      }
      drawn++;
    }
    L.drawn = drawn; L.totalGlyphs = n;
    if (!pos.length) return;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
    g.setIndex(idx);
    g.computeVertexNormals(); g.computeBoundingSphere();
    const mat = new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide, roughness: 0.6, metalness: 0.05, flatShading: true, transparent: d.opacity != null && d.opacity < 1, opacity: d.opacity == null ? 1 : d.opacity, clippingPlanes: this.clip.planes });
    const mesh = new THREE.Mesh(g, mat); mesh.userData.kind = 'structural'; L.group.add(mesh);
    if (tick.length) {
      const lg = new THREE.BufferGeometry();
      lg.setAttribute('position', new THREE.Float32BufferAttribute(tick, 3));
      lg.setAttribute('color', new THREE.Float32BufferAttribute(tcol, 3));
      L.group.add(new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ vertexColors: true, clippingPlanes: this.clip.planes })));
    }
    // an invisible point cloud so hovering and picking still report the row
    const ppos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) { const v = this.toSceneArr(ps.xyz[3 * i], ps.xyz[3 * i + 1], ps.xyz[3 * i + 2]); ppos[3 * i] = v[0]; ppos[3 * i + 1] = v[1]; ppos[3 * i + 2] = v[2]; }
    const pg = new THREE.BufferGeometry(); pg.setAttribute('position', new THREE.BufferAttribute(ppos, 3)); pg.computeBoundingSphere();
    const pts = new THREE.Points(pg, new THREE.PointsMaterial({ size: 6, sizeAttenuation: false, transparent: true, opacity: 0.01, depthWrite: false, clippingPlanes: this.clip.planes }));
    pts.userData.kind = 'points'; L.group.add(pts);
    if (d.labels) this.addLabels(ps, L, d.labelField || 'dip');
  }
  addLabels(ps, L, field) {
    const col = ps.attributes[field]; if (!col) return; const n = Math.min(ps.n, 300);
    // declutter priority: the site itself, then anything carrying a grade
    const site = ps.attributes.is_site || null; const grades = Object.keys(ps.attributes).filter(k => GRADE_COL.test(k)).map(k => ps.attributes[k]);
    let placed = 0;
    for (let i = 0; i < n; i++) {
      const txt = col[i]; if (txt == null || txt === '') continue;
      const pr = site && +site[i] === 1 ? 3 : grades.some(g => g[i] != null && g[i] !== '' && +g[i] === +g[i]) ? 2 : 1;
      const z = ps.xyz[3 * i + 2]; const sp = this.labelSprite(String(txt).slice(0, 40), [ps.xyz[3 * i], ps.xyz[3 * i + 1], z === z ? z : 0], pr, 12); sp.userData.index = i; L.group.add(sp); placed++;
    }
    L.labelled = placed; L.labelTotal = ps.n;
  }
  buildBlocks(bm, L) {
    const d = L.display; const attr = d.attribute || Object.keys(bm.attributes).find(k => bm.attributes[k].type === 'number') || Object.keys(bm.attributes)[0];
    if (!attr) { this.buildBlockOutline(bm, L); return; }
    const a = bm.attributes[attr]; const vals = a.values; const n = bm.n; const cat = a.type !== 'number';
    let lo = Infinity, hi = -Infinity; const catIndex = new Map();
    if (cat) { for (const v of vals) if (v != null && v !== '') catIndex.set(v, catIndex.has(v) ? catIndex.get(v) : catIndex.size); }
    else for (const v of vals) { if (v !== v) continue; if (v < lo) lo = v; if (v > hi) hi = v; }
    const range = d.range || [lo, hi]; L.range = cat ? null : range; L.categories = cat ? [...catIndex.keys()] : null;
    const cmap = defaultColormap(bm, d, cat);
    const cut = d.cutoff != null ? d.cutoff : -Infinity, cutHi = d.cutoffHi != null ? d.cutoffHi : Infinity;
    const keep = []; for (let i = 0; i < n; i++) { const v = vals[i]; if (cat) { if (v == null || v === '' ) continue; if (d.category && v !== d.category) continue; } else { if (v !== v || v < cut || v > cutHi) continue; } keep.push(i); }
    const maxInst = 400000; const stride = keep.length > maxInst ? Math.ceil(keep.length / maxInst) : 1;
    const shown = stride > 1 ? keep.filter((_, q) => q % stride === 0) : keep; L.shownBlocks = shown.length; L.totalBlocks = keep.length;
    const geo = new THREE.BoxGeometry(bm.blockSize[0] * (d.shrink || 0.92), bm.blockSize[2] * (d.shrink || 0.92), bm.blockSize[1] * (d.shrink || 0.92));
    const mat = new THREE.MeshStandardMaterial({ roughness: 0.8, metalness: 0.05, transparent: d.opacity != null && d.opacity < 1, opacity: d.opacity == null ? 1 : d.opacity, clippingPlanes: this.clip.planes });
    const inst = new THREE.InstancedMesh(geo, mat, shown.length); const m4 = new THREE.Matrix4(); const c3 = new THREE.Color();
    const az = bm.azimuth * DEG; const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), az); const s1 = new THREE.Vector3(1, 1, 1);
    for (let q2 = 0; q2 < shown.length; q2++) {
      const idx = shown[q2]; const [i, j, k] = bm.ijk(idx); const [x, y, z] = bm.centroid(i, j, k);
      m4.compose(new THREE.Vector3(x - this.origin[0], z - this.origin[2], -(y - this.origin[1])), q, s1); inst.setMatrixAt(q2, m4);
      let c; if (cat) c = GM.colormap(cmap, catIndex.size > 1 ? catIndex.get(vals[idx]) / (catIndex.size - 1) : 0.5); else c = GM.colormap(cmap, range[1] > range[0] ? (vals[idx] - range[0]) / (range[1] - range[0]) : 0.5);
      inst.setColorAt(q2, c3.setRGB(c[0] / 255, c[1] / 255, c[2] / 255));
    }
    inst.instanceMatrix.needsUpdate = true; if (inst.instanceColor) inst.instanceColor.needsUpdate = true; inst.userData.blockIds = shown; inst.userData.kind = 'blocks'; inst.userData.attribute = attr; L.group.add(inst);
    this.buildBlockOutline(bm, L);
  }
  buildBlockOutline(bm, L) {
    const b = bm.bounds(); const box = new THREE.Box3(this.toScene(b[0], b[1], b[2]), this.toScene(b[3], b[4], b[5])); box.min.z = -(b[4] - this.origin[1]); box.max.z = -(b[1] - this.origin[1]);
    const h = new THREE.Box3Helper(box, 0x5a6a7a); h.userData.clippable = false; h.userData.pickOnly = false; h.raycast = () => { }; L.group.add(h);
  }
  buildDrillholes(dh, L) {
    const d = L.display; const traces = dh.desurvey(2); const ls = new GM.LineSet({ name: dh.name, role: 'drillhole-traces', color: dh.color });
    for (const [hole, pts] of Object.entries(traces)) ls.addPolyline(pts.map(p => [p[1], p[2], p[3]]), { hole, width_m: d.radius ? d.radius * 2 : 2 });
    this.buildLines(ls, { display: Object.assign({ tubes: true, radius: 1.0 }, d, { labels: false }), group: L.group, obj: ls });
    // colour-coded intervals for a chosen table/column
    if (d.table && d.column && dh.intervals[d.table]) {
      const rows = dh.intervals[d.table]; const vals = rows.map(r => +r[d.column]); const r = this.colorsForAttribute(Float64Array.from(vals), defaultColormap(dh, d), d.range); L.range = r.range;
      const acc = { color: [255, 255, 255], pos: [], idx: [], colors: [] };
      rows.forEach((row, q) => { const f = +row.from, t = +row.to; if (!(f === f && t === t) || vals[q] !== vals[q]) return; const pts = []; const steps = Math.max(1, Math.ceil((t - f) / 2)); for (let s = 0; s <= steps; s++) { const p = dh.locate(row.hole, f + (t - f) * s / steps, traces); if (p) pts.push([p[0] - this.origin[0], p[2] - this.origin[2], -(p[1] - this.origin[1])]); } if (pts.length < 2) return; const before = acc.pos.length / 3; appendTube(acc, pts, (d.radius || 1.0) * 1.8, 8, q); const after = acc.pos.length / 3; for (let v = before; v < after; v++) acc.colors.push(r.colors[3 * q], r.colors[3 * q + 1], r.colors[3 * q + 2]); });
      if (acc.pos.length) { const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.Float32BufferAttribute(acc.pos, 3)); geo.setAttribute('color', new THREE.Float32BufferAttribute(acc.colors, 3)); geo.setIndex(acc.idx); geo.computeVertexNormals(); const im = new THREE.Mesh(geo, this.material([255, 255, 255], { vertexColors: true })); im.userData.faceRanges = acc.faceRanges || []; im.userData.kind = 'intervals'; im.userData.table = d.table; L.group.add(im); }
    }
    // collars
    const ps = new GM.PointSet({ name: 'collars', role: 'collars', color: dh.color }); for (const c of dh.collars) ps.add(+c.x, +c.y, +c.z, { name: c.hole });
    this.buildPoints(ps, { display: { size: 9, labels: d.labels !== false, labelField: 'name' }, group: L.group, obj: ps });
  }
  buildImage(ip, L) {
    const d = L.display; const corners = ip.corners(); // TL TR BR BL world
    const pos = new Float32Array(12); corners.forEach((c, i) => { const s = this.toSceneArr(c[0], c[1], c[2] === c[2] ? c[2] : (d.elevation || 0)); pos[3 * i] = s[0]; pos[3 * i + 1] = s[1]; pos[3 * i + 2] = s[2]; });
    const geo = new THREE.BufferGeometry(); geo.setAttribute('position', new THREE.BufferAttribute(pos, 3)); geo.setAttribute('uv', new THREE.Float32BufferAttribute([0, 1, 1, 1, 1, 0, 0, 0], 2)); geo.setIndex([0, 2, 1, 0, 3, 2]); geo.computeVertexNormals();
    const mat = new THREE.MeshBasicMaterial({ side: THREE.DoubleSide, transparent: true, opacity: 1, clippingPlanes: this.clip.planes, color: 0xffffff });
    mat.userData.alwaysTransparent = true; mat.userData.keepDepth = true;
    const mesh = new THREE.Mesh(geo, mat); mesh.userData.kind = 'imageplane'; L.group.add(mesh);
    if (ip.image) { new THREE.TextureLoader().load(ip.image, tex => { tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 4; mat.map = tex; mat.needsUpdate = true; this.invalidate(); }, undefined, () => { mat.color.set(0x556677); this.invalidate(); }); }
    const edge = new THREE.LineLoop(new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(pos.slice(), 3)), new THREE.LineBasicMaterial({ color: 0xffd27a, transparent: true, opacity: 0.7 })); edge.raycast = () => { }; L.group.add(edge);
  }
  buildSection(sec, L) {
    const d = L.display; if (!sec.start || !sec.end) return;
    const zmin = sec.zMin == null ? -500 : sec.zMin, zmax = sec.zMax == null ? 2000 : sec.zMax;
    const c = [[sec.start[0], sec.start[1], zmax], [sec.end[0], sec.end[1], zmax], [sec.end[0], sec.end[1], zmin], [sec.start[0], sec.start[1], zmin]];
    const pos = new Float32Array(12); c.forEach((p, i) => { const s = this.toSceneArr(p[0], p[1], p[2]); pos[3 * i] = s[0]; pos[3 * i + 1] = s[1]; pos[3 * i + 2] = s[2]; });
    const geo = new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(pos, 3)); geo.setIndex([0, 2, 1, 0, 3, 2]);
    const mat = new THREE.MeshBasicMaterial({ color: 0x2dd4bf, transparent: true, opacity: d.planeOpacity == null ? 0.08 : d.planeOpacity, side: THREE.DoubleSide, depthWrite: false }); mat.userData.alwaysTransparent = true;
    // The quad is display only: it must not intercept hovers and clicks meant
    // for the terrain or the workings behind it (the section tool picks the
    // ground, never the plane).
    const m = new THREE.Mesh(geo, mat); m.userData.clippable = false; m.userData.kind = 'section'; m.raycast = () => { }; L.group.add(m);
    const edge = new THREE.LineLoop(new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(pos.slice(), 3)), new THREE.LineBasicMaterial({ color: 0x2dd4bf, transparent: true, opacity: 0.9 })); edge.userData.clippable = false; edge.raycast = () => { }; L.group.add(edge);
    const lbl = this.labelSprite(sec.name || 'section', [(c[0][0] + c[1][0]) / 2, (c[0][1] + c[1][1]) / 2, zmax], 1, 4); L.group.add(lbl);
    // products (intersection lines, ribbons) are added by the tools into L.products group
    L.products = new THREE.Group(); L.products.userData.clippable = false; L.group.add(L.products);
  }
  /* overlays for tools */
  clearOverlay() { for (const c of [...this.overlay.children]) { this.overlay.remove(c); disposeGroup(c); } this.invalidate(); }
  overlayPolyline(pts, color = 0xffd27a, dashed = false) { const pos = []; for (const p of pts) pos.push(...this.toSceneArr(p[0], p[1], p[2])); const g = new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); const m = dashed ? new THREE.LineDashedMaterial({ color, dashSize: 20, gapSize: 10, depthTest: false }) : new THREE.LineBasicMaterial({ color, depthTest: false }); const l = new THREE.Line(g, m); if (dashed) l.computeLineDistances(); l.renderOrder = 999; l.userData.clippable = false; this.overlay.add(l); this.invalidate(); return l; }
  overlayMarker(p, color = 0xffd27a, size = 10) { const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: this.pointTexture(), color, depthTest: false, sizeAttenuation: false })); sp.scale.set(size / 300, size / 300, 1); const s = this.toSceneArr(p[0], p[1], p[2]); sp.position.set(s[0], s[1], s[2]); sp.renderOrder = 1000; sp.userData.clippable = false; this.overlay.add(sp); this.invalidate(); return sp; }
  screenshot() { this.renderer.render(this.scene, this.camera); return this.canvas.toDataURL('image/png'); }
  northArrow(size = 1) { /* drawn in the HTML overlay by the viewer using camera azimuth */ const v = new THREE.Vector3(0, 0, -1); const cam = this.camera.getWorldDirection(new THREE.Vector3()); return Math.atan2(cam.x, -cam.z); }
}

function appendTube(acc, pts, radius, sides, partIndex) {
  // sweep a ring along the polyline (per-segment cylinders joined at vertices);
  // the triangle range each part occupies is recorded so a raycast face
  // resolves back to the part (`userData.faceRanges` on the batched mesh)
  const base = acc.pos.length / 3, face0 = acc.idx.length / 3;
  const n = pts.length; if (n < 2) return;
  const frames = [];
  for (let i = 0; i < n; i++) {
    const a = pts[Math.max(0, i - 1)], b = pts[Math.min(n - 1, i + 1)];
    const t = new THREE.Vector3(b[0] - a[0], b[1] - a[1], b[2] - a[2]); if (t.lengthSq() < 1e-12) t.set(0, 1, 0); t.normalize();
    let up = Math.abs(t.y) > 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0);
    const nrm = new THREE.Vector3().crossVectors(t, up).normalize(), bin = new THREE.Vector3().crossVectors(t, nrm).normalize();
    frames.push([nrm, bin]);
  }
  for (let i = 0; i < n; i++) { const [nrm, bin] = frames[i]; for (let s = 0; s < sides; s++) { const a = s / sides * Math.PI * 2; acc.pos.push(pts[i][0] + radius * (Math.cos(a) * nrm.x + Math.sin(a) * bin.x), pts[i][1] + radius * (Math.cos(a) * nrm.y + Math.sin(a) * bin.y), pts[i][2] + radius * (Math.cos(a) * nrm.z + Math.sin(a) * bin.z)); } }
  for (let i = 0; i < n - 1; i++) for (let s = 0; s < sides; s++) { const s2 = (s + 1) % sides; const a = base + i * sides + s, b = base + i * sides + s2, c = base + (i + 1) * sides + s, d = base + (i + 1) * sides + s2; acc.idx.push(a, c, b, b, c, d); }
  // end caps
  for (const [ring, flip] of [[0, true], [n - 1, false]]) { const center = acc.pos.length / 3; acc.pos.push(pts[ring][0], pts[ring][1], pts[ring][2]); for (let s = 0; s < sides; s++) { const a = base + ring * sides + s, b = base + ring * sides + (s + 1) % sides; if (flip) acc.idx.push(center, b, a); else acc.idx.push(center, a, b); } }
  if (!acc.partIndex) acc.partIndex = [];
  acc.partIndex.push(partIndex);
  if (!acc.faceRanges) acc.faceRanges = [];
  acc.faceRanges.push([face0, acc.idx.length / 3, partIndex]);
}
/** Point half-way along a polyline by arc length. */
function polylineMidpoint(pts) {
  if (pts.length === 1) return pts[0].slice();
  let total = 0; const seg = [];
  for (let i = 1; i < pts.length; i++) { const l = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1], pts[i][2] - pts[i - 1][2]); seg.push(l); total += l; }
  let run = 0;
  for (let i = 0; i < seg.length; i++) {
    if (run + seg[i] >= total / 2) { const t = seg[i] > 0 ? (total / 2 - run) / seg[i] : 0; const a = pts[i], b = pts[i + 1]; return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
    run += seg[i];
  }
  return pts[pts.length - 1].slice();
}
/* Every material remembers the opacity it was built with; the layer slider
   multiplies it rather than overwriting it, so a described tube (0.72) or an
   assumed one (0.5) never brightens into a surveyed-looking solid. */
function stampOpacity(m) { if (m.userData.baseOpacity == null) { m.userData.baseOpacity = m.opacity == null ? 1 : m.opacity; m.userData.baseTransparent = !!m.transparent; m.userData.baseDepthWrite = m.depthWrite !== false; } return m; }
function applyOpacity(m, a) { stampOpacity(m); const u = m.userData; const op = u.baseOpacity * a; m.opacity = op; m.transparent = op < 1 || u.baseTransparent || !!u.alwaysTransparent; m.depthWrite = u.baseDepthWrite && !(a < 1 && !u.keepDepth); m.needsUpdate = true; }

export function makeTextSprite(text, opts = {}) {
  const c = document.createElement('canvas'); const x = c.getContext('2d'); const font = `${opts.size || 22}px ui-monospace, Menlo, monospace`; x.font = font;
  const w = Math.ceil(x.measureText(text).width) + 16, h = (opts.size || 22) + 12; c.width = w; c.height = h; x.font = font; x.fillStyle = 'rgba(8,10,14,0.72)'; x.fillRect(0, 0, w, h); x.fillStyle = opts.color || '#e6edf3'; x.textBaseline = 'middle'; x.fillText(text, 8, h / 2);
  const tex = new THREE.CanvasTexture(c); const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, sizeAttenuation: false, transparent: true })); sp.scale.set(w / 2200, h / 2200, 1); sp.center.set(0.5, 0); sp.renderOrder = 990; return sp;
}

export function disposeGroup(g) { g.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) { const ms = Array.isArray(o.material) ? o.material : [o.material]; for (const m of ms) { if (m.map && m.map !== undefined && m.map.dispose && !m.map.userData?.shared) m.map.dispose(); m.dispose(); } } }); }

export function canvasTexture(canvas) { const t = new THREE.CanvasTexture(canvas); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8; t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping; return t; }
export { THREE };
