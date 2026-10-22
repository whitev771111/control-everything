'use strict';
(() => {
  const tabs = [...document.querySelectorAll('[data-scene]')];
  const panels = [...document.querySelectorAll('.demo-scene')];
  const play = document.getElementById('demo-play');
  const experience = document.getElementById('experience');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let current = 0, timer = null, playing = false, visible = false;

  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (playing && visible && !document.hidden && !reduceMotion.matches) {
      timer = setTimeout(() => { select((current + 1) % tabs.length); schedule(); }, 6000);
    }
  }
  function setPlaying(value) {
    playing = value && !reduceMotion.matches;
    play.disabled = reduceMotion.matches;
    play.setAttribute('aria-pressed', String(playing));
    play.setAttribute('aria-label', reduceMotion.matches ? '系统已开启减少动态，可手动切换场景' : playing ? '暂停自动演示' : '开启自动演示');
    play.textContent = reduceMotion.matches ? '减少动态已开启' : playing ? '暂停演示 Ⅱ' : '播放演示 ▷';
    schedule();
  }
  function select(index, focus = false) {
    current = index;
    tabs.forEach((tab, i) => {
      tab.setAttribute('aria-selected', String(i === current));
      tab.tabIndex = i === current ? 0 : -1;
      panels[i].hidden = i !== current;
    });
    if (!reduceMotion.matches) panels[current].animate([
      { opacity: 0, transform: 'translateY(9px)' },
      { opacity: 1, transform: 'translateY(0)' },
    ], { duration: 380, easing: 'cubic-bezier(.22,1,.36,1)' });
    if (focus) tabs[current].focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => { setPlaying(false); select(index); });
    tab.addEventListener('keydown', event => {
      let next = index;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault(); setPlaying(false); select(next, true);
    });
    tab.tabIndex = index === 0 ? 0 : -1;
  });
  play.addEventListener('click', () => setPlaying(!playing));
  document.querySelectorAll('[data-next], [data-jump]').forEach(control => control.addEventListener('click', () => {
    const target = control.dataset.next || control.dataset.jump;
    const index = tabs.findIndex(tab => tab.dataset.scene === target);
    if (index >= 0) { setPlaying(false); select(index, true); }
  }));
  // Never change a panel while its interactive content has keyboard focus.
  panels.forEach(panel => panel.addEventListener('focusin', () => setPlaying(false)));
  const taskCopy = {
    design: ['完善产品介绍页', '核对产品文案、首屏排版和手机上的阅读体验。', '待验收'],
    docs: ['补充安装指南', '整理安装步骤和首次打开工作台时需要了解的内容。', '进行中'],
    release: ['整理版本说明', '列出本次变化和当前边界，再核对下载入口。', '待办'],
  };
  document.querySelectorAll('[data-task]').forEach(button => button.addEventListener('click', () => {
    const copy = taskCopy[button.dataset.task];
    document.querySelectorAll('[data-task]').forEach(item => {
      const selected = item === button;
      item.classList.toggle('selected', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
    ['demo-task-title', 'demo-task-note', 'demo-task-state'].forEach((id, i) => { document.getElementById(id).textContent = copy[i]; });
    const preview = document.querySelector('.task-preview');
    if (!reduceMotion.matches) preview.animate([{ opacity: .3, transform: 'translateX(5px)' }, { opacity: 1, transform: 'none' }], { duration: 250, easing: 'ease-out' });
  }));
  const demoObserver = new IntersectionObserver(entries => { visible = entries[0].isIntersecting; schedule(); }, { threshold: .2 });
  demoObserver.observe(experience);
  const revealObserver = new IntersectionObserver(entries => entries.forEach(entry => {
    if (entry.isIntersecting) { entry.target.classList.add('is-visible'); revealObserver.unobserve(entry.target); }
  }), { threshold: .08 });
  document.querySelectorAll('.reveal').forEach(element => {
    if (!reduceMotion.matches) element.classList.add('reveal-ready');
    revealObserver.observe(element);
  });
  document.addEventListener('visibilitychange', schedule);
  reduceMotion.addEventListener('change', () => {
    if (reduceMotion.matches) { setPlaying(false); document.querySelectorAll('.reveal-ready').forEach(element => element.classList.add('is-visible')); }
    else setPlaying(false);
  });
  setPlaying(false);
  window.addEventListener('pagehide', () => { clearTimeout(timer); timer = null; });
  window.addEventListener('pageshow', schedule);
})();
