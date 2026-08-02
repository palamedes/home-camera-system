/*
 * Client-side fisheye dewarping.
 *
 * A 360 camera renders a circular image on a square sensor. This module takes
 * the already-playing live <video> as a WebGL texture and reprojects it into
 * flat ("virtual PTZ") views or a panorama — entirely on the client GPU, so
 * the server does nothing extra and the WebRTC pipeline in live.js is
 * untouched. The <video> keeps receiving the stream (covered, not hidden) and
 * we sample it each frame.
 *
 * One GL context, one texture: the decoded frame is uploaded once per frame no
 * matter how many virtual views are drawn — each view is just another
 * scissored viewport reusing the texture with a different yaw/pitch/fov.
 *
 * Projection: fisheye (equidistant or equisolid) <-> rectilinear. For each
 * output pixel we build a virtual pinhole ray, rotate it by yaw/pitch, then map
 * its direction back to a fisheye sample coordinate.
 */

(function () {
  const VERT = `
    attribute vec2 a_pos;
    varying vec2 v_ndc;
    void main() {
      v_ndc = a_pos;
      gl_Position = vec4(a_pos, 0.0, 1.0);
    }`;

  // Shared uniforms describing the lens; the two fragment shaders differ only
  // in how they turn an output pixel into a (theta, phi) direction.
  const LENS_UNIFORMS = `
    uniform sampler2D u_tex;
    uniform vec2  u_center;     // circle center in texture UV
    uniform float u_radius;     // circle radius in x-UV units
    uniform vec2  u_texAspect;  // (1, texW/texH) keeps the sampled radius round
    uniform float u_fovMax;     // lens half-FoV (radians)
    uniform int   u_proj;       // 0 = equidistant, 1 = equisolid
    uniform float u_chirality;  // +1 ceiling mount, -1 desk (mirror)
    uniform float u_roll;       // azimuth offset to level the horizon
    vec4 sampleFisheye(float theta, float phi) {
      float rNorm = (u_proj == 1)
        ? sin(theta * 0.5) / sin(u_fovMax * 0.5)
        : theta / u_fovMax;
      if (rNorm > 1.0) return vec4(0.0, 0.0, 0.0, 1.0);
      float a = phi + u_roll;
      vec2 uv = u_center + rNorm * u_radius * vec2(cos(a), sin(a)) * u_texAspect;
      if (u_chirality < 0.0) uv.x = 2.0 * u_center.x - uv.x;
      return texture2D(u_tex, uv);
    }`;

  const FRAG_RECT = `
    precision highp float;
    varying vec2 v_ndc;
    ${LENS_UNIFORMS}
    uniform float u_fov;      // virtual horizontal FoV (radians)
    uniform float u_aspect;   // viewport width/height
    uniform mat3  u_rot;      // yaw/pitch rotation
    uniform float u_rotate;   // roll of the rendered picture around its view axis
    void main() {
      float t = tan(u_fov * 0.5);
      vec3 vd = vec3(v_ndc.x * t * u_aspect, v_ndc.y * t, 1.0);
      // Rotate the output image around the view axis (cosmetic straighten).
      float cr = cos(u_rotate), sr = sin(u_rotate);
      vd = vec3(vd.x * cr - vd.y * sr, vd.x * sr + vd.y * cr, vd.z);
      // u_rot is a look-at basis (right, up, forward) — no gimbal roll.
      vec3 dir = normalize(u_rot * vd);
      float theta = acos(clamp(dir.z, -1.0, 1.0));
      float phi = atan(dir.y, dir.x);
      gl_FragColor = sampleFisheye(theta, phi);
    }`;

  const FRAG_PANO = `
    precision highp float;
    varying vec2 v_ndc;
    ${LENS_UNIFORMS}
    uniform float u_phi0;
    uniform float u_phiRange;
    uniform float u_theta0;
    uniform float u_thetaRange;
    void main() {
      vec2 uv01 = v_ndc * 0.5 + 0.5;
      float phi = u_phi0 + uv01.x * u_phiRange;
      float theta = u_theta0 + (1.0 - uv01.y) * u_thetaRange;
      gl_FragColor = sampleFisheye(theta, phi);
    }`;

  const D2R = Math.PI / 180;

  const DEFAULT_CALIB = {
    cx: 0.5, cy: 0.5, radius: 0.5,
    fovMax: 90 * D2R,      // 180-degree lens; raise if the far edges go black
    proj: 1,               // equisolid (Reolink)
    chirality: 1,          // ceiling mount
    roll: 0,               // azimuth of the fisheye sampling (scene geometry)
    rotate: 0,             // roll of the rendered picture (cosmetic)
  };

  function compile(gl, type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      console.error('fisheye shader:', gl.getShaderInfoLog(sh));
      return null;
    }
    return sh;
  }

  function buildProgram(gl, fragSrc) {
    const vs = compile(gl, gl.VERTEX_SHADER, VERT);
    const fs = compile(gl, gl.FRAGMENT_SHADER, fragSrc);
    if (!vs || !fs) return null;
    const p = gl.createProgram();
    gl.attachShader(p, vs);
    gl.attachShader(p, fs);
    gl.bindAttribLocation(p, 0, 'a_pos');
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      console.error('fisheye link:', gl.getProgramInfoLog(p));
      return null;
    }
    return p;
  }

  // Look-at basis (right, up, forward) as a column-major mat3.
  //
  // yaw pans around the fisheye's optical axis (+Z); pitch is elevation, 0 at
  // the equator (walls/horizon) rising toward +Z (nadir, straight down for a
  // ceiling mount). The "up" of the view is derived from the axis rather than
  // a fixed Euler order, so the horizon stays level and dragging never rolls
  // the picture — the failure mode of the old yaw*pitch matrix near the pole.
  function orientation(yaw, pitch) {
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const f = [cp * sy, cp * cy, sp];            // forward
    // right = normalize(cross(+Z, forward)) = normalize((-f.y, f.x, 0))
    let rx = -f[1], ry = f[0], rz = 0;
    const rl = Math.hypot(rx, ry, rz) || 1;
    rx /= rl; ry /= rl; rz /= rl;
    // up = cross(right, forward) — points toward the axis so verticals are upright
    const ux = ry * f[2] - rz * f[1];
    const uy = rz * f[0] - rx * f[2];
    const uz = rx * f[1] - ry * f[0];
    return new Float32Array([rx, ry, rz, ux, uy, uz, f[0], f[1], f[2]]);
  }

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function loadCalib(id) {
    try {
      const raw = localStorage.getItem('fisheye:' + id);
      if (raw) return { ...DEFAULT_CALIB, ...JSON.parse(raw) };
    } catch (_) {}
    return { ...DEFAULT_CALIB };
  }
  function saveCalib(id, c) {
    try { localStorage.setItem('fisheye:' + id, JSON.stringify(c)); } catch (_) {}
  }

  function initFisheye(video, canvas, opts) {
    const gl = canvas.getContext('webgl', { antialias: false, alpha: false });
    if (!gl) return null;  // caller keeps the raw <video> visible

    const id = opts.cameraId || 'cam';
    let mode = opts.mode || 'single';           // single | dual | quad | pano
    let interactive = opts.interactive !== false;  // runtime-toggleable
    const persist = opts.persistCalib !== false;
    let activeView = 0;                          // which view single-mode shows
    let calib = opts.calib ? { ...DEFAULT_CALIB, ...opts.calib } : loadCalib(id);

    const progRect = buildProgram(gl, FRAG_RECT);
    const progPano = buildProgram(gl, FRAG_PANO);
    if (!progRect || !progPano) return null;

    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);

    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

    // Virtual views. Quad uses four; dual the first two; single shows the
    // active one. Seeded from opts.view when provided (fixed dashboard tiles).
    // Default pitch 0 = looking at the horizon, so a wall/door is centered and
    // upright rather than skewed.
    const views = [
      { yaw: 0, pitch: 0, fov: 90 * D2R },
      { yaw: Math.PI / 2, pitch: 0, fov: 90 * D2R },
      { yaw: Math.PI, pitch: 0, fov: 90 * D2R },
      { yaw: -Math.PI / 2, pitch: 0, fov: 90 * D2R },
    ];
    if (opts.view) views[0] = { ...views[0], ...opts.view };

    function viewports() {
      const w = canvas.width, h = canvas.height;
      if (mode === 'quad') {
        const hw = (w / 2) | 0, hh = (h / 2) | 0;
        return [
          { x: 0, y: hh, w: hw, h: hh, view: 0 },
          { x: hw, y: hh, w: w - hw, h: hh, view: 1 },
          { x: 0, y: 0, w: hw, h: h - hh, view: 2 },
          { x: hw, y: 0, w: w - hw, h: h - hh, view: 3 },
        ];
      }
      if (mode === 'dual') {
        const hw = (w / 2) | 0;
        return [
          { x: 0, y: 0, w: hw, h: h, view: 0 },
          { x: hw, y: 0, w: w - hw, h: h, view: 1 },
        ];
      }
      return [{ x: 0, y: 0, w: w, h: h, view: activeView }];
    }

    function setLensUniforms(gl, loc) {
      const texW = video.videoWidth || 1, texH = video.videoHeight || 1;
      gl.uniform1i(loc.tex, 0);
      gl.uniform2f(loc.center, calib.cx, calib.cy);
      gl.uniform1f(loc.radius, calib.radius);
      gl.uniform2f(loc.texAspect, 1.0, texW / texH);
      gl.uniform1f(loc.fovMax, calib.fovMax);
      gl.uniform1i(loc.proj, calib.proj);
      gl.uniform1f(loc.chirality, calib.chirality);
      gl.uniform1f(loc.roll, calib.roll);
    }

    // Uniform locations per program (fetched once).
    const rectLoc = locs(progRect, ['tex', 'center', 'radius', 'texAspect',
      'fovMax', 'proj', 'chirality', 'roll', 'fov', 'aspect', 'rot', 'rotate']);
    const panoLoc = locs(progPano, ['tex', 'center', 'radius', 'texAspect',
      'fovMax', 'proj', 'chirality', 'roll', 'phi0', 'phiRange', 'theta0', 'thetaRange']);

    function locs(prog, names) {
      const out = {};
      for (const n of names) out[n] = gl.getUniformLocation(prog, 'u_' + n);
      return out;
    }

    function bindQuad(prog) {
      gl.useProgram(prog);
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    }

    function renderFrame() {
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
      gl.enable(gl.SCISSOR_TEST);

      if (mode === 'pano') {
        bindQuad(progPano);
        setLensUniforms(gl, panoLoc);
        gl.uniform1f(panoLoc.phi0, -Math.PI + calib.roll);
        gl.uniform1f(panoLoc.phiRange, 2 * Math.PI);
        gl.uniform1f(panoLoc.theta0, 20 * D2R);
        gl.uniform1f(panoLoc.thetaRange, Math.min(calib.fovMax, 80 * D2R) - 20 * D2R);
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.scissor(0, 0, canvas.width, canvas.height);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        return;
      }

      bindQuad(progRect);
      setLensUniforms(gl, rectLoc);
      gl.uniform1f(rectLoc.rotate, calib.rotate || 0);
      for (const vp of viewports()) {
        const v = views[vp.view];
        gl.viewport(vp.x, vp.y, vp.w, vp.h);
        gl.scissor(vp.x, vp.y, vp.w, vp.h);
        gl.uniformMatrix3fv(rectLoc.rot, false, orientation(v.yaw, v.pitch));
        gl.uniform1f(rectLoc.fov, v.fov);
        gl.uniform1f(rectLoc.aspect, vp.w / vp.h);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      }
    }

    // ---- render loop --------------------------------------------------

    let running = true;
    let rafId = null, rvfcId = null;
    const hasRVFC = typeof video.requestVideoFrameCallback === 'function';

    function tick() {
      if (!running) return;
      // Re-check size each frame so the canvas snaps to full resolution as
      // soon as it's shown/laid out, instead of rendering tiny until a manual
      // resize. resize() early-returns when nothing changed.
      resize();
      if (video.readyState >= 2 && video.videoWidth) renderFrame();
      schedule();
    }
    function schedule() {
      if (hasRVFC) rvfcId = video.requestVideoFrameCallback(tick);
      else rafId = requestAnimationFrame(tick);
    }

    function resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.round(canvas.clientWidth * dpr);
      const h = Math.round(canvas.clientHeight * dpr);
      if (w && h && (canvas.width !== w || canvas.height !== h)) {
        canvas.width = w; canvas.height = h;
      }
    }
    window.addEventListener('resize', resize);
    resize();
    schedule();

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { running = false; }
      else if (!running) { running = true; schedule(); }
    });

    // ---- interaction: drag pans/tilts, wheel zooms, dbl-click focuses -

    // Which view index sits under a client point, for the current layout.
    function indexAt(clientX, clientY) {
      const r = canvas.getBoundingClientRect();
      const left = (clientX - r.left) < r.width / 2;
      const top = (clientY - r.top) < r.height / 2;
      if (mode === 'quad') return (top ? 0 : 2) + (left ? 0 : 1);
      if (mode === 'dual') return left ? 0 : 1;
      return activeView;
    }

    // Handlers are always registered but no-op unless `interactive` is on, so
    // edit mode can be toggled at runtime without rebuilding the renderer.
    let drag = null;
    canvas.addEventListener('pointerdown', e => {
      if (!interactive) return;
      drag = { x: e.clientX, y: e.clientY, view: views[indexAt(e.clientX, e.clientY)] };
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', e => {
      if (!interactive || !drag) return;
      const v = drag.view;
      const k = v.fov / canvas.clientHeight;   // radians per pixel — zoom-aware
      // Drag right pans right; drag down tilts the view downward.
      v.yaw += (e.clientX - drag.x) * k;
      v.pitch = clamp(v.pitch + (e.clientY - drag.y) * k, -0.35, Math.PI / 2 - 0.05);
      drag.x = e.clientX; drag.y = e.clientY;
    });
    const endDrag = () => { drag = null; };
    canvas.addEventListener('pointerup', endDrag);
    canvas.addEventListener('pointercancel', endDrag);
    canvas.addEventListener('wheel', e => {
      if (!interactive) return;
      e.preventDefault();
      views[indexAt(e.clientX, e.clientY)].fov =
        clamp(views[indexAt(e.clientX, e.clientY)].fov * (e.deltaY > 0 ? 1.06 : 0.94),
          12 * D2R, 150 * D2R);
    }, { passive: false });
    // Double-click a tile in a multi-view mode to focus it full-frame.
    canvas.addEventListener('dblclick', e => {
      if (!interactive) return;
      if (mode === 'quad' || mode === 'dual') {
        activeView = indexAt(e.clientX, e.clientY);
        mode = 'single';
        resize();
        if (opts.onModeChange) opts.onModeChange('single');
      }
    });

    return {
      setMode(m) { mode = m; resize(); },
      getMode() { return mode; },
      setInteractive(v) { interactive = !!v; },
      activeIndex() { return activeView; },
      activeView() { return { ...views[activeView] }; },
      setView(v) { views[activeView] = { ...views[activeView], ...v }; },
      setCalib(patch) { calib = { ...calib, ...patch }; if (persist) saveCalib(id, calib); },
      getCalib() { return { ...calib }; },
      resetCalib() { calib = { ...DEFAULT_CALIB }; if (persist) saveCalib(id, calib); },
      destroy() {
        running = false;
        if (rafId) cancelAnimationFrame(rafId);
        if (rvfcId && video.cancelVideoFrameCallback) video.cancelVideoFrameCallback(rvfcId);
        window.removeEventListener('resize', resize);
      },
    };
  }

  window.initFisheye = initFisheye;

  /*
   * Dashboard virtual-camera tiles: pull the parent fisheye's stream into a
   * hidden <video> (WebRTC, via live.js) and render one fixed dewarp view into
   * the tile's <canvas>. No interaction — a virtual camera is a saved angle.
   */
  function initVirtualTiles(root) {
    (root || document).querySelectorAll('[data-vcam]').forEach(el => {
      const video = el.querySelector('video');
      const canvas = el.querySelector('canvas');
      if (!video || !canvas) return;
      let view = {}, calib = {};
      try { view = JSON.parse(el.dataset.view || '{}'); } catch (_) {}
      try { calib = JSON.parse(el.dataset.calib || '{}'); } catch (_) {}
      if (typeof initLiveTile === 'function') {
        initLiveTile(video, { stream: el.dataset.stream });
      }
      initFisheye(video, canvas, {
        mode: 'single', interactive: false, persistCalib: false, view, calib,
      });
    });
  }

  window.initVirtualTiles = initVirtualTiles;
})();
