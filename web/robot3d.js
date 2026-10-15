import * as THREE from '/vendor/three-0.186.0/three.module.js';

const colors = { confirmed: 0x8ae2cf, pending: 0xedbd78, retained: 0xaab8d4, unknown: 0x7f8b9a };

export function createRobotScene({ view, onSelect, onUnavailable, animationEnabled = true }) {
  const canvas = document.createElement('canvas');
  canvas.className = 'robot-canvas';
  canvas.dataset.rendererMode = '3d';
  canvas.setAttribute('aria-hidden', 'true');
  canvas.style.width = '100%';
  canvas.style.height = '420px';
  canvas.style.touchAction = 'none';
  let renderer = null;
  let context = null;
  let disposed = false;
  let frame = 0;
  let pointer = null;
  let latestView = view;
  let yaw = -0.28;
  let targetYaw = yaw;
  let elevation = 0.55;
  let targetElevation = elevation;
  let zoom = 1;
  let targetZoom = zoom;
  let lastTime = 0;
  let idleTime = 0;
  let pauseUntil = 0;
  let drawingWidth = 0;
  let drawingHeight = 0;
  let resizeObserver = null;
  let width = Math.min(960, window.innerWidth);
  let height = 420;
  let roaming = true;
  let metricsTime = 0;
  let sampleIndex = 0;
  let sampleCount = 0;
  const intervals = new Float32Array(180);
  const particleCount = 192;
  const particlePositions = new Float32Array(particleCount * 3);
  let particles = null;
  let roomWidth = 4, roomDepth = 4;
  const metrics = document.createElement('output');
  metrics.id = 'robot-frame-metrics';
  metrics.textContent = '帧率采样中';
  metrics.title = '最近可见 requestAnimationFrame 间隔；FPS / p95，不代表 GPU 或所有设备性能';
  const geometries = [];
  const materials = [];
  const listeners = [];
  const robots = new Map();
  const robotBatches = [];
  const stationBatches = [];
  const scene = new THREE.Scene();
  const world = new THREE.Group();
  scene.add(world);
  scene.background = new THREE.Color(0x111923);
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 300);
  scene.add(new THREE.AmbientLight(0xffffff, 2));
  const light = new THREE.DirectionalLight(0xffffff, 3);
  light.position.set(-6, 12, 8);
  scene.add(light);
  const raycaster = new THREE.Raycaster();
  raycaster.layers.enable(1);
  const mouse = new THREE.Vector2();
  // One distance-parameterized, tangent-continuous patrol shared by every home.
  const patrol = new THREE.CurvePath();
  const line = (x1, z1, x2, z2) => patrol.add(new THREE.LineCurve(new THREE.Vector2(x1, z1), new THREE.Vector2(x2, z2)));
  const arc = (x, z, start, end) => patrol.add(new THREE.EllipseCurve(x, z, 0.2, 0.2, start, end, true));
  line(0, 0.2, 0, 0.9); arc(0.2, 0.9, Math.PI, Math.PI / 2);
  line(0.2, 1.1, 0.6, 1.1); arc(0.6, 0.9, Math.PI / 2, 0);
  line(0.8, 0.9, 0.8, 0.2); arc(0.6, 0.2, 0, -Math.PI / 2);
  line(0.6, 0, 0.2, 0); arc(0.2, 0.2, -Math.PI / 2, -Math.PI);
  const patrolLength = patrol.getLength();
  const patrolPoint = new THREE.Vector2();

  function moveRobot(robot, delta) {
    const data = robot.userData;
    const pinned = roaming && data.sessionId === latestView.selected;
    const homeDistance = Math.hypot(data.home.x - robot.position.x, data.home.z - robot.position.z);
    const targetSpeed = pinned ? 0 : roaming ? data.cruise : homeDistance > 0.001 ? Math.min(data.cruise, Math.sqrt(4 * homeDistance)) : 0;
    const oldSpeed = data.speed;
    data.speed = pinned ? 0 : THREE.MathUtils.damp(oldSpeed, targetSpeed, 8, delta);
    let remaining = pinned ? 0 : targetSpeed * delta + (oldSpeed - targetSpeed) * (1 - Math.exp(-8 * delta)) / 8;
    const beforeX = robot.position.x, beforeZ = robot.position.z;
    if (!pinned && (roaming || homeDistance > 0.001)) {
      if (!roaming || data.joining) {
        const targetX = data.home.x, targetZ = data.home.z + (roaming ? 0.2 : 0);
        const dx = targetX - robot.position.x, dz = targetZ - robot.position.z, distance = Math.hypot(dx, dz);
        const step = Math.min(distance, remaining);
        if (distance > 0) { robot.position.x += dx / distance * step; robot.position.z += dz / distance * step; }
        remaining -= step;
        if (roaming && distance <= step) data.joining = false;
      }
      if (roaming && !data.joining) {
        data.patrolDistance = (data.patrolDistance + remaining) % patrolLength;
        patrol.getPoint(data.patrolDistance / patrolLength, patrolPoint);
        robot.position.x = data.home.x + patrolPoint.x; robot.position.z = data.home.z + patrolPoint.y;
      }
    }
    const dx = robot.position.x - beforeX, dz = robot.position.z - beforeZ, distance = Math.hypot(dx, dz);
    if (distance > 0.000001) {
      const heading = Math.atan2(dx, dz);
      const turn = Math.atan2(Math.sin(heading - robot.rotation.y), Math.cos(heading - robot.rotation.y));
      robot.rotation.y += THREE.MathUtils.clamp(turn * (1 - Math.exp(-20 * delta)), -6 * delta, 6 * delta);
      data.travel += distance;
    } else if (!roaming) {
      // A home turn must ignore full rotations accumulated during patrol laps.
      const turn = Math.atan2(-Math.sin(robot.rotation.y), Math.cos(robot.rotation.y));
      robot.rotation.y += THREE.MathUtils.clamp(turn * (1 - Math.exp(-10 * delta)), -6 * delta, 6 * delta);
    }
    data.walkBlend = THREE.MathUtils.damp(data.walkBlend, pinned ? 0 : data.speed / data.cruise, 12, delta);
    const swing = Math.sin(data.travel * 12) * 0.42 * data.walkBlend;
    robot.position.y = Math.abs(Math.sin(data.travel * 12)) * 0.025 * data.walkBlend;
    for (let index = 0; index < data.limbs.length; index++) data.limbs[index].rotation.x = swing * (index === 0 || index === 3 ? 1 : -1);
  }

  function listen(target, event, handler) {
    target.addEventListener(event, handler);
    listeners.push(() => target.removeEventListener(event, handler));
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    if (frame) cancelAnimationFrame(frame);
    resizeObserver?.disconnect();
    listeners.forEach(remove => remove());
    clearBatches(robotBatches);
    clearBatches(stationBatches);
    robots.clear();
    world.clear();
    geometries.forEach(geometry => geometry.dispose());
    materials.forEach(material => material.dispose());
    if (renderer) renderer.dispose();
    else if (context) {
      try { context.getExtension('WEBGL_lose_context')?.loseContext(); } catch { /* The failed canvas is detached. */ }
    }
  }
  function fail(code) {
    if (disposed) return;
    try { onUnavailable(code); } catch { /* A renderer callback cannot break the DOM app. */ }
    finally { dispose(); }
  }
  function moveParticles() {
    for (let i = 0; i < particleCount; i++) {
      const t = (idleTime * 0.18 + i / particleCount) % 1;
      particlePositions[i * 3] = (t * 2 - 1) * roomWidth;
      particlePositions[i * 3 + 1] = 0.15 + Math.sin(t * Math.PI) * 0.8;
      particlePositions[i * 3 + 2] = (i % 4 - 1.5) * roomDepth / 2;
    }
    particles.geometry.attributes.position.needsUpdate = true;
  }
  function schedule() {
    if (disposed || frame || document.hidden) return;
    frame = requestAnimationFrame(time => {
      frame = 0;
      if (disposed) return;
      const interval = lastTime ? time - lastTime : 0;
      const delta = Math.min(Math.max(0, interval / 1000), 0.1);
      lastTime = time;
      if (animationEnabled && interval > 0) {
        intervals[sampleIndex++ % intervals.length] = interval;
        sampleCount = Math.min(intervals.length, sampleCount + 1);
        if (time - metricsTime >= 1000) {
          const sorted = Array.from(intervals.subarray(0, sampleCount)).sort((a, b) => a - b);
          const mean = sorted.reduce((sum, value) => sum + value, 0) / sampleCount;
          metrics.textContent = `${Math.round(1000 / mean)} FPS · p95 ${sorted[Math.ceil(sampleCount * 0.95) - 1].toFixed(1)} ms`;
          metricsTime = time;
        }
      }
      const automatic = animationEnabled && !pointer && performance.now() >= pauseUntil;
      if (automatic) {
        idleTime += delta;
        // Decorative motion is independent of every session's activity evidence.
        const steps = Math.max(1, Math.ceil(delta * 60));
        for (let step = 0; step < steps; step++) for (const robot of robots.values()) moveRobot(robot, delta / steps);
        moveParticles();
      }
      const damping = 1 - Math.exp(-14 * delta);
      yaw += (targetYaw - yaw) * damping;
      elevation += (targetElevation - elevation) * damping;
      zoom += (targetZoom - zoom) * damping;
      if (Math.abs(targetYaw - yaw) < 0.0001) yaw = targetYaw;
      if (Math.abs(targetElevation - elevation) < 0.0001) elevation = targetElevation;
      if (Math.abs(targetZoom - zoom) < 0.0001) zoom = targetZoom;
      try { draw(); } catch { fail('draw_failed'); return; }
      if (animationEnabled || yaw !== targetYaw || elevation !== targetElevation || zoom !== targetZoom) schedule();
      else lastTime = 0;
    });
  }
  function draw() {
    const aspect = width / height;
    if (width !== drawingWidth || height !== drawingHeight) {
      drawingWidth = width;
      drawingHeight = height;
      camera.aspect = aspect;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
    }
    const verticalSlope = Math.tan(camera.fov * Math.PI / 360);
    const sinYaw = Math.sin(yaw), cosYaw = Math.cos(yaw);
    const sinElevation = Math.sin(elevation), cosElevation = Math.cos(elevation);
    let distance = 0;
    // Fit the eight room bounds to both frustum axes, including each corner's depth.
    for (let sx = -1; sx <= 1; sx += 2) for (let sz = -1; sz <= 1; sz += 2) for (let top = 0; top <= 1; top++) {
      const x = sx * roomWidth, z = sz * roomDepth, y = (top ? 2.6 : -0.2) - 1.05;
      const depth = sinYaw * cosElevation * x + sinElevation * y + cosYaw * cosElevation * z;
      const horizontal = cosYaw * x - sinYaw * z;
      const vertical = -sinYaw * sinElevation * x + cosElevation * y - cosYaw * sinElevation * z;
      distance = Math.max(distance, Math.abs(horizontal) / (verticalSlope * aspect) + depth, Math.abs(vertical) / verticalSlope + depth);
    }
    distance *= 1.08 * zoom;
    camera.position.set(sinYaw * cosElevation * distance,
      1.05 + sinElevation * distance, cosYaw * cosElevation * distance);
    camera.lookAt(0, 1.05, 0);
    world.updateMatrixWorld(true);
    uploadBatches(robotBatches);
    renderer.render(scene, camera);
    if (context.isContextLost()) throw new Error('WebGL context lost during drawing');
  }
  function clearBatches(batches) {
    for (const { mesh } of batches) { scene.remove(mesh); mesh.dispose(); }
    batches.length = 0;
  }
  function uploadBatches(batches) {
    for (const { mesh, parts } of batches) {
      parts.forEach((part, index) => mesh.setMatrixAt(index, part.matrixWorld));
      mesh.instanceMatrix.needsUpdate = true;
      mesh.computeBoundingSphere();
    }
  }
  function rebuildBatches(parts, batches, dynamic) {
    clearBatches(batches);
    const groups = new Map();
    for (const part of parts) {
      const key = `${part.geometry.uuid}:${part.material.uuid}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(part);
    }
    for (const parts of groups.values()) {
      const mesh = new THREE.InstancedMesh(parts[0].geometry, parts[0].material, parts.length);
      if (dynamic) mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      scene.add(mesh);
      batches.push({ mesh, parts });
    }
    uploadBatches(batches);
  }
  try {
    try { context = canvas.getContext('webgl2', { alpha: false, antialias: true }); }
    catch { throw Object.assign(new Error('WebGL2 context creation failed'), { code: 'webgl_unavailable' }); }
    if (!context) throw Object.assign(new Error('WebGL2 unavailable'), { code: 'webgl_unavailable' });
    renderer = new THREE.WebGLRenderer({ canvas, context, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    const box = new THREE.BoxGeometry(1, 1, 1);
    geometries.push(box);
    const antenna = new THREE.CylinderGeometry(0.05, 0.05, 0.28, 8);
    geometries.push(antenna);
    const joint = new THREE.SphereGeometry(0.1, 10, 8);
    geometries.push(joint);
    const pad = new THREE.CylinderGeometry(0.9, 0.9, 0.05, 24);
    geometries.push(pad);
    const byCategory = {};
    for (const [category, color] of Object.entries(colors)) {
      byCategory[category] = new THREE.MeshStandardMaterial({ color, roughness: 0.7, metalness: 0.12 });
      materials.push(byCategory[category]);
    }
    const dark = new THREE.MeshStandardMaterial({ color: 0x17212c, roughness: 0.9 });
    materials.push(dark);
    const eyes = new THREE.MeshBasicMaterial({ color: 0xe5edf5 });
    materials.push(eyes);
    const selected = new THREE.MeshBasicMaterial({ color: 0xe5edf5, wireframe: true });
    materials.push(selected);
    const territory = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-1.4, -0.035, -1.55), new THREE.Vector3(1.8, -0.035, -1.55),
      new THREE.Vector3(1.8, -0.035, 2.05), new THREE.Vector3(-1.4, -0.035, 2.05)
    ]);
    const boundary = new THREE.LineBasicMaterial({ color: 0x527b91, transparent: true, opacity: 0.7 });
    const selectedBoundary = new THREE.LineBasicMaterial({ color: 0xe5edf5 });
    geometries.push(territory); materials.push(boundary, selectedBoundary);
    const floorGeometry = new THREE.BoxGeometry(2, 0.12, 2);
    const floorMaterial = new THREE.MeshStandardMaterial({ color: 0x233542, roughness: 0.95 });
    geometries.push(floorGeometry);
    materials.push(floorMaterial);
    const floor = new THREE.Mesh(floorGeometry, floorMaterial);
    floor.position.y = -0.13;
    scene.add(floor);
    const grid = new THREE.GridHelper(2, 20, 0x4b6776, 0x304958);
    grid.position.y = -0.064;
    geometries.push(grid.geometry);
    materials.push(...(Array.isArray(grid.material) ? grid.material : [grid.material]));
    scene.add(grid);
    const neon = new THREE.MeshBasicMaterial({ color: 0x68cddb });
    const screen = new THREE.MeshBasicMaterial({ color: 0x416989 });
    materials.push(neon, screen);
    const room = new THREE.Group();
    scene.add(room);
    function block(group, material, x, y, z, sx, sy, sz) {
      const mesh = new THREE.Mesh(box, material);
      mesh.position.set(x, y, z); mesh.scale.set(sx, sy, sz); group.add(mesh);
      return mesh;
    }
    for (const side of [-1, 1]) {
      block(room, dark, side, 0.18, 0, 0.06, 0.5, 2);
      block(room, dark, 0, 0.18, side, 2, 0.5, 0.06);
      block(room, neon, side, 0.46, 0, 0.025, 0.025, 2);
      block(room, neon, 0, 0.46, side, 2, 0.025, 0.025);
    }
    const control = new THREE.Group();
    block(control, dark, 0, 0.5, 0, 2.8, 1, 0.8);
    block(control, screen, 0, 1.2, -0.05, 2.4, 0.8, 0.08);
    for (let i = 0; i < 4; i++) block(control, neon, (i - 1.5) * 0.5, 1.22, 0, 0.35, 0.04, 0.04);
    scene.add(control);
    const particleGeometry = new THREE.BufferGeometry();
    particleGeometry.setAttribute('position', new THREE.BufferAttribute(particlePositions, 3));
    const particleMaterial = new THREE.PointsMaterial({ color: 0x7cdded, size: 0.075, transparent: true, opacity: 0.6, depthWrite: false, blending: THREE.AdditiveBlending });
    geometries.push(particleGeometry); materials.push(particleMaterial);
    particles = new THREE.Points(particleGeometry, particleMaterial);
    particles.frustumCulled = false;
    scene.add(particles);
    const stations = new Map();
    function makeRobot(sessionId) {
      const robot = new THREE.Group();
      robot.userData.sessionId = sessionId;
      robot.userData.colored = [];
      robot.userData.limbs = [];
      robot.userData.parts = [];
      function part(geometry, material, x, y, z, sx = 1, sy = 1, sz = 1, colored = false) {
        const mesh = new THREE.Mesh(geometry, material);
        mesh.position.set(x, y, z);
        mesh.scale.set(sx, sy, sz);
        robot.add(mesh);
        // Keep the original hierarchy for motion and picking; camera draws instances.
        if (geometry !== pad) { mesh.layers.set(1); robot.userData.parts.push(mesh); }
        if (colored) robot.userData.colored.push(mesh);
        return mesh;
      }
      const body = byCategory.unknown;
      part(box, body, 0, 1.8, 0, 0.9, 0.66, 0.62, true);
      part(box, dark, 0, 1.82, 0.32, 0.65, 0.31, 0.04);
      part(box, eyes, -0.17, 1.84, 0.35, 0.1, 0.08, 0.04);
      part(box, eyes, 0.17, 1.84, 0.35, 0.1, 0.08, 0.04);
      part(box, body, 0, 1.05, 0, 0.75, 0.74, 0.5, true);
      for (const side of [-1, 1]) {
        const arm = part(box, body, side * 0.58, 1.04, 0, 0.18, 0.6, 0.25, true);
        part(joint, dark, side * 0.47, 1.29, 0);
        const leg = part(box, body, side * 0.23, 0.38, 0, 0.23, 0.47, 0.3, true);
        const foot = part(box, dark, side * 0.23, 0.1, 0.06, 0.3, 0.15, 0.46);
        const armPivot = new THREE.Group(), legPivot = new THREE.Group();
        armPivot.position.set(side * 0.58, 1.29, 0); legPivot.position.set(side * 0.23, 0.65, 0);
        arm.position.set(0, -0.25, 0); leg.position.set(0, -0.27, 0); foot.position.set(0, -0.55, 0.06);
        armPivot.add(arm); legPivot.add(leg, foot); robot.add(armPivot, legPivot);
        robot.userData.limbs.push(armPivot, legPivot);
      }
      part(antenna, body, 0, 2.28, 0, 1, 1, 1, true);
      part(joint, eyes, 0, 2.47, 0);
      robot.userData.selection = part(pad, selected, 0, 0.025, 0);
      world.add(robot);
      return robot;
    }
    function update(nextView) {
      if (disposed) return;
      latestView = nextView;
      const garden = nextView.theme === 'garden';
      scene.background.setHex(garden ? 0xd4e4c8 : 0x111923);
      floorMaterial.color.setHex(garden ? 0xa6c48a : 0x233542);
      dark.color.setHex(garden ? 0x947750 : 0x17212c);
      neon.color.setHex(garden ? 0xe3bc70 : 0x68cddb);
      screen.color.setHex(garden ? 0x5e7c69 : 0x416989);
      boundary.color.setHex(garden ? 0x759164 : 0x527b91);
      selectedBoundary.color.setHex(garden ? 0x4e6338 : 0xe5edf5);
      byCategory.unknown.color.setHex(garden ? 0x8fa781 : colors.unknown);
      for (const material of Array.isArray(grid.material) ? grid.material : [grid.material]) material.color.setHex(garden ? 0xadc39a : 0xffffff);
      let batchesDirty = false;
      const present = new Set(nextView.records.map(record => record.sessionId));
      for (const [sessionId, robot] of robots) {
        if (!present.has(sessionId)) {
          world.remove(robot); robots.delete(sessionId);
          scene.remove(stations.get(sessionId)); stations.delete(sessionId);
          batchesDirty = true;
        }
      }
      const occupied = new Set(Array.from(robots.values(), robot => robot.userData.slot));
      nextView.records.forEach(record => {
        let robot = robots.get(record.sessionId);
        if (!robot) {
          batchesDirty = true;
          let slot = 0;
          while (occupied.has(slot)) slot++;
          occupied.add(slot);
          robot = makeRobot(record.sessionId); robots.set(record.sessionId, robot);
          const row = Math.floor(slot / 4);
          const home = { x: ((slot % 4) - 1.5) * 3.4, z: (row % 2 ? -Math.ceil(row / 2) : row / 2) * 3.8 };
          Object.assign(robot.userData, { slot, home, travel: slot * 0.37, cruise: 0.75 - (slot % 5) * 0.02, speed: 0, walkBlend: 0, joining: true, patrolDistance: 0 });
          robot.position.set(home.x, 0, home.z);
          const station = new THREE.Group();
          station.position.set(home.x, 0, home.z);
          block(station, dark, 0.2, -0.0525, 0.25, 3.2, 0.025, 3.6);
          station.userData.ownerKey = record.sessionId;
          station.userData.boundary = new THREE.LineLoop(territory, boundary);
          station.add(station.userData.boundary);
          block(station, dark, 0, 0.55, -0.9, 1.5, 0.14, 0.5);
          block(station, dark, 0, 0.25, -0.9, 0.15, 0.5, 0.25);
          block(station, screen, 0, 0.95, -1.1, 1, 0.65, 0.05);
          station.userData.marker = block(station, byCategory.unknown, 0, 0.01, 0.75, 1.6, 0.03, 0.08);
          station.userData.parts = [];
          station.traverse(part => { if (part.isMesh) { part.layers.set(1); station.userData.parts.push(part); } });
          scene.add(station); stations.set(record.sessionId, station);
        }
        const material = byCategory[record.category] || byCategory.unknown;
        if (robot.userData.colored[0].material !== material) batchesDirty = true;
        robot.userData.colored.forEach(mesh => { mesh.material = material; });
        stations.get(record.sessionId).userData.marker.material = material;
        stations.get(record.sessionId).userData.boundary.material = record.sessionId === nextView.selected ? selectedBoundary : boundary;
        robot.userData.selection.visible = record.sessionId === nextView.selected;
      });
      roomWidth = Math.max(7, ...Array.from(robots.values(), robot => Math.abs(robot.userData.home.x) + 2));
      roomDepth = Math.max(5, ...Array.from(robots.values(), robot => Math.abs(robot.userData.home.z) + 3));
      floor.scale.set(roomWidth, 1, roomDepth);
      grid.scale.set(roomWidth, 1, roomDepth);
      room.scale.set(roomWidth, 1, roomDepth);
      control.position.z = -roomDepth + 1;
      if (batchesDirty) {
        world.updateMatrixWorld(true);
        for (const station of stations.values()) station.updateMatrixWorld(true);
        rebuildBatches(Array.from(robots.values()).flatMap(robot => robot.userData.parts), robotBatches, true);
        rebuildBatches(Array.from(stations.values()).flatMap(station => station.userData.parts), stationBatches, false);
      }
      moveParticles();
      schedule();
    }
    function pauseAutomatic() { pauseUntil = performance.now() + 250; }
    listen(canvas, 'webglcontextlost', event => { event.preventDefault(); fail('context_lost'); });
    listen(canvas, 'pointerdown', event => {
      if (disposed || event.button !== 0 || !event.isPrimary) return;
      pauseAutomatic();
      // A click freezes residual damping so its target does not move beneath the pointer.
      targetYaw = yaw; targetElevation = elevation; targetZoom = zoom;
      pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, lastX: event.clientX, lastY: event.clientY, moved: false };
      canvas.setPointerCapture(event.pointerId);
    });
    listen(canvas, 'pointermove', event => {
      if (!pointer || pointer.id !== event.pointerId) return;
      pauseAutomatic();
      if (Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) > 4) pointer.moved = true;
      if (pointer.moved) {
        targetYaw -= (event.clientX - pointer.lastX) * 0.008;
        targetElevation = Math.max(0.18, Math.min(1.15, targetElevation + (event.clientY - pointer.lastY) * 0.006));
        schedule();
      }
      pointer.lastX = event.clientX;
      pointer.lastY = event.clientY;
    });
    listen(canvas, 'pointerup', event => {
      if (!pointer || pointer.id !== event.pointerId) return;
      pauseAutomatic();
      const moved = pointer.moved || Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) > 4;
      pointer = null;
      if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
      if (moved || disposed) return;
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      mouse.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
      raycaster.setFromCamera(mouse, camera);
      const hit = raycaster.intersectObjects(Array.from(robots.values()), true)[0];
      let target = hit?.object;
      while (target && !target.userData.sessionId) target = target.parent;
      if (target && latestView.records.some(record => record.sessionId === target.userData.sessionId)) {
        try { onSelect(target.userData.sessionId); } catch { /* Selection is owned by the app. */ }
      }
    });
    listen(canvas, 'pointercancel', () => { pointer = null; });
    listen(canvas, 'lostpointercapture', () => { pointer = null; });
    listen(canvas, 'wheel', event => {
      event.preventDefault();
      pauseAutomatic();
      targetZoom = Math.max(0.75, Math.min(1.8, targetZoom * Math.exp(Math.max(-100, Math.min(100, event.deltaY)) * 0.002)));
      schedule();
    });
    function resize(nextWidth = canvas.clientWidth, nextHeight = canvas.clientHeight) {
      width = Math.max(1, Math.floor(nextWidth || Math.min(960, window.innerWidth)));
      height = Math.max(1, Math.floor(nextHeight || 420));
      schedule();
    }
    listen(window, 'resize', () => resize());
    listen(document, 'visibilitychange', () => {
      lastTime = 0;
      if (document.hidden) { pointer = null; metrics.textContent = '后台已暂停'; }
      if (document.hidden && frame) { cancelAnimationFrame(frame); frame = 0; }
      else schedule();
    });
    resizeObserver = new ResizeObserver(entries => {
      const rect = entries[0]?.contentRect;
      resize(rect?.width, rect?.height);
    });
    resizeObserver.observe(canvas);
    update(view);
    if (frame) { cancelAnimationFrame(frame); frame = 0; }
    try { draw(); } catch { throw Object.assign(new Error('First 3D draw failed'), { code: 'first_draw_failed' }); }
    schedule();
    return {
      canvas, metrics, update, dispose,
      setRoaming(enabled) {
        if (disposed) return;
        roaming = enabled;
        if (enabled) for (const robot of robots.values()) { robot.userData.joining = true; robot.userData.patrolDistance = 0; }
        pauseUntil = 0;
        schedule();
      },
      rotate(delta) {
        if (disposed) return;
        pauseAutomatic();
        targetYaw = delta === 0 ? yaw + Math.atan2(Math.sin(-0.28 - yaw), Math.cos(-0.28 - yaw)) : targetYaw + delta;
        if (delta === 0) { targetElevation = 0.55; targetZoom = 1; }
        schedule();
      },
      setAnimation(enabled) {
        if (disposed) return;
        animationEnabled = enabled;
        lastTime = 0;
        sampleCount = 0; sampleIndex = 0; metricsTime = 0;
        metrics.textContent = enabled ? '帧率采样中' : '动画已暂停';
        if (!enabled) { targetYaw = yaw; targetElevation = elevation; targetZoom = zoom; }
        schedule();
      }
    };
  } catch (error) {
    dispose();
    throw Object.assign(new Error(error.message || '3D initialization failed'), {
      code: ['webgl_unavailable', 'first_draw_failed'].includes(error.code) ? error.code : 'initialization_failed'
    });
  }
}
