'use strict';

(() => {
  const colors = { confirmed: '#8ae2cf', pending: '#edbd78', retained: '#aab8d4', unknown: '#7f8b9a' };

  function create({ stage, status, modeButtons, rotateButtons, onSelect }) {
    const pixel = document.createElement('canvas');
    pixel.className = 'robot-canvas';
    pixel.dataset.rendererMode = 'pixel';
    pixel.setAttribute('aria-hidden', 'true');
    let context = null;
    try { context = pixel.getContext('2d'); } catch { /* DOM remains available. */ }
    let view = { records: [], selected: null };
    let requestedMode = '3d';
    let actualMode = 'text';
    let generation = 0;
    let scene = null;
    let frame = 0;
    let disposed = false;
    let hitboxes = [];
    let restoreCanvas = null;
    let restoreListener = null;
    const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
    let animationPaused = motionPreference.matches;
    let roamingEnabled = true;
    let savedObservationMotion = null;
    const motionButton = document.createElement('button');
    motionButton.id = 'robot-motion-toggle';
    motionButton.type = 'button';
    motionButton.title = '装饰动画不表示会话正在工作';
    rotateButtons.reset.parentElement.append(motionButton);
    const roamingButtons = {};
    for (const [key, text] of [['free', '自由巡游'], ['home', '回到工位']]) {
      const button = document.createElement('button');
      button.id = `robot-roam-${key}`;
      button.type = 'button';
      button.textContent = text;
      button.title = '仅安排本视图位置，不改变会话、终端或工作状态';
      rotateButtons.reset.parentElement.append(button);
      roamingButtons[key] = button;
    }
    const listeners = [];

    function listen(target, event, handler) {
      target.addEventListener(event, handler);
      listeners.push(() => target.removeEventListener(event, handler));
    }
    function setStatus(state, text) {
      stage.dataset.rendererMode = actualMode;
      stage.dataset.rendererState = state;
      status.textContent = view.theme === 'garden' && state === 'ready'
        ? `${actualMode === '3d' ? '3D' : actualMode === 'pixel' ? '像素' : '文字'} 庭院 · 点选伙伴查看详情`
        : text;
      for (const [mode, button] of Object.entries(modeButtons)) button.setAttribute('aria-pressed', String(mode === actualMode));
      for (const button of Object.values(rotateButtons)) button.disabled = actualMode !== '3d';
      motionButton.disabled = actualMode !== '3d';
      motionButton.setAttribute('aria-pressed', String(animationPaused));
      motionButton.textContent = animationPaused ? '播放动画' : '暂停动画';
      for (const [key, button] of Object.entries(roamingButtons)) {
        button.disabled = actualMode !== '3d';
        button.setAttribute('aria-pressed', String((key === 'free') === roamingEnabled));
      }
    }
    function clearRestore() {
      if (restoreCanvas) restoreCanvas.removeEventListener('webglcontextrestored', restoreListener);
      restoreCanvas = null;
      restoreListener = null;
    }
    function stopScene() {
      if (!scene) return;
      const previous = scene;
      scene = null;
      try { previous.dispose(); } finally { previous.canvas.remove(); previous.metrics.remove(); }
    }
    function select(sessionId) {
      if (disposed || !view.records.some(record => record.sessionId === sessionId)) return;
      try { onSelect(sessionId); } catch { setStatus('selection_failed', '会话选择未确认；可使用文字列表'); }
    }
    function drawPixel() {
      if (!context || actualMode !== 'pixel') return;
      const width = Math.max(1, Math.floor(stage.clientWidth));
      const scale = Math.max(2, Math.min(5, Math.floor(width / 100)));
      const cellWidth = 32 * scale + 20;
      const cellHeight = 44 * scale + 44;
      const columns = Math.max(1, Math.floor(width / cellWidth));
      const rows = Math.ceil(view.records.length / columns);
      pixel.width = width;
      pixel.height = Math.max(320, rows * cellHeight + 24);
      pixel.style.width = '100%';
      pixel.style.height = `${pixel.height}px`;
      pixel.dataset.pixelScale = String(scale);
      context.imageSmoothingEnabled = false;
      context.fillStyle = view.theme === 'garden' ? '#d4e4c8' : '#111923';
      context.fillRect(0, 0, pixel.width, pixel.height);
      hitboxes = [];
      if (!view.records.length) {
        context.fillStyle = '#9caec0';
        context.font = '13px sans-serif';
        context.textAlign = 'center';
        context.fillText('尚无会话证据', width / 2, 160);
      }
      view.records.forEach((record, index) => {
        const column = index % columns;
        const row = Math.floor(index / columns);
        const x = Math.round((width - columns * cellWidth) / 2 + column * cellWidth + cellWidth / 2 - 12 * scale);
        const y = 16 + row * cellHeight;
        const box = { sessionId: record.sessionId, x: x - 4 * scale, y, width: 32 * scale, height: 40 * scale + 28 };
        hitboxes.push(box);
        if (record.sessionId === view.selected) {
          context.strokeStyle = '#e5edf5';
          context.lineWidth = 2;
          context.strokeRect(box.x + 1, box.y + 1, box.width - 2, box.height - 2);
        }
        const rect = (px, py, w, h, color) => {
          context.fillStyle = color;
          context.fillRect(x + px * scale, y + py * scale, w * scale, h * scale);
        };
        const color = colors[record.category] || colors.unknown;
        rect(11, 3, 2, 4, color);
        rect(10, 1, 4, 2, '#e5edf5');
        rect(3, 7, 18, 12, color);
        rect(5, 10, 14, 6, '#111923');
        rect(7, 12, 3, 2, '#e5edf5');
        rect(14, 12, 3, 2, '#e5edf5');
        rect(5, 20, 14, 12, color);
        rect(0, 21, 4, 10, color);
        rect(20, 21, 4, 10, color);
        rect(6, 33, 5, 6, color);
        rect(13, 33, 5, 6, color);
        context.fillStyle = view.theme === 'garden' ? '#3e533a' : '#d9e7f8';
        context.font = '11px monospace';
        context.textAlign = 'center';
        context.fillText(record.sessionId.replace(/^codex:/, '').slice(0, 8), x + 12 * scale, y + 40 * scale + 18);
      });
    }
    function schedulePixel() {
      if (disposed || frame || document.hidden || actualMode !== 'pixel') return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        try { drawPixel(); } catch {
          context = null;
          hitboxes = [];
          actualMode = 'text';
          stage.replaceChildren();
          setStatus('pixel_unavailable', '文字模式 · 像素绘制不可用');
        }
      });
    }
    function showPixelOrText(state, explanation) {
      actualMode = context ? 'pixel' : 'text';
      stage.replaceChildren(...(context ? [pixel] : []));
      setStatus(state, `${actualMode === 'pixel' ? '像素' : '文字'}模式 · ${explanation}${context ? '' : '；像素不可用'}`);
      schedulePixel();
    }
    function unavailable(code) {
      const descriptions = {
        module_unavailable: '3D 模块加载失败，可手动重试',
        webgl_unavailable: 'WebGL2 不可用，可使用文字列表',
        initialization_failed: '3D 初始化失败，可手动重试',
        first_draw_failed: '3D 首次绘制失败，可手动重试',
        draw_failed: '3D 绘制失败，可手动重试',
        context_lost: '3D 上下文丢失，可手动重试'
      };
      const lostCanvas = code === 'context_lost' ? scene?.canvas : null;
      const failureGeneration = ++generation;
      stopScene();
      showPixelOrText(code, descriptions[code] || '3D 不可用，可手动重试');
      if (lostCanvas) {
        restoreCanvas = lostCanvas;
        restoreListener = () => {
          if (!disposed && generation === failureGeneration && requestedMode === '3d') {
            setStatus('context_restored', `${actualMode === 'pixel' ? '像素' : '文字'}模式 · 上下文已恢复，请手动点击 3D 重试`);
          }
          clearRestore();
        };
        lostCanvas.addEventListener('webglcontextrestored', restoreListener);
      }
    }
    async function enable3d() {
      requestedMode = '3d';
      clearRestore();
      stopScene();
      const attempt = ++generation;
      showPixelOrText('loading', '正在尝试 3D');
      let module;
      try { module = await import('/robot3d.js'); } catch {
        if (!disposed && attempt === generation) unavailable('module_unavailable');
        return;
      }
      if (disposed || attempt !== generation || requestedMode !== '3d') return;
      let ready;
      try {
        ready = module.createRobotScene({ view, animationEnabled: !animationPaused, onSelect: select, onUnavailable: reason => {
          if (!disposed && attempt === generation) unavailable(reason);
        } });
      } catch (error) {
        if (!disposed && attempt === generation) unavailable(error.code || 'initialization_failed');
        return;
      }
      if (disposed || attempt !== generation || requestedMode !== '3d') { ready.dispose(); return; }
      scene = ready;
      actualMode = '3d';
      stage.replaceChildren(ready.canvas);
      rotateButtons.reset.parentElement.append(ready.metrics);
      ready.setRoaming(roamingEnabled);
      setStatus('ready', `3D 模式 · ${view.records.length ? '拖动环绕 / 滚轮缩放，点选会话' : '尚无会话证据'}；动画仅装饰；活动 / 完成未知`);
      try { ready.update(view); } catch { unavailable('draw_failed'); }
    }
    function setMode(mode) {
      if (disposed) return;
      if (mode === '3d') { enable3d(); return; }
      requestedMode = mode;
      generation++;
      clearRestore();
      stopScene();
      if (mode === 'pixel') showPixelOrText('ready', '点选会话；活动 / 完成未知');
      else {
        actualMode = 'text';
        hitboxes = [];
        stage.replaceChildren();
        setStatus('ready', '文字模式 · 使用会话列表；活动 / 完成未知');
      }
    }
    listen(pixel, 'click', event => {
      if (actualMode !== 'pixel' || !context) return;
      const rect = pixel.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const x = (event.clientX - rect.left) * pixel.width / rect.width;
      const y = (event.clientY - rect.top) * pixel.height / rect.height;
      const hit = hitboxes.find(box => x >= box.x && x < box.x + box.width && y >= box.y && y < box.y + box.height);
      if (hit) select(hit.sessionId);
    });
    for (const [mode, button] of Object.entries(modeButtons)) listen(button, 'click', () => setMode(mode));
    for (const [direction, button] of Object.entries(rotateButtons)) listen(button, 'click', () => scene?.rotate(direction === 'reset' ? 0 : direction === 'left' ? -0.3 : 0.3));
    listen(motionButton, 'click', () => {
      if (actualMode !== '3d' || !scene) return;
      animationPaused = !animationPaused;
      scene.setAnimation(!animationPaused);
      setStatus('ready', '3D 模式 · 拖动环绕 / 滚轮缩放，点选会话；动画仅装饰；活动 / 完成未知');
    });
    for (const [key, button] of Object.entries(roamingButtons)) listen(button, 'click', () => {
      if (actualMode !== '3d' || !scene) return;
      roamingEnabled = key === 'free';
      scene.setRoaming(roamingEnabled);
      setStatus('ready', '3D 模式 · 拖动环绕 / 滚轮缩放，点选会话；巡游仅装饰；活动 / 完成未知');
    });
    listen(motionPreference, 'change', event => {
      if (!event.matches) return;
      animationPaused = true;
      scene?.setAnimation(false);
      setStatus(stage.dataset.rendererState, status.textContent);
    });
    listen(window, 'resize', schedulePixel);
    listen(document, 'visibilitychange', () => {
      if (document.hidden && frame) { cancelAnimationFrame(frame); frame = 0; }
      else {
        if (view.theme === 'garden' && !motionPreference.matches && animationPaused) {
          animationPaused = false; scene?.setAnimation(true); setStatus(stage.dataset.rendererState, status.textContent);
        }
        schedulePixel();
      }
    });
    const observer = new ResizeObserver(schedulePixel);
    observer.observe(stage);
    enable3d();
    return {
      update(nextView) {
        if (disposed) return;
        const nextTheme = nextView.theme === 'garden' ? 'garden' : undefined;
        if (view.theme !== nextTheme && nextTheme === 'garden') {
          savedObservationMotion = { roamingEnabled, animationPaused };
          roamingEnabled = true;
          animationPaused = motionPreference.matches || document.hidden;
          scene?.setRoaming(true); scene?.setAnimation(!animationPaused);
        } else if (view.theme === 'garden' && nextTheme !== 'garden' && savedObservationMotion) {
          ({ roamingEnabled, animationPaused } = savedObservationMotion);
          animationPaused ||= motionPreference.matches;
          savedObservationMotion = null;
          scene?.setRoaming(roamingEnabled); scene?.setAnimation(!animationPaused);
        }
        view = { records: nextView.records.map(record => ({ sessionId: record.sessionId, label: record.label, category: colors[record.category] ? record.category : 'unknown' })), selected: nextView.selected, theme: nextTheme };
        if (scene) {
          scene.update(view);
          setStatus('ready', `3D 模式 · ${view.records.length ? '拖动环绕 / 滚轮缩放，点选会话' : '尚无会话证据'}；动画仅装饰；活动 / 完成未知`);
        }
        else schedulePixel();
      },
      dispose() {
        if (disposed) return;
        disposed = true;
        generation++;
        clearRestore();
        stopScene();
        if (frame) cancelAnimationFrame(frame);
        observer.disconnect();
        listeners.forEach(remove => remove());
        hitboxes = [];
        stage.replaceChildren();
        actualMode = 'text';
        setStatus('disposed', '文字模式 · 图形已停止');
        motionButton.remove();
        Object.values(roamingButtons).forEach(button => button.remove());
      }
    };
  }
  window.SessionCockpit = { create };
})();
