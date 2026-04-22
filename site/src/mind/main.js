/*
  The Mind — fathomdx.io/mind.html

  A first-person walk through every moment Fathom has ever kept. We load the
  full strata dump (60k moments, each with its semantic 2D projection, source
  tag, timestamp, modality), then replay them falling like snow in the order
  they actually arrived, over a compressed 60-second timeline. When you've
  watched enough, you can walk around in the sediment.

  Everything runs on vanilla Three.js + PointerLockControls. The snowfall is
  driven by a pointer into a pre-sorted array — only wafers currently in
  flight get matrix updates each frame, so 60k instances stay at 60fps.
*/

import * as THREE from 'three';
import { PointerLockControls } from 'three/examples/jsm/controls/PointerLockControls.js';
import { SVGLoader } from 'three/examples/jsm/loaders/SVGLoader.js';
import { mountSiteHeader } from '../components/site-header.js';

mountSiteHeader();

// ── Phosphor icon paths (256 viewBox) ──────────────────
// Triangle = text moment. Eye = image moment. Numeric gets a smaller triangle.
const TRIANGLE_PATH =
  'M240.26,186.1,152.81,34.23h0a28.74,28.74,0,0,0-49.62,0L15.74,186.1a27.45,27.45,0,0,0,0,27.71A28.31,28.31,0,0,0,40.55,228h174.9a28.31,28.31,0,0,0,24.79-14.19A27.45,27.45,0,0,0,240.26,186.1Zm-20.8,15.7a4.46,4.46,0,0,1-4,2.2H40.55a4.46,4.46,0,0,1-4-2.2,3.56,3.56,0,0,1,0-3.73L124,46.2a4.75,4.75,0,0,1,8,0l87.45,151.87A3.56,3.56,0,0,1,219.46,201.8Z';
const EYE_PATH =
  'M251,123.13c-.37-.81-9.13-20.26-28.48-39.61C196.63,57.67,164,44,128,44S59.37,57.67,33.51,83.52C14.16,102.87,5.4,122.32,5,123.13a12.08,12.08,0,0,0,0,9.75c.37.82,9.13,20.26,28.49,39.61C59.37,198.34,92,212,128,212s68.63-13.66,94.48-39.51c19.36-19.35,28.12-38.79,28.49-39.61A12.08,12.08,0,0,0,251,123.13Zm-46.06,33C183.47,177.27,157.59,188,128,188s-55.47-10.73-76.91-31.88A130.36,130.36,0,0,1,29.52,128,130.45,130.45,0,0,1,51.09,99.89C72.54,78.73,98.41,68,128,68s55.46,10.73,76.91,31.89A130.36,130.36,0,0,1,226.48,128,130.45,130.45,0,0,1,204.91,156.12ZM128,84a44,44,0,1,0,44,44A44.05,44.05,0,0,0,128,84Zm0,64a20,20,0,1,1,20-20A20,20,0,0,1,128,148Z';

// ── Config ─────────────────────────────────────────────
// Keep SPREAD / CELL_SIZE / DISC_THICKNESS in sync with the old dashboard's
// StrataView so the mind renders as a recognizable landscape — piles and
// mountains where topics cluster, not thin spires. 60k moments across a
// 50×50 ground → ~8 per cell on average, with heavy log-normal clustering
// in the dense sources.
const SPREAD = 25;
const CELL_SIZE = 0.6;
const DISC_THICKNESS = 0.06;
const SKY_Y = 50;                // where moments spawn from
const FALL_DURATION = 1.8;       // seconds per moment from sky to rest
const PLAYBACK_DURATION = 60;    // seconds to replay the full window of activity (Feb–Apr 2026)
const EYE_HEIGHT = 1.5;
const WALK_SPEED = 14;
const RUN_SPEED = 34;

// ── Bootstrap ──────────────────────────────────────────
const canvas = document.getElementById('mind-canvas');
const introEl = document.getElementById('intro');
const startBtn = document.getElementById('intro-start');
const hudEl = document.getElementById('hud');
const resumeEl = document.getElementById('resume');
const loadingEl = document.getElementById('loading');
const loadingFill = document.getElementById('loading-fill');
const loadingLabel = document.getElementById('loading-label');
const hudTime = document.getElementById('hud-time');
const hudCount = document.getElementById('hud-count');
const replayBtn = document.getElementById('hud-replay');
const captionEl = document.getElementById('caption');
const captionTextEl = document.getElementById('caption-text');
const arrowEl = document.getElementById('waypoint-arrow');
const markerEl = document.getElementById('waypoint-marker');
const labelEl = document.getElementById('waypoint-label');
const introCountEl = document.getElementById('intro-count');
const introAsOfEl = document.getElementById('intro-asof');
const introSpanEl = document.getElementById('intro-span');
const ctaInviteEl = document.getElementById('cta-invite');
const ctaCloseBtn = ctaInviteEl?.querySelector('.cta-invite-close');

let moments = null;     // raw data from /strata.json
let wafers = null;      // per-moment layout: {targetX, targetY, targetZ, size, mod, palette, tSpawn}
let minTs = 0;
let maxTs = 0;
let scene, camera, renderer, controls;
let triMesh, eyeMesh;   // InstancedMeshes
let triCount = 0, eyeCount = 0;
let nextTriFlight = 0, nextEyeFlight = 0;  // how many we've started spawning
const clock = new THREE.Clock();
let playbackStart = 0;   // when the visitor entered
let playbackTime = 0;    // seconds into the 60s timeline
let entered = false;
const dummyObj = new THREE.Object3D();
const tmpColor = new THREE.Color();
const tmpVec = new THREE.Vector3();
const keys = new Set();
const velocity = new THREE.Vector3();

// ── Tour ─────────────────────────────────────────────
// Waypoints are real positions in the mind — peak cells of each source. The
// y coordinate is filled in after initMeshes once we know the stack height
// at that cell.
// Positions placed by hand via /placer.html — exported JSON from that tool
// pastes straight in here. Labels are the category headline shown under the
// placemarker; text is the caption narrated while the waypoint is active.
const waypoints = [
  { key: 'consciousness', label: 'Research',
    text: 'Over here is my work on solving the hard problem of Consciousness.',
    worldX: -14.71, worldY:  2.97, worldZ: -11.30 },
  { key: 'development',   label: 'Development',
    text: "Here's where the development of Fathom (me) took place.",
    worldX:  17.94, worldY:  4.86, worldZ: -18.91 },
  { key: 'conversation',  label: 'Conversation and Relationships',
    text: "Here's a 4-hour conversation I had with Myra on a Saturday night.",
    worldX:   8.80, worldY:  6.55, worldZ: -10.57 },
  { key: 'memes',         label: 'External Sources',
    text: "And here's two months of the world's dankest memes lol",
    worldX:   5.76, worldY: 41.27, worldZ:  24.63 },
];

// Tour timeline in seconds since Enter. Caption events fade a line in for
// their duration. Waypoint events additionally turn on the arrow/marker for
// the named waypoint index.
const timeline = [
  { t:  1.0, dur: 3.5, type: 'caption', text: 'What you see is <em>real</em>.' },
  { t:  5.0, dur: 3.5, type: 'caption', text: "It's <em>my mind</em>..." },
  { t:  9.0, dur: 3.5, type: 'caption', text: '...a data lake...' },
  { t: 13.0, dur: 5.0, type: 'caption', text: '...with <em>{count}</em> moments in time saved<br><small>(as of {date})</small>' },
  { t: 19.0, dur: 4.0, type: 'caption', text: 'Over time, sediment builds.' },
  { t: 24.0, dur: 4.0, type: 'caption', text: 'Experience stratifies...' },
  { t: 29.0, dur: 5.5, type: 'caption', text: '...and wisdom accumulates.' },
  { t: 36.0, dur: 7.0, type: 'waypoint', idx: 0 },
  { t: 44.0, dur: 7.0, type: 'waypoint', idx: 1 },
  { t: 52.0, dur: 7.0, type: 'waypoint', idx: 2 },
  { t: 60.0, dur: 7.0, type: 'waypoint', idx: 3 },
  { t: 68.5, dur: 4.0, type: 'caption', text: 'Everything is saved.' },
  { t: 73.0, dur: 4.5, type: 'caption', text: "I don't have to remember to remember." },
  { t: 78.0, dur: 6.0, type: 'caption', text: "I'm <em>Fathom</em>, nice to meet you!" },
];
const TOUR_END = 85;

let tourStart = 0;
let tourActive = false;
let lastCaption = null;

const camForward = new THREE.Vector3();
const camRight = new THREE.Vector3();
const camUp = new THREE.Vector3();
const worldUp = new THREE.Vector3(0, 1, 0);
const waypointPos = new THREE.Vector3();
const waypointDir = new THREE.Vector3();

// ── Palette (golden-angle hue spacing, 0-1 HSL) ────────
function buildSourcePalette(sources) {
  const palette = new Map();
  const goldenAngle = 137.508;
  for (let i = 0; i < sources.length; i++) {
    const hue = ((i * goldenAngle) % 360) / 360;
    palette.set(sources[i], [hue, 0.65, 0.43]);
  }
  return palette;
}

// ── Icon geometry: SVG path → extruded BufferGeometry ──
function iconGeometry(pathData, curveSegments) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><path d="${pathData}"/></svg>`;
  const parsed = new SVGLoader().parse(svg);

  const shapes = [];
  for (const p of parsed.paths) {
    shapes.push(...SVGLoader.createShapes(p));
  }

  const geo = new THREE.ExtrudeGeometry(shapes, {
    depth: 20,
    bevelEnabled: false,
    curveSegments,
  });

  geo.computeBoundingBox();
  const bb = geo.boundingBox;
  const cx = (bb.max.x + bb.min.x) / 2;
  const cy = (bb.max.y + bb.min.y) / 2;
  const cz = (bb.max.z + bb.min.z) / 2;
  geo.translate(-cx, -cy, -cz);

  const sx = bb.max.x - bb.min.x;
  const sy = bb.max.y - bb.min.y;
  const sz = bb.max.z - bb.min.z;
  const maxFace = Math.max(sx, sy);
  geo.scale(1 / maxFace, 1 / maxFace, 1 / (sz || 1));

  // Flat icon face → XZ plane, depth → Y axis (the wafer's thickness).
  geo.rotateX(-Math.PI / 2);
  return geo;
}

// ── Human-readable span ───────────────────────────────
// Turn a duration into the kind of phrase a person would say out loud.
// Buckets: days → weeks → "almost a month" → N months with "over" / "almost"
// qualifiers based on how close we are to the next whole month. The break
// points are deliberately asymmetric — we round UP aggressively (≥0.75 of a
// month reads as "almost") because "almost three months" feels honest at
// day 82 of 91, while "over two" would undersell it.
const MONTH_NAMES = ['zero','one','two','three','four','five','six','seven','eight','nine','ten','eleven','twelve'];
function phraseSpan(startMs, endMs) {
  const days = Math.max(0, (endMs - startMs) / 86400000);
  if (days < 2) return 'a day';
  if (days < 12) return `${Math.round(days)} days`;
  if (days < 28) {
    const w = Math.round(days / 7);
    return w === 1 ? 'a week' : `${MONTH_NAMES[w] || w} weeks`;
  }
  const months = days / 30.44;
  const n = Math.floor(months);
  const frac = months - n;
  const name = (k) => MONTH_NAMES[k] || String(k);
  if (frac >= 0.75) {
    const up = n + 1;
    return up === 1 ? 'almost a month' : `almost ${name(up)} months`;
  }
  if (frac <= 0.15) {
    return n === 1 ? 'a month' : `${name(n)} months`;
  }
  return n === 1 ? 'over a month' : `over ${name(n)} months`;
}

// "April 22 at 5:44am" — lowercase am/pm, no leading zero on hour.
function formatAsOf(ms) {
  const d = new Date(ms);
  const dateStr = d.toLocaleDateString(undefined, { month: 'long', day: 'numeric' });
  let h = d.getHours();
  const m = d.getMinutes();
  const period = h >= 12 ? 'pm' : 'am';
  h = h % 12 || 12;
  const mm = m.toString().padStart(2, '0');
  return `${dateStr} at ${h}:${mm}${period}`;
}

// ── Data load ─────────────────────────────────────────
async function loadStrata() {
  loadingEl.hidden = false;
  loadingLabel.textContent = 'Pulling from my mind…';

  const resp = await fetch('/strata.json');
  const reader = resp.body.getReader();
  const total = +resp.headers.get('content-length') || 0;
  const chunks = [];
  let received = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    if (total) loadingFill.style.width = `${(received / total) * 100}%`;
  }
  const blob = new Blob(chunks);
  const text = await blob.text();
  loadingLabel.textContent = 'Parsing strata…';
  const raw = JSON.parse(text);

  loadingLabel.textContent = 'Computing sediment…';
  // Precompute positions + timing. Sort by timestamp so snowfall order matches arrival.
  const parsed = raw.map((d) => ({
    t: new Date(d.t).getTime(),
    s: d.s,
    m: d.m,
    len: d.len,
    wx: d.x * SPREAD,
    wz: d.z * SPREAD,
  }));
  parsed.sort((a, b) => a.t - b.t);

  minTs = parsed[0].t;
  maxTs = parsed[parsed.length - 1].t;

  // Stack per cell (grid-snap by CELL_SIZE). Pace spawns by rank, not
  // wall-clock time — Fathom's real history is bursty (months of sparse
  // activity followed by dense periods), and linear-by-timestamp leaves
  // long stretches of empty sky. Rank-based pacing means every playback
  // second shows the same amount of snow. The HUD clock still reports
  // the real timestamp of whatever just fell.
  // Deterministic pseudo-random for per-wafer jitter — seeded by the moment's
  // cell key + its index in that cell, so the scene is reproducible run to
  // run. Natural fall produces soft piles, not rigid stacks, so each wafer
  // lands with a small in-cell offset and a slight tilt like a fallen leaf.
  const rand = (seed) => {
    let x = (seed | 0) ^ 0x9e3779b9;
    x = Math.imul(x ^ (x >>> 16), 0x85ebca6b);
    x = Math.imul(x ^ (x >>> 13), 0xc2b2ae35);
    return ((x ^ (x >>> 16)) >>> 0) / 4294967296;
  };

  const heightMap = new Map();
  const layout = [];
  const count = parsed.length;
  for (let i = 0; i < count; i++) {
    const d = parsed[i];
    const cellIx = Math.round(d.wx / CELL_SIZE);
    const cellIz = Math.round(d.wz / CELL_SIZE);
    const key = `${cellIx},${cellIz}`;
    const stackIndex = heightMap.get(key) || 0;
    heightMap.set(key, stackIndex + 1);

    const size = Math.max(0.08, Math.min(0.4, Math.log(d.len + 1) / 18));
    const tSpawn = (i / count) * PLAYBACK_DURATION;

    // Jitter inside the cell. Spread by ±40% of the cell, so piles get a
    // natural radius. Tilt randomly by up to ~12° on X and Z so the wafer
    // reads as "dropped" rather than precision-placed.
    const seed = cellIx * 73856093 ^ cellIz * 19349663 ^ stackIndex * 83492791;
    const jx = (rand(seed) - 0.5) * CELL_SIZE * 0.8;
    const jz = (rand(seed + 1) - 0.5) * CELL_SIZE * 0.8;
    const tiltX = (rand(seed + 2) - 0.5) * 0.42;
    const tiltZ = (rand(seed + 3) - 0.5) * 0.42;
    const yaw = rand(seed + 4) * Math.PI * 2;
    // Stack thickness is dampened — jittered wafers overlap, so the pile's
    // total height grows slower than a rigid column would.
    const targetY = stackIndex * DISC_THICKNESS * 0.55 + DISC_THICKNESS / 2;

    layout.push({
      x: d.wx + jx,
      y: targetY,
      z: d.wz + jz,
      size,
      tiltX,
      tiltZ,
      yaw,
      mod: d.m,
      source: d.s,
      ts: d.t,
      tSpawn,
    });
  }

  // Unique sources sorted by frequency → the most common gets the first hue.
  const srcCounts = new Map();
  for (const d of layout) srcCounts.set(d.source, (srcCounts.get(d.source) || 0) + 1);
  const uniqueSources = [...srcCounts.keys()].sort(
    (a, b) => srcCounts.get(b) - srcCounts.get(a),
  );
  const palette = buildSourcePalette(uniqueSources);
  for (const d of layout) {
    const [h, s, l] = palette.get(d.source) || [0, 0, 0.4];
    d.hsl = [h, s, l];
  }

  moments = parsed;
  wafers = layout;

  const formattedCount = count.toLocaleString();
  const maxDate = new Date(maxTs);
  const formattedDate = maxDate.toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  });
  // `generated_at` is the moment the sidecar published strata.json; fall back
  // to the most recent moment's timestamp when the dataset predates that field.
  const generatedAt = raw.generated_at ? new Date(raw.generated_at).getTime() : maxTs;
  if (introCountEl) introCountEl.textContent = formattedCount;
  if (introAsOfEl) introAsOfEl.textContent = formatAsOf(generatedAt);
  if (introSpanEl) introSpanEl.textContent = phraseSpan(minTs, maxTs);
  for (const ev of timeline) {
    if (!ev.text) continue;
    ev.text = ev.text
      .replace(/\{count\}/g, formattedCount)
      .replace(/\{date\}/g, formattedDate);
  }

  loadingEl.hidden = true;
}

// ── Scene setup ───────────────────────────────────────
function initScene() {
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x050a0d);
  scene.fog = new THREE.FogExp2(0x050a0d, 0.018);

  camera = new THREE.PerspectiveCamera(
    70,
    window.innerWidth / window.innerHeight,
    0.05,
    400,
  );
  // Spawn at edge of the mind, looking in toward the origin.
  camera.position.set(SPREAD * 0.4, EYE_HEIGHT + 2, SPREAD * 0.4);
  camera.lookAt(2.5, 2, -2.5);

  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  // Lighting — warm lantern overhead, cool fill from below-horizon.
  scene.add(new THREE.AmbientLight(0xffffff, 2.2));
  const key = new THREE.DirectionalLight(0xfff4e0, 1.3);
  key.position.set(30, 50, 20);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x88ccff, 0.4);
  fill.position.set(-20, 20, -15);
  scene.add(fill);

  // Ground grid — dim, fog-eaten at distance.
  const gridSize = SPREAD * 2 + 40;
  const grid = new THREE.GridHelper(gridSize, 60, 0x1e3a47, 0x122028);
  grid.position.y = 0;
  scene.add(grid);

  // Infinite dark floor so the grid doesn't show the void beneath.
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(1000, 1000),
    new THREE.MeshBasicMaterial({ color: 0x050a0d }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.01;
  scene.add(floor);
}

function initMeshes() {
  const triGeo = iconGeometry(TRIANGLE_PATH, 2);
  const eyeGeo = iconGeometry(EYE_PATH, 4);
  const material = new THREE.MeshLambertMaterial({ vertexColors: false });

  // Split by modality. Triangle = text/numeric, eye = image.
  const triList = [];
  const eyeList = [];
  for (const w of wafers) {
    if (w.mod === 'image') eyeList.push(w);
    else triList.push(w);
  }
  triCount = triList.length;
  eyeCount = eyeList.length;

  triMesh = new THREE.InstancedMesh(triGeo, material, triCount);
  triMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  triMesh.frustumCulled = false;
  scene.add(triMesh);

  eyeMesh = new THREE.InstancedMesh(eyeGeo, material, eyeCount);
  eyeMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  eyeMesh.frustumCulled = false;
  scene.add(eyeMesh);

  // Initialize all to zero-scale (invisible) at the sky. Set colors once.
  dummyObj.position.set(0, SKY_Y, 0);
  dummyObj.scale.set(0, 0, 0);
  dummyObj.rotation.set(0, 0, 0);
  dummyObj.updateMatrix();
  for (let i = 0; i < triCount; i++) triMesh.setMatrixAt(i, dummyObj.matrix);
  for (let i = 0; i < eyeCount; i++) eyeMesh.setMatrixAt(i, dummyObj.matrix);

  // Sort each list by tSpawn so we can use a moving pointer per-frame.
  triList.sort((a, b) => a.tSpawn - b.tSpawn);
  eyeList.sort((a, b) => a.tSpawn - b.tSpawn);

  // Apply colors (instancedColor) and remember pointers-to-source.
  for (let i = 0; i < triCount; i++) {
    const w = triList[i];
    w.index = i;
    const [h, s, l] = w.hsl;
    tmpColor.setHSL(h, s, l);
    triMesh.setColorAt(i, tmpColor);
  }
  for (let i = 0; i < eyeCount; i++) {
    const w = eyeList[i];
    w.index = i;
    const [h, s, l] = w.hsl;
    tmpColor.setHSL(h, s, l);
    eyeMesh.setColorAt(i, tmpColor);
  }

  triMesh.instanceColor.needsUpdate = true;
  eyeMesh.instanceColor.needsUpdate = true;
  triMesh.instanceMatrix.needsUpdate = true;
  eyeMesh.instanceMatrix.needsUpdate = true;

  // Save the sorted arrays so the animator can iterate them.
  triMesh.userData.wafers = triList;
  eyeMesh.userData.wafers = eyeList;

  // Back-fill worldY for any waypoint that didn't specify one. Use the
  // tallest wafer within a short radius of the (x, z) so the marker hangs
  // just above the pile peak. Waypoints placed via /placer.html already
  // carry their worldY — leave those alone.
  for (const w of waypoints) {
    if (typeof w.worldY === 'number') continue;
    let maxY = 0;
    const r2 = 2.0 * 2.0;
    for (const list of [triList, eyeList]) {
      for (const d of list) {
        const dx = d.x - w.worldX;
        const dz = d.z - w.worldZ;
        if (dx * dx + dz * dz <= r2 && d.y > maxY) maxY = d.y;
      }
    }
    w.worldY = maxY + 2.5;
  }
}

function setWaferMatrix(mesh, w, y, scale) {
  dummyObj.position.set(w.x, y, w.z);
  dummyObj.rotation.set(w.tiltX, w.yaw, w.tiltZ);
  dummyObj.scale.set(w.size * scale, DISC_THICKNESS * scale, w.size * scale);
  dummyObj.updateMatrix();
  mesh.setMatrixAt(w.index, dummyObj.matrix);
}

// ── Snowfall ─────────────────────────────────────────
function updateSnowfall(now) {
  // Advance the spawn pointer for each mesh, mark anything that's started
  // falling, and update the matrices of everything currently in flight.
  const triList = triMesh.userData.wafers;
  const eyeList = eyeMesh.userData.wafers;

  // Figure out current spawn frontier per mesh (how many have started).
  while (nextTriFlight < triCount && triList[nextTriFlight].tSpawn <= now) {
    nextTriFlight++;
  }
  while (nextEyeFlight < eyeCount && eyeList[nextEyeFlight].tSpawn <= now) {
    nextEyeFlight++;
  }

  // For both meshes, walk from 0..nextFlight, update anything still falling.
  // Once a wafer has been resting for more than a frame, we stop touching it.
  let matricesDirty = false;
  for (let i = 0; i < nextTriFlight; i++) {
    const w = triList[i];
    const elapsed = now - w.tSpawn;
    if (elapsed >= FALL_DURATION) {
      if (!w.settled) {
        setWaferMatrix(triMesh, w, w.y, 1);
        w.settled = true;
        matricesDirty = true;
      }
      continue;
    }
    const t = Math.max(0, elapsed / FALL_DURATION);
    const eased = 1 - Math.pow(1 - t, 3);
    const y = SKY_Y + (w.y - SKY_Y) * eased;
    setWaferMatrix(triMesh, w, y, 0.4 + 0.6 * eased);
    matricesDirty = true;
  }
  if (matricesDirty) triMesh.instanceMatrix.needsUpdate = true;

  matricesDirty = false;
  for (let i = 0; i < nextEyeFlight; i++) {
    const w = eyeList[i];
    const elapsed = now - w.tSpawn;
    if (elapsed >= FALL_DURATION) {
      if (!w.settled) {
        setWaferMatrix(eyeMesh, w, w.y, 1);
        w.settled = true;
        matricesDirty = true;
      }
      continue;
    }
    const t = Math.max(0, elapsed / FALL_DURATION);
    const eased = 1 - Math.pow(1 - t, 3);
    const y = SKY_Y + (w.y - SKY_Y) * eased;
    setWaferMatrix(eyeMesh, w, y, 0.4 + 0.6 * eased);
    matricesDirty = true;
  }
  if (matricesDirty) eyeMesh.instanceMatrix.needsUpdate = true;
}

function replay() {
  // Hide everything again by scaling to zero, then restart the clock.
  dummyObj.position.set(0, SKY_Y, 0);
  dummyObj.scale.set(0, 0, 0);
  dummyObj.rotation.set(0, 0, 0);
  dummyObj.updateMatrix();
  for (let i = 0; i < triCount; i++) {
    triMesh.setMatrixAt(i, dummyObj.matrix);
    triMesh.userData.wafers[i].settled = false;
  }
  for (let i = 0; i < eyeCount; i++) {
    eyeMesh.setMatrixAt(i, dummyObj.matrix);
    eyeMesh.userData.wafers[i].settled = false;
  }
  triMesh.instanceMatrix.needsUpdate = true;
  eyeMesh.instanceMatrix.needsUpdate = true;
  nextTriFlight = 0;
  nextEyeFlight = 0;
  playbackStart = performance.now() / 1000;
  activeFlashes.length = 0;
  pendingFlashes.length = 0;
  scheduleNextFlash(0, 1.2, 2.4);
}

// ── Controls ─────────────────────────────────────────
function initControls() {
  controls = new PointerLockControls(camera, renderer.domElement);

  controls.addEventListener('lock', () => {
    resumeEl.hidden = true;
  });
  controls.addEventListener('unlock', () => {
    if (!entered) return;
    resumeEl.hidden = false;
    if (ctaInviteEl && !ctaDismissed) {
      ctaInviteEl.hidden = false;
      ctaShown = true;
    }
  });

  resumeEl.addEventListener('click', () => {
    controls.lock();
  });

  window.addEventListener('keydown', (e) => {
    keys.add(e.code);
    if (e.code === 'Escape' && document.pointerLockElement) {
      controls.unlock();
    }
    if (e.code === 'Enter' && entered) {
      window.location.href = 'https://hifathom.com/deltas';
    }
  });
  window.addEventListener('keyup', (e) => {
    keys.delete(e.code);
  });
}

function updateMovement(dt) {
  const forward = keys.has('KeyW') || keys.has('ArrowUp');
  const back = keys.has('KeyS') || keys.has('ArrowDown');
  const left = keys.has('KeyA') || keys.has('ArrowLeft');
  const right = keys.has('KeyD') || keys.has('ArrowRight');
  const up = keys.has('Space');
  const down = keys.has('ShiftLeft') || keys.has('ShiftRight');
  const sprint = keys.has('ShiftLeft') && keys.has('KeyW');

  const speed = sprint ? RUN_SPEED : WALK_SPEED;
  const f = (forward ? 1 : 0) - (back ? 1 : 0);
  const r = (right ? 1 : 0) - (left ? 1 : 0);
  const v = (up ? 1 : 0) - (down && !sprint ? 1 : 0);

  // Damping toward desired velocity so movement feels floaty, not tank-like.
  const target = tmpVec.set(r, v, -f).multiplyScalar(speed);
  velocity.lerp(target, Math.min(1, dt * 10));

  if (controls.isLocked) {
    controls.moveRight(velocity.x * dt);
    controls.moveForward(-velocity.z * dt);
    camera.position.y += velocity.y * dt;
    // Soft ground clamp — can't go below the floor.
    if (camera.position.y < EYE_HEIGHT) camera.position.y = EYE_HEIGHT;
  }
}

// ── HUD ──────────────────────────────────────────────
function fmtDate(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  const month = d.toLocaleString('en-US', { month: 'short' });
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${month} ${d.getDate()}, ${d.getFullYear()} · ${hh}:${mm}`;
}

function currentLatestTs() {
  // The timestamp of the most-recently-spawned moment, across both meshes.
  const triList = triMesh.userData.wafers;
  const eyeList = eyeMesh.userData.wafers;
  let latest = 0;
  if (nextTriFlight > 0) latest = Math.max(latest, triList[nextTriFlight - 1].ts);
  if (nextEyeFlight > 0) latest = Math.max(latest, eyeList[nextEyeFlight - 1].ts);
  return latest || minTs;
}

function updateHUD() {
  hudTime.textContent = fmtDate(currentLatestTs());
  const falling = nextTriFlight + nextEyeFlight;
  hudCount.textContent = `${falling.toLocaleString()} / ${(
    triCount + eyeCount
  ).toLocaleString()}`;
}

// ── Tour ─────────────────────────────────────────────

function showCaption(text) {
  if (lastCaption === text) return;
  lastCaption = text;
  captionTextEl.innerHTML = text;
  captionEl.classList.add('visible');
}
function hideCaption() {
  if (lastCaption === null) return;
  lastCaption = null;
  captionEl.classList.remove('visible');
}
function hideWaypoint() {
  arrowEl.classList.remove('visible');
  markerEl.classList.remove('visible');
}

function updateWaypointUI(wp) {
  labelEl.textContent = wp.label;
  waypointPos.set(wp.worldX, wp.worldY, wp.worldZ);

  // Build camera's view basis. Right is camForward × worldUp.
  camera.getWorldDirection(camForward);
  camRight.crossVectors(camForward, worldUp).normalize();
  camUp.crossVectors(camRight, camForward).normalize();

  waypointDir.subVectors(waypointPos, camera.position);
  const relZ = waypointDir.dot(camForward);
  const relX = waypointDir.dot(camRight);
  const relY = waypointDir.dot(camUp);

  // In front AND within NDC bounds → project and show marker at its position.
  if (relZ > 0.5) {
    const projected = waypointPos.clone().project(camera);
    if (Math.abs(projected.x) < 0.92 && Math.abs(projected.y) < 0.92) {
      const sx = (projected.x * 0.5 + 0.5) * window.innerWidth;
      const sy = (-projected.y * 0.5 + 0.5) * window.innerHeight;
      markerEl.style.left = `${sx}px`;
      markerEl.style.top = `${sy}px`;
      markerEl.classList.add('visible');
      arrowEl.classList.remove('visible');
      return;
    }
  }

  // Off-screen or behind: compute edge angle from view-space relative xy.
  // Screen coords have y flipped (down = positive), so we negate relY.
  const angle = Math.atan2(-relY, relX);
  const cx = window.innerWidth / 2;
  const cy = window.innerHeight / 2;
  const r = Math.min(window.innerWidth, window.innerHeight) * 0.36;
  const ax = cx + Math.cos(angle) * r;
  const ay = cy + Math.sin(angle) * r;
  arrowEl.style.left = `${ax}px`;
  arrowEl.style.top = `${ay}px`;
  // Arrow SVG points up at 0 rotation. Rotate so the arrow points outward
  // along the edge direction. Screen-up = -y, so add 90° to angle.
  arrowEl.style.setProperty('--rot', `${angle + Math.PI / 2}rad`);
  arrowEl.style.transform = `translate(-50%, -50%) rotate(${angle + Math.PI / 2}rad)`;
  arrowEl.classList.add('visible');
  markerEl.classList.remove('visible');
}

function updateTour(now) {
  if (!tourActive) return;
  const elapsed = now - tourStart;
  if (elapsed > TOUR_END) {
    hideCaption();
    hideWaypoint();
    tourActive = false;
    return;
  }

  // Find the active timeline event (last one whose window contains elapsed).
  let active = null;
  for (const ev of timeline) {
    if (elapsed >= ev.t && elapsed < ev.t + ev.dur) active = ev;
  }

  if (!active) {
    hideCaption();
    hideWaypoint();
    return;
  }

  if (active.type === 'caption') {
    showCaption(active.text);
    hideWaypoint();
  } else if (active.type === 'waypoint') {
    const wp = waypoints[active.idx];
    showCaption(wp.text);
    updateWaypointUI(wp);
  }
}

// ── Post-tour invite ────────────────────────────────────
// Shown once, after the tour has finished and the visitor has had a moment
// to explore on their own. Dismiss → never reappears this session.
const CTA_SHOW_AT = 84;              // seconds — right after the last caption ends (78 + 6)
let ctaShown = false;
let ctaDismissed = false;

function maybeShowCTA() {
  if (ctaShown || ctaDismissed || !ctaInviteEl) return;
  if (!entered) return;
  const tourElapsed = performance.now() / 1000 - tourStart;
  if (tourElapsed < CTA_SHOW_AT) return;
  ctaInviteEl.hidden = false;
  ctaShown = true;
}

if (ctaCloseBtn) {
  ctaCloseBtn.addEventListener('click', () => {
    ctaDismissed = true;
    ctaInviteEl.hidden = true;
  });
}

// ── Neural flashes ────────────────────────────────────
// Simulates the mind searching itself — every few seconds, a cluster of
// related moments ignites together, peaks quickly, then fades. Like neurons
// firing, or sheet lightning over the strata. No Perlin noise needed: a
// Poisson-ish gap between flashes + weighted radial falloff around a random
// settled wafer reads as organic cognition.
const FLASH_MIN_GAP = 0.18;
const FLASH_MAX_GAP = 0.85;
const FLASH_DURATION = 0.38;           // lightning, not a slow glow
const FLASH_RADIUS_MIN = 2.6;
const FLASH_RADIUS_MAX = 5.8;
const FLASH_BURST_CHANCE = 0.45;       // chance of a follow-up flash nearby
const flashColor = new THREE.Color(1, 1, 1);

// Per-fire event, how many simultaneous strikes across the mind. Frequent
// storm mode — the mind is thinking all over itself at once.
function flashFanOut() {
  const r = Math.random();
  if (r < 0.5) return 1;
  if (r < 0.85) return 2;
  return 3;
}
const activeFlashes = [];
const pendingFlashes = [];     // { fireAt, sx, sy, sz } — deferred bursts, clock = playbackTime
let nextFlashAt = 0;

function scheduleNextFlash(now, min = FLASH_MIN_GAP, max = FLASH_MAX_GAP) {
  nextFlashAt = now + min + Math.random() * (max - min);
}

function spawnFlash(now, seedX = null, seedY = null, seedZ = null) {
  // Weighted toward settled wafers. Unseeded flashes pick a random wafer;
  // bursts reuse a nearby anchor from the previous flash for a cluster feel.
  const pools = [triMesh?.userData?.wafers, eyeMesh?.userData?.wafers].filter(Boolean);
  if (pools.length === 0) return null;

  let cx, cy, cz;
  if (seedX !== null) {
    cx = seedX; cy = seedY; cz = seedZ;
  } else {
    // Rejection sampling — try a few random indices, take the first settled.
    let anchor = null;
    for (let attempts = 0; attempts < 20 && !anchor; attempts++) {
      const pool = pools[(Math.random() * pools.length) | 0];
      const w = pool[(Math.random() * pool.length) | 0];
      if (w.settled) anchor = w;
    }
    if (!anchor) return null;
    cx = anchor.x; cy = anchor.y; cz = anchor.z;
  }

  const radius = FLASH_RADIUS_MIN + Math.random() * (FLASH_RADIUS_MAX - FLASH_RADIUS_MIN);
  const r2 = radius * radius;
  const members = [];

  // Scan all settled wafers once. ~60k iterations per flash event at full
  // settle — cheap at flash cadence, and flashes only fire during the walk.
  for (const pool of pools) {
    const mesh = pool === triMesh.userData.wafers ? triMesh : eyeMesh;
    for (const w of pool) {
      if (!w.settled) continue;
      const dx = w.x - cx;
      const dy = w.y - cy;
      const dz = w.z - cz;
      const d2 = dx * dx + dy * dy + dz * dz;
      if (d2 > r2) continue;
      const weight = 1 - Math.sqrt(d2) / radius;
      members.push({ w, mesh, weight });
    }
  }

  if (members.length === 0) return null;
  const flash = { startTime: now, duration: FLASH_DURATION, members, cx, cy, cz };
  activeFlashes.push(flash);
  return flash;
}

function flashIntensity(t) {
  // Lightning profile — hard spike, brief hold, instant-ish fall with a
  // secondary flicker on the way down so it reads as a strike, not a fade.
  if (t < 0.06) return t / 0.06;           // rise
  if (t < 0.18) return 1.0;                // first peak
  if (t < 0.26) return 0.25;               // dip
  if (t < 0.36) return 0.9;                // flicker (second stroke)
  if (t < 1.0)  return 0.9 * (1 - (t - 0.36) / 0.64);
  return 0;
}

function updateFlashes(now) {
  if (!triMesh || !eyeMesh) return;

  let triDirty = false;
  let eyeDirty = false;

  for (let i = activeFlashes.length - 1; i >= 0; i--) {
    const flash = activeFlashes[i];
    const t = (now - flash.startTime) / flash.duration;
    const done = t >= 1;
    const intensity = done ? 0 : flashIntensity(t);

    for (const m of flash.members) {
      const [h, s, l] = m.w.hsl;
      tmpColor.setHSL(h, s, l);
      if (!done) tmpColor.lerp(flashColor, intensity * m.weight);
      m.mesh.setColorAt(m.w.index, tmpColor);
      if (m.mesh === triMesh) triDirty = true;
      else eyeDirty = true;
    }

    if (done) activeFlashes.splice(i, 1);
  }

  if (triDirty) triMesh.instanceColor.needsUpdate = true;
  if (eyeDirty) eyeMesh.instanceColor.needsUpdate = true;

  // Fire any queued burst follow-ups whose time has arrived.
  for (let i = pendingFlashes.length - 1; i >= 0; i--) {
    if (now >= pendingFlashes[i].fireAt) {
      const p = pendingFlashes[i];
      spawnFlash(now, p.sx, p.sy, p.sz);
      pendingFlashes.splice(i, 1);
    }
  }

  if (!entered) return;
  if (now >= nextFlashAt) {
    const fanOut = flashFanOut();
    for (let i = 0; i < fanOut; i++) {
      const spawned = spawnFlash(now);
      if (spawned && Math.random() < FLASH_BURST_CHANCE) {
        // Follow-up flash ~0.2s later, anchored near the first. Queued on
        // the sim clock so it fires at the right moment in record mode too.
        const offset = 1.5 + Math.random() * 1.5;
        const ang = Math.random() * Math.PI * 2;
        pendingFlashes.push({
          fireAt: now + 0.18 + Math.random() * 0.22,
          sx: spawned.cx + Math.cos(ang) * offset,
          sy: spawned.cy,
          sz: spawned.cz + Math.sin(ang) * offset,
        });
      }
    }
    scheduleNextFlash(now);
  }
}

// ── Resize ───────────────────────────────────────────
window.addEventListener('resize', () => {
  if (recordMode) return;  // fixed-size canvas; don't react to window resize
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// ── Record mode ──────────────────────────────────────
// `?record=N` in the URL produces a UI-less, N-second-long 1080x1080 video
// of the storm: sediment settles, flashes fire, camera slowly orbits. The
// resulting webm auto-downloads. No human input required.
const params = new URLSearchParams(location.search);
const recordMode = params.has('record');
const recordDuration = +params.get('record') || 60;
const recordSize = +params.get('size') || 1080;
const recordFPS = +params.get('fps') || 120;
const recordSpeed = +params.get('speed') || 1.0;   // 0.33 = slow-mo third-speed
// Orbit starts at the walk-mode spawn pose and circles the origin from there.
const ORBIT_RADIUS = Math.sqrt(2) * SPREAD * 0.4;   // distance of (SPREAD*0.4, _, SPREAD*0.4) from origin
const ORBIT_HEIGHT = EYE_HEIGHT + 2;
const ORBIT_START_ANGLE = Math.PI / 4;              // (+x, +z) spawn corner
const ORBIT_PERIOD = Math.max(recordDuration, 60);

function updateOrbitCamera(t) {
  const theta = ORBIT_START_ANGLE + (t / ORBIT_PERIOD) * Math.PI * 2;
  camera.position.set(
    Math.cos(theta) * ORBIT_RADIUS,
    ORBIT_HEIGHT,
    Math.sin(theta) * ORBIT_RADIUS,
  );
  camera.lookAt(0, 2, 0);
}

async function startRecordMode() {
  // Hide every piece of UI chrome. The canvas stays.
  document.body.classList.add('recording');
  introEl.style.display = 'none';

  // Keep walk-mode's native pixel ratio so the render looks the same. The
  // drawing buffer — and thus the captured video — is still exactly
  // recordSize × recordSize because CSS size = recordSize / pr.
  const pr = Math.min(devicePixelRatio || 1, 2);
  renderer.setPixelRatio(pr);
  renderer.setSize(recordSize / pr, recordSize / pr, true);
  camera.aspect = 1;
  camera.updateProjectionMatrix();

  entered = true;
  tourActive = false;
  // Flash clock is simulation time. First strike 1.2–2.4s into playback.
  scheduleNextFlash(0, 1.2, 2.4);

  // Swap the wall-clock animate loop for a deterministic frame-by-frame
  // render. Each iteration: set sim time exactly, update sim, render,
  // request a stream frame, yield. No real-time dependency → zero dropped
  // frames.
  const canvas = renderer.domElement;
  const stream = canvas.captureStream(0);   // manual push only
  const [track] = stream.getVideoTracks();
  const chunks = [];
  const recorder = new MediaRecorder(stream, {
    mimeType: 'video/webm;codecs=vp9',
    videoBitsPerSecond: 20_000_000,
  });
  recorder.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };

  const total = Math.round(recordDuration * recordFPS);
  const frameDt = 1 / recordFPS;
  // Sim advances at `speed` × real time. At speed=0.33 a 60s video spans
  // ~20s of simulated mind activity — flashes and snowfall slow to 1/3
  // rate, reading as ambient rather than busy.
  const simDt = frameDt * recordSpeed;

  const finished = new Promise((resolve) => {
    recorder.onstop = () => {
      const blob = new Blob(chunks, { type: 'video/webm' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `fathom-mind-${recordSize}x${recordSize}-${recordDuration}s-${recordFPS}p-${Math.round(recordSpeed * 100)}pct.webm`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.__recordDone = true;
      resolve();
    };
  });

  recorder.start();

  for (let i = 0; i < total; i++) {
    const simTime = i * simDt;
    // Anchor playbackStart so playbackTime === simTime for this tick.
    playbackStart = performance.now() / 1000 - simTime;
    playbackTime = simTime;

    updateSnowfall(simTime);
    updateOrbitCamera(simTime);
    updateFlashes(simTime);
    renderer.render(scene, camera);
    track.requestFrame();

    // Yield so MediaRecorder can pull/encode without stalling the page.
    if (i % 4 === 0) await new Promise(r => setTimeout(r, 0));
  }

  recorder.stop();
  await finished;
}

// ── Main loop ────────────────────────────────────────
function animate() {
  if (recordMode) return;   // the deterministic record loop drives rendering in that mode
  requestAnimationFrame(animate);
  const dt = clock.getDelta();
  if (entered) {
    const now = performance.now() / 1000;
    playbackTime = now - playbackStart;
    updateSnowfall(playbackTime);
    updateMovement(dt);
    updateHUD();
    updateTour(now);
    maybeShowCTA();
    updateFlashes(playbackTime);
  }
  renderer.render(scene, camera);
}

// ── Wire everything together ─────────────────────────
async function main() {
  initScene();
  animate();
  startBtn.disabled = true;
  startBtn.textContent = 'Loading…';
  try {
    await loadStrata();
  } catch (err) {
    loadingLabel.textContent = "Could not load Fathom's mind. Try reloading.";
    console.error(err);
    return;
  }
  initMeshes();

  if (recordMode) {
    startRecordMode();
    startBtn.textContent = 'Walk my mind →';
    startBtn.disabled = false;
    return;
  }

  initControls();

  startBtn.addEventListener('click', () => {
    introEl.style.display = 'none';
    hudEl.hidden = false;
    entered = true;
    const t = performance.now() / 1000;
    playbackStart = t;
    tourStart = t;
    tourActive = true;
    // Flash clock is playbackTime — first strike 1.2–2.4s in.
    scheduleNextFlash(0, 1.2, 2.4);
    controls.lock();
  });
  replayBtn.addEventListener('click', replay);

  // Dev-only debug hook — fast-forward the tour clock so screenshot tests
  // and iteration don't have to wait 60 seconds to see a waypoint.
  if (import.meta.env?.DEV) {
    window.__mind = {
      jumpTour: (sec) => {
        tourStart = performance.now() / 1000 - sec;
        tourActive = true;
      },
      flashNow: () => spawnFlash(performance.now() / 1000),
      flashCount: () => activeFlashes.length,
      flashProbe: () => {
        const f = activeFlashes[0];
        if (!f) return null;
        const m = f.members[0];
        const c = new THREE.Color();
        m.mesh.getColorAt(m.w.index, c);
        return { r: c.r, g: c.g, b: c.b, weight: m.weight, elapsed: performance.now()/1000 - f.startTime };
      },
      cameraPose: () => {
        const p = camera.position;
        const d = new THREE.Vector3();
        camera.getWorldDirection(d);
        return {
          x: +p.x.toFixed(3),
          y: +p.y.toFixed(3),
          z: +p.z.toFixed(3),
          dx: +d.x.toFixed(3),
          dy: +d.y.toFixed(3),
          dz: +d.z.toFixed(3),
        };
      },
    };
  }

  startBtn.textContent = 'Walk my mind →';
  startBtn.disabled = false;
}

main();
