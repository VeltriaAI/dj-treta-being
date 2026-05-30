// VDJ Treta — local beat-synced visual engine.
//
// Pipeline:
//   Mixxx /api/live (≈20 Hz)  ─► beat-clock PLL ─► smooth phase + kick @60fps
//   Mixxx /api/status + track_info (≈1 Hz) ─► genre / key / now-playing
//        ─► director picks the scene clip ─► GLSL renderer ─► screen
//
// The PLL is the trick: polling is coarse (~20 Hz) but we advance beat phase
// locally from BPM at frame rate and softly correct toward each fresh
// beat_distance reading, so the visuals land on the kick with no stutter.
//
// Scene engine is double-buffered: two <video> elements + two textures blended
// by uMix. The same crossfade machinery hides the loop seam (fade a clip to
// itself before its end) AND smooths track changes (fade to a new clip).

// ───────────────────────── shared live state ─────────────────────────
const S = {
  bpm: 124, phase: 0, kick: 0, beatConf: 0,
  energy: 0, vuL: 0, vuR: 0,
  hue: 0.55, genre: '', keyStr: '', title: '', artist: '',
  activeDeck: 2, playing: false,
  // structure (computed client-side from the VU stream, mirrors relay perception)
  drop: 0, breakdown: 0, buildup: 0, energyDir: 'steady',
};

// VU history for drop/breakdown/buildup detection (~3s at 20 Hz)
const vuHist = [];

// PLL anchor
let anchorPhase = 0;          // beat phase at anchorTime
let anchorTime = performance.now();
let lastFramePhase = 0;

// ───────────────────────── data polling ─────────────────────────
const PITCH_HUE = { C:0.00, 'C#':0.07, D:0.13, 'D#':0.20, E:0.27, F:0.37,
  'F#':0.46, G:0.55, 'G#':0.63, A:0.72, 'A#':0.82, B:0.92 };

function keyToHue(keyStr) {
  if (!keyStr) return S.hue;
  const m = keyStr.match(/^([A-G]#?)/);
  if (!m) return S.hue;
  let h = PITCH_HUE[m[1]] ?? S.hue;
  if (/m$/.test(keyStr)) h = (h + 0.04) % 1;   // minor → nudge cooler
  return h;
}

function activeDeckOf(live) {
  // The deck we're actually hearing: playing, loudest by volume·VU.
  let best = null, bestScore = -1;
  for (const d of [1, 2]) {
    const dk = live['deck' + d];
    if (!dk || !dk.playing) continue;
    const score = (dk.volume || 0) * ((dk.vu_left + dk.vu_right) / 2 + 0.01);
    if (score > bestScore) { bestScore = score; best = d; }
  }
  return best;
}

const _mean = (a, i, j) => { const s = a.slice(i, j); return s.length ? s.reduce((x, y) => x + y, 0) / s.length : 0; };

// Drop / breakdown / buildup from the master-VU stream — mirrors the relay's
// perception heuristics, run client-side so it works fully local.
function detectStructure() {
  const n = vuHist.length;
  if (n >= 30) {
    const d = _mean(vuHist, n - 15, n) - _mean(vuHist, n - 30, n - 15);
    S.energyDir = d > 0.06 ? 'building' : d > 0.02 ? 'rising'
      : d < -0.06 ? 'dropping' : d < -0.02 ? 'falling' : 'steady';
  }
  let breakdown = false;
  if (n >= 40) {
    const recent = _mean(vuHist, n - 20, n), prior = _mean(vuHist, n - 40, n - 20);
    if (prior > 0.30 && recent < prior * 0.55) breakdown = true;  // floor dropped out
  }
  let drop = false;
  if (n >= 25) {
    const quiet = _mean(vuHist, n - 25, n - 12), now = _mean(vuHist, n - 5, n);
    if (quiet < 0.28 && now > quiet + 0.32) drop = true;          // surge out of quiet
  }
  if (drop && !S._dropArmed) { S.drop = 1; S._dropArmed = true; } // edge-trigger once
  if (!drop) S._dropArmed = false;
  S._breakTarget = breakdown ? 1 : 0;
  S._buildTarget = (S.energyDir === 'building' && !breakdown) ? 1 : 0;
}

async function pollLive() {
  try {
    const live = await (await fetch('/mixxx/api/live', { cache: 'no-store' })).json();
    const d = activeDeckOf(live);
    if (d) {
      S.activeDeck = d;
      S.playing = true;
      const dk = live['deck' + d];
      if (dk.bpm > 20) S.bpm = dk.bpm;
      softCorrectPLL(dk.beat_distance);
      if (dk.beat_active) S.beatConf = 1;          // ground-truth kick marker
    } else {
      S.playing = false;
    }
    S.vuL = live.master_vu_left || 0;
    S.vuR = live.master_vu_right || 0;
    const vu = (S.vuL + S.vuR) / 2;
    // smooth energy toward VU (attack faster than release)
    const target = Math.min(1, vu / 0.85);
    S.energy += (target - S.energy) * (target > S.energy ? 0.35 : 0.06);
    vuHist.push(vu);
    if (vuHist.length > 60) vuHist.shift();
    detectStructure();
  } catch (e) { /* Mixxx not reachable yet — keep last state */ }
}

async function pollMeta() {
  try {
    await (await fetch('/mixxx/api/status', { cache: 'no-store' })).json();
    const d = S.activeDeck;
    const ti = await (await fetch(`/mixxx/api/deck/${d}/track_info`, { cache: 'no-store' })).json();
    const nk = ti.key || '';
    if (nk !== S.keyStr) S.keyStr = nk;
    S.targetHue = keyToHue(nk);
    S.genre = ti.genre || '';
    if (ti.title !== S.title || ti.artist !== S.artist) {
      S.title = ti.title || ''; S.artist = ti.artist || '';
      showNowPlaying(S.title, S.artist);
    }
  } catch (e) { /* ignore */ }
}

// soft phase-locked loop: nudge anchor toward measured beat_distance
function softCorrectPLL(measuredPhase) {
  if (measuredPhase == null) return;
  const now = performance.now();
  const predicted = framePhase(now);
  let err = measuredPhase - predicted;
  err -= Math.round(err);                 // wrap to [-0.5, 0.5]
  // move anchor so predicted shifts by 0.25·err (critically-ish damped)
  anchorPhase = (anchorPhase + 0.25 * err + 1) % 1;
}

function framePhase(now) {
  const beatsElapsed = ((now - anchorTime) / 1000) * (S.bpm / 60);
  return (anchorPhase + beatsElapsed) % 1;
}

// ───────────────────────── WebGL renderer ─────────────────────────
const canvas = document.getElementById('gl');
const gl = canvas.getContext('webgl2', { antialias: false, alpha: false });
if (!gl) document.body.innerHTML = '<p style="color:#fff;font:14px monospace;padding:20px">WebGL2 not available.</p>';

const VERT = `#version 300 es
const vec2 P[3] = vec2[](vec2(-1.,-1.), vec2(3.,-1.), vec2(-1.,3.));
void main(){ gl_Position = vec4(P[gl_VertexID], 0., 1.); }`;

const FRAG = `#version 300 es
precision highp float;
out vec4 o;
uniform vec2  uRes;
uniform float uTime, uPhase, uKick, uEnergy, uHue, uHasTex, uIsVideo, uMix;
uniform float uDrop, uBreak, uBuild;
uniform sampler2D uTex0, uTex1;

// hsv→rgb
vec3 hsv(vec3 c){
  vec4 K=vec4(1.,2./3.,1./3.,3.);
  vec3 p=abs(fract(c.xxx+K.xyz)*6.-K.www);
  return c.z*mix(K.xxx,clamp(p-K.xxx,0.,1.),c.y);
}
// value noise + fbm
float h21(vec2 p){ p=fract(p*vec2(123.34,456.21)); p+=dot(p,p+45.32); return fract(p.x*p.y); }
float vnoise(vec2 p){
  vec2 i=floor(p), f=fract(p); f=f*f*(3.-2.*f);
  float a=h21(i), b=h21(i+vec2(1,0)), c=h21(i+vec2(0,1)), d=h21(i+vec2(1,1));
  return mix(mix(a,b,f.x),mix(c,d,f.x),f.y);
}
float fbm(vec2 p){
  float s=0., a=.5; mat2 m=mat2(1.6,1.2,-1.2,1.6);
  for(int i=0;i<5;i++){ s+=a*vnoise(p); p=m*p; a*=.5; } return s;
}
// crossfaded scene sample at one channel offset (blends the two video buffers)
float scene(vec2 uvR, vec2 uvG, vec2 uvB, int ch){
  if(ch==0) return mix(texture(uTex0,uvR).r, texture(uTex1,uvR).r, uMix);
  if(ch==1) return mix(texture(uTex0,uvG).g, texture(uTex1,uvG).g, uMix);
  return mix(texture(uTex0,uvB).b, texture(uTex1,uvB).b, uMix);
}

void main(){
  vec2 uv=(gl_FragCoord.xy-.5*uRes)/uRes.y;   // centered, aspect-correct
  float beatPulse = uKick;                     // 1 at kick, decays
  float energy = clamp(uEnergy + 0.25*uBuild, 0.0, 1.2);  // buildup lifts energy

  // domain-warped flow — speed & turbulence scale with energy (buildup accelerates)
  float t = uTime*(0.06 + energy*0.22 + 0.15*uBuild);
  vec2 q = vec2(fbm(uv*1.5 + t), fbm(uv*1.5 - t + 4.3));
  vec2 r = vec2(fbm(uv*1.5 + 1.7*q + t*0.6), fbm(uv*1.5 + 1.7*q - t*0.5));
  float f = fbm(uv*2.2 + 2.0*r + vec2(0.0, t));

  // throttle: energy + buildup push in (faster travel), kick breathes, drop punches
  float zoom = 1.0 - 0.06*beatPulse - 0.10*uBuild - 0.05*energy + 0.10*uDrop;
  vec2 cuv = uv*zoom;
  vec3 col;

  if(uHasTex > 0.5){
    // footage already moves → warp lighter; overscan crops the Veo watermark.
    float wAmt = mix(1.0, 0.30, uIsVideo);
    float os   = mix(1.0, 0.86, uIsVideo);     // sample inner 86% → ~7% crop/side
    vec2 tuv = cuv*0.5 + 0.5;
    tuv = (tuv - 0.5)*os + 0.5;
    vec2 warp = ((r-0.5)*(0.05 + energy*0.10) + (q-0.5)*0.03*beatPulse) * wAmt;
    vec2 base = tuv + warp + normalize(uv + vec2(1e-4)) * uDrop * 0.05; // warp-burst out of center on drop
    float ca = (0.004 + 0.012*beatPulse + 0.03*uDrop) * mix(1.0, 1.4, uIsVideo); // chroma on kick/drop
    col = vec3(scene(base+vec2(ca,0.), base, base-vec2(ca,0.), 0),
               scene(base+vec2(ca,0.), base, base-vec2(ca,0.), 1),
               scene(base+vec2(ca,0.), base, base-vec2(ca,0.), 2));
    col *= mix(0.85 + 0.5*f, 0.95 + 0.20*f*energy, uIsVideo);
    col = mix(col, col*hsv(vec3(uHue,0.5,1.0))*1.4, mix(0.25, 0.12, uIsVideo));
  } else {
    // pure procedural aurora keyed to the musical key
    float hue = fract(uHue + 0.12*f + 0.05*beatPulse);
    float sat = 0.6 + 0.3*energy;
    float val = pow(f, 1.6)*(0.7 + 0.6*energy);
    col = hsv(vec3(hue, sat, val));
    col += hsv(vec3(fract(uHue+0.5), 0.7, 1.0)) * pow(max(0.,r.x),3.0)*0.6; // accent
  }

  float d = length(uv);

  // breakdown — the floor drops out: desaturate, cool, dim into a hush
  float lum = dot(col, vec3(0.299, 0.587, 0.114));
  vec3 cool = vec3(lum) * vec3(0.72, 0.86, 1.12);
  col = mix(col, cool, uBreak * 0.8);
  col *= 1.0 - 0.28 * uBreak;

  // kick bloom from center
  col += hsv(vec3(uHue,0.3,1.0)) * beatPulse * 0.35 * exp(-d*2.5);

  // overall brightness pulse + energy lift
  col *= 0.92 + 0.18*beatPulse + 0.10*energy;

  // drop — white burst from center + full-frame flash
  col += vec3(1.0) * uDrop * (0.7*exp(-d*1.8) + 0.18);

  // grade — neon-on-black like pro VJ loops: deep vignette, crushed blacks,
  // lifted luminous highlights, punchier saturation.
  col *= 1.0 - 0.55*pow(d*0.82, 2.2);           // stronger vignette frames the space
  col = col/(col+0.48);                          // tonemap (less midtone lift)
  col = max(col - 0.018, 0.0) / 0.982;           // crush near-blacks to true black
  col = pow(col, vec3(0.92));
  float sl = dot(col, vec3(0.299, 0.587, 0.114));
  col = mix(vec3(sl), col, 1.18);                // saturation pop
  o = vec4(clamp(col, 0.0, 1.0), 1.0);
}`;

function compile(type, src) {
  const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
  return s;
}
const prog = gl.createProgram();
gl.attachShader(prog, compile(gl.VERTEX_SHADER, VERT));
gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, FRAG));
gl.linkProgram(prog);
if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
gl.useProgram(prog);
const U = {};
for (const n of ['uRes', 'uTime', 'uPhase', 'uKick', 'uEnergy', 'uHue', 'uHasTex',
                 'uIsVideo', 'uMix', 'uDrop', 'uBreak', 'uBuild', 'uTex0', 'uTex1'])
  U[n] = gl.getUniformLocation(prog, n);

gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);   // video/image rows are top-down
function makeTex() {
  const tx = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tx);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([10, 10, 14, 255]));
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return tx;
}
const texs = [makeTex(), makeTex()];

// ───────────────────────── scene engine (double-buffered crossfade) ───────────
// Two <video> elements; uMix blends their textures. Crossfade handles both the
// loop seam (fade a clip to itself before its end) and track changes.
const CROSSFADE = 0.7;                  // seconds
let hasTex = 0, hasVideo = 0;
let live = 0;                           // index of the element currently shown
let mix = 0, mixTarget = 0;             // 0 → texs[0], 1 → texs[1]
let curSlug = null;
let loopFading = false;

// Energy-state "spaces" — the three altitudes of the journey, flown live off
// Mixxx energy + section. The crossfade engine blends between them.
let sceneState = null, stateSince = 0;
const MIN_DWELL = 3.5;   // seconds in a state before a calm (non-drop) transition

function makeVid() {
  const v = document.createElement('video');
  v.muted = true; v.loop = true; v.playsInline = true; v.preload = 'auto';
  v._slug = null;
  // Real Chrome won't decode a <video> that isn't in the DOM (it stalls at
  // readyState 0). Attach it hidden-but-rendered (not display:none) so the
  // media pipeline stays alive; the texture uses the full intrinsic resolution
  // regardless of this 2px on-screen size.
  v.style.cssText = 'position:fixed;left:0;bottom:0;width:2px;height:2px;opacity:0.01;z-index:-1;pointer-events:none';
  document.body.appendChild(v);
  return v;
}
const vids = [makeVid(), makeVid()];

// Which space the music wants right now. Hysteresis (wider exit than entry
// thresholds) keeps it from flapping at band edges.
function desiredState() {
  if (S.drop > 0.5) return 'neon';                 // the drop → punch to the peak
  if (S.breakdown > 0.5) return 'float';           // breakdown → cut engines, float
  const e = S.energy;
  if (sceneState === 'neon')  return e > 0.55 ? 'neon'  : (e < 0.30 ? 'float' : 'wormhole');
  if (sceneState === 'float') return e < 0.42 ? 'float' : (e > 0.72 ? 'neon' : 'wormhole');
  return e > 0.72 ? 'neon' : (e < 0.28 ? 'float' : 'wormhole');   // from wormhole / initial
}
function updateScene(nowMs) {
  const want = desiredState();
  if (want === sceneState) return;
  const dropForce = want === 'neon' && S.drop > 0.5;   // drops bypass the dwell timer
  if (sceneState === null || dropForce || (nowMs - stateSince) / 1000 >= MIN_DWELL) {
    sceneState = want; stateSince = nowMs;
    setScene(want);
  }
}

// First scene snaps in; later scenes crossfade.
function setScene(slug) {
  if (curSlug === null) loadInto(live, slug, () => { curSlug = slug; mix = mixTarget = live; });
  else crossfadeTo(slug);
}

function crossfadeTo(slug, restart) {
  const next = 1 - live;
  loadInto(next, slug, () => { live = next; curSlug = slug; mixTarget = next; }, restart);
}

// Load `slug` into element `idx`, then start it and run `onReady`. If the
// element already holds the clip, just (optionally) restart it.
function loadInto(idx, slug, onReady, restart) {
  const v = vids[idx];
  const begin = () => { if (restart) { try { v.currentTime = 0; } catch (e) {} } v.play().catch(() => {}); hasVideo = 1; hasTex = 1; imgMode = 0; onReady(); };
  if (v._slug === slug && v.readyState >= 2) { begin(); return; }
  v._slug = slug;
  v.onloadeddata = () => { v.onloadeddata = null; begin(); };
  v.onerror = () => { v.onerror = null; loadImageFallback(idx, slug); };
  v.src = `scenes/${slug}.mp4?v=` + Date.now();
  v.load();
}

// Rare path: no clip for this slug → try a still, else go procedural.
let imgMode = 0;
function loadImageFallback(idx, slug) {
  const tries = [`scenes/${slug}.png`, `scenes/${slug}.jpg`, 'scenes/default.png'];
  const attempt = (i) => {
    if (i >= tries.length) { hasVideo = 0; hasTex = 0; imgMode = 0; return; } // procedural
    const img = new Image();
    img.onload = () => {
      gl.bindTexture(gl.TEXTURE_2D, texs[idx]);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
      hasVideo = 0; hasTex = 1; imgMode = 1; live = idx; mix = mixTarget = idx; curSlug = slug;
    };
    img.onerror = () => attempt(i + 1);
    img.src = tries[i] + '?v=' + Date.now();
  };
  attempt(0);
}

function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(innerWidth * dpr);
  canvas.height = Math.floor(innerHeight * dpr);
  gl.viewport(0, 0, canvas.width, canvas.height);
}
addEventListener('resize', resize); resize();

// ───────────────────────── frame loop ─────────────────────────
let t0 = performance.now();
function frame(now) {
  const dt = Math.min(0.05, (now - (frame._last || now)) / 1000); frame._last = now;

  // beat phase + kick detection (phase wrap = downbeat)
  S.phase = framePhase(now);
  if (S.phase < lastFramePhase - 0.3) S.kick = 1;     // wrapped 1→0 → kick
  lastFramePhase = S.phase;
  if (S.beatConf > 0 && S.kick < 0.6) { S.kick = 1; }  // confirm via beat_active
  S.beatConf *= Math.exp(-dt / 0.05);
  S.kick *= Math.exp(-dt / 0.13);                      // decay kick pulse

  // ease hue toward target (avoid hard color jumps on key change)
  if (S.targetHue != null) {
    let dh = S.targetHue - S.hue; dh -= Math.round(dh);
    S.hue = (S.hue + dh * 0.02 + 1) % 1;
  }

  // crossfade easing (linear over CROSSFADE seconds)
  const step = dt / CROSSFADE;
  if (mix < mixTarget) mix = Math.min(mixTarget, mix + step);
  else if (mix > mixTarget) mix = Math.max(mixTarget, mix - step);
  const settled = Math.abs(mix - mixTarget) < 0.01;

  // seamless loop: before the live clip hits its end, crossfade it to itself
  // (restarted from 0) on the other buffer, hiding the wrap seam.
  if (hasVideo && imgMode === 0 && settled && !loopFading) {
    const lv = vids[live];
    if (lv.duration && isFinite(lv.duration) && lv.currentTime >= lv.duration - CROSSFADE) {
      loopFading = true;
      crossfadeTo(curSlug, true);
    }
  }
  if (settled) loopFading = false;

  // upload whichever buffers have fresh frames (video mode only)
  for (let i = 0; hasVideo && i < 2; i++) {
    if (vids[i].readyState >= 2 && (i === live || mix > 0.001 && mix < 0.999 || !settled)) {
      gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, texs[i]);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, vids[i]);
    }
  }

  gl.uniform2f(U.uRes, canvas.width, canvas.height);
  gl.uniform1f(U.uTime, (now - t0) / 1000);
  gl.uniform1f(U.uPhase, S.phase);
  gl.uniform1f(U.uKick, S.kick);
  gl.uniform1f(U.uEnergy, S.energy);
  gl.uniform1f(U.uHue, S.hue);
  gl.uniform1f(U.uHasTex, hasTex);
  gl.uniform1f(U.uIsVideo, hasVideo);
  gl.uniform1f(U.uMix, mix);
  gl.uniform1f(U.uDrop, S.drop);
  gl.uniform1f(U.uBreak, S.breakdown);
  gl.uniform1f(U.uBuild, S.buildup);
  gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, texs[0]); gl.uniform1i(U.uTex0, 0);
  gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, texs[1]); gl.uniform1i(U.uTex1, 1);
  gl.drawArrays(gl.TRIANGLES, 0, 3);

  if (hudOn) drawHud();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ───────────────────────── overlays + keys ─────────────────────────
const hud = document.getElementById('hud');
const np = document.getElementById('nowplaying');
let hudOn = false;
function drawHud() {
  hud.textContent =
    `VDJ TRETA   deck ${S.activeDeck}${S.playing ? '' : ' (idle)'}\n` +
    `bpm   ${S.bpm.toFixed(1)}\n` +
    `phase ${'▮'.repeat(Math.round(S.phase * 12)).padEnd(12, '·')}\n` +
    `kick  ${'█'.repeat(Math.round(S.kick * 12))}\n` +
    `energy${'█'.repeat(Math.round(S.energy * 12))}  ${(S.energy * 10).toFixed(1)}\n` +
    `key   ${S.keyStr}   hue ${(S.hue).toFixed(2)}\n` +
    `genre ${S.genre}\n` +
    `dir   ${S.energyDir}  ${S.drop > 0.1 ? 'DROP ' : ''}${S.breakdown > 0.3 ? 'BREAK ' : ''}${S.buildup > 0.3 ? 'BUILD' : ''}\n` +
    `scene ${hasVideo ? `video:${curSlug}` : hasTex ? 'image' : 'procedural'}${mix > 0.01 && mix < 0.99 ? ' ⇄' : ''}`;
}
let npTimer;
function showNowPlaying(title, artist) {
  if (!title) return;
  np.querySelector('.t').textContent = title;
  np.querySelector('.a').textContent = artist;
  np.classList.add('show');
  clearTimeout(npTimer);
  npTimer = setTimeout(() => np.classList.remove('show'), 8000);
}
addEventListener('keydown', (e) => {
  if (e.key === 'f' || e.key === 'F') {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen();
    else document.exitFullscreen();
  } else if (e.key === 'd' || e.key === 'D') {
    hudOn = !hudOn; hud.style.display = hudOn ? 'block' : 'none';
  } else if (e.key === '1') { S.drop = 1; }                 // manual drop burst
  else if (e.key === '2') { S._breakTarget = S._breakTarget ? 0 : 1; }  // toggle breakdown
  else if (e.key === '3') { S._buildTarget = S._buildTarget ? 0 : 1; }  // toggle buildup
});

// Chrome pauses <video> in a backgrounded tab; resume when we're visible again.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) vids.forEach(v => { if (v._slug) v.play().catch(() => {}); });
});

// ───────────────────────── start ─────────────────────────
(async () => { await pollLive(); anchorTime = performance.now(); })();
setInterval(pollLive, 50);    // 20 Hz beat/VU
setInterval(pollMeta, 1000);  // 1 Hz track/genre/key
pollMeta();

// Section logic on its own tick (setInterval survives a backgrounded tab; rAF
// doesn't). Eases drop/breakdown/buildup and flies between energy-state scenes.
let _logicLast = performance.now();
setInterval(() => {
  const n = performance.now(), d = Math.min(0.2, (n - _logicLast) / 1000); _logicLast = n;
  S.breakdown += ((S._breakTarget || 0) - S.breakdown) * Math.min(1, d * 2.5);
  S.buildup += ((S._buildTarget || 0) - S.buildup) * Math.min(1, d * 1.5);
  updateScene(n);   // choose scene from full-strength signals, THEN decay the drop
  S.drop *= Math.exp(-d / 0.55);                       // sharp burst, ~0.5s tail
}, 33);
// scene is chosen live by updateScene() in the frame loop (energy-state driven)
