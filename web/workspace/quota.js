// Quota presentation only; requests and page state belong to the controller.
(() => {
'use strict';
function render(quota, { root, summary, refreshButton, el, dateTime, reason, statusNames }) {
  function quotaTitle(quota) { return statusNames[quota.status] || '状态未知'; }
  function quotaIssue(quota) { return `${reason(quota.reason)}${quota.error_code !== null && quota.error_code !== undefined ? `（${quota.error_code}）` : ''}`; }

  root.replaceChildren(); summary.replaceChildren();
  root.dataset.status = quota.status;
  summary.dataset.status = quota.status;
  const firstBucket = (quota.buckets || []).find(bucket => [bucket.primary, bucket.secondary].some(window => typeof window?.used_percent === 'number' && Number.isFinite(window.used_percent)));
  const firstWindow = firstBucket && [firstBucket.primary, firstBucket.secondary].find(window => typeof window?.used_percent === 'number' && Number.isFinite(window.used_percent));
  const remaining = firstWindow ? Math.max(0, Math.min(100, 100 - firstWindow.used_percent)).toFixed(1).replace(/\.0$/, '') : null;
  summary.append(el('h2', '', firstWindow ? `剩余 ${remaining}%` : quotaTitle(quota)));
  const brief = quota.status === 'not_checked' ? '点击「查询额度」读取官方只读接口。页面不会自动启动查询。' : ['unavailable', 'stale'].includes(quota.status) ? quotaIssue(quota) : '各窗口的已用与剩余比例见用量页。';
  if (firstWindow) {
    const minutes = firstWindow.window_minutes;
    const duration = minutes === null ? '窗口时长未知' : minutes >= 1440 && minutes % 1440 === 0 ? `${minutes / 1440} 天窗口` : `${minutes} 分钟窗口`;
    summary.append(el('p', 'muted', `${firstBucket.id} · ${duration} · 已用 ${firstWindow.used_percent}%${quota.status === 'stale' ? ' · 旧缓存' : ''}`));
    if (quota.status === 'stale') summary.append(el('p', 'muted', brief));
  } else summary.append(el('p', 'muted', brief));
  if (quota.sampled_at) summary.append(el('p', 'muted', `${dateTime(quota.sampled_at)} 采样`));
  if (!quota.buckets?.length) {
    const empty = el('div', 'quota-empty'), graphic = el('div', 'quota-orbit'); graphic.setAttribute('aria-hidden', 'true');
    graphic.append(el('span', '', '—'));
    const copy = el('div', 'quota-empty-copy');
    copy.append(el('h3', '', quota.status === 'not_checked' ? '尚未读取额度' : '暂时无法读取'),
      el('p', '', quota.status === 'not_checked' ? '查询后查看各窗口的剩余额度。' : reason(quota.reason)));
    empty.append(graphic, copy); root.append(empty);
  } else root.append(el('p', 'quota-state', quotaTitle(quota)));
  if (quota.status === 'stale') root.append(el('p', 'quota-error', '下方保留上次成功结果，不代表当前额度。'));
  for (const bucket of quota.buckets || []) {
    const section = el('div', 'quota-bucket'); section.append(el('h3', '', bucket.id === 'codex' ? '订阅额度' : `额度池 · ${bucket.id}`));
    for (const [key, label] of [['primary', '主窗口'], ['secondary', '次窗口']]) {
      const window = bucket[key]; if (!window) continue;
      const row = el('div', 'quota-window'), heading = el('div', 'window-heading');
      const duration = window.window_minutes === null ? '时长未知' : window.window_minutes >= 1440 && window.window_minutes % 1440 === 0 ? `${window.window_minutes / 1440} 天` : `${window.window_minutes} 分钟`;
      const valid = typeof window.used_percent === 'number' && Number.isFinite(window.used_percent);
      heading.append(el('span', '', `${duration}额度`), el('strong', '', valid ? `剩余 ${Math.max(0, Math.min(100, 100 - window.used_percent)).toFixed(1).replace(/\.0$/, '')}%` : '剩余未知'));
      const dial = el('div', 'quota-dial');
      const available = valid ? Math.max(0, Math.min(100, 100 - window.used_percent)) : null;
      dial.dataset.known = String(valid);
      const chart = el('svg'); chart.setAttribute('viewBox', '0 0 160 160'); chart.setAttribute('aria-hidden', 'true');
      for (const filled of [false, true]) {
        const circle = el('circle'); circle.setAttribute('cx', '80'); circle.setAttribute('cy', '80'); circle.setAttribute('r', '69');
        circle.setAttribute('pathLength', '100'); circle.setAttribute('class', filled ? 'quota-ring-value' : 'quota-ring-track');
        if (filled) circle.setAttribute('stroke-dasharray', `${available ?? 0} 100`);
        chart.append(circle);
      }
      dial.append(chart);
      dial.append(el('strong', '', valid ? `${Number(available.toFixed(1))}%` : '—'), el('span', '', valid ? '剩余额度' : '额度未知'));
      row.append(heading, dial);
      if (valid) { const bar = el('progress'); bar.max = 100; bar.value = window.used_percent; bar.setAttribute('aria-label', `${label}已用 ${window.used_percent}%`); row.append(bar); }
      row.append(el('p', '', `${valid ? `已用 ${window.used_percent}% · ` : ''}重置：${dateTime(window.resets_at)}（北京时间）`)); section.append(row);
    }
    root.append(section);
  }
  const details = el('details', 'quota-diagnostics'); details.append(el('summary', '', '查询详情'));
  if (quota.status !== 'available') details.append(el('p', '', brief));
  details.append(el('p', 'quota-meta', `来源：Codex 官方 app-server · 最近采样 ${dateTime(quota.sampled_at)}${quota.last_success_at ? ` · 最近成功 ${dateTime(quota.last_success_at)}` : ''}`));
  root.append(details);
  refreshButton.textContent = quota.status === 'not_checked' ? '查询额度' : '重新查询';
}

window.WorkspaceQuota = { render };
})();
