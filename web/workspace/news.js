// Generated from web/workspace/news.ts by tools/scripts/build_news.cjs. Do not edit.
'use strict';
(function () {
    const statuses = ['pending', 'available', 'stale', 'unavailable'];
    const reasons = ['not_loaded', 'refreshing', 'partial_failure', 'cache_expired', 'timeout', 'http_error', 'oversize', 'invalid_feed', 'unsupported_format', 'parse_budget', 'filtered_all', 'redirect_rejected', 'encoding_unsupported', 'worker_failed', 'worker_busy', 'cleanup_unverified', 'platform_unsupported', 'cooldown'];
    const statusText = { pending: '尚在采集', available: '缓存有效', stale: '缓存陈旧或部分来源失败', unavailable: '来源不可用' };
    const reasonText = {
        not_loaded: '尚无完成样本', refreshing: '正在刷新', partial_failure: '部分来源未成功', cache_expired: '缓存已过期',
        timeout: '采集超时', http_error: '来源暂时无法访问', oversize: '来源超过大小上限', invalid_feed: '来源格式无效',
        unsupported_format: '来源格式尚不支持', parse_budget: '来源超过解析预算', filtered_all: '返回条目均未通过筛选',
        redirect_rejected: '来源重定向不被允许', encoding_unsupported: '来源编码不受支持', worker_failed: '采集未正常完成',
        worker_busy: '采集槽正忙', cleanup_unverified: '采集清理未证实，已停止新批次', platform_unsupported: '当前平台不支持采集', cooldown: '刷新间隔尚未到期',
    };
    const record = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);
    const text = (value, max) => typeof value === 'string' && Array.from(value).length <= max;
    const count = (value) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
    function oneOf(value, allowed) { return typeof value === 'string' && allowed.some(item => item === value); }
    function timestamp(value) {
        if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) || Number(value.slice(0, 4)) < 1)
            return false;
        const time = Date.parse(value);
        return Number.isFinite(time) && new Date(time).toISOString().slice(0, 19) === value.slice(0, 19);
    }
    const nullableTime = (value) => value === null || timestamp(value);
    const nullableReason = (value) => value === null || oneOf(value, reasons);
    function isItem(value) {
        return record(value) && text(value.id, 64) && text(value.source_id, 32) && text(value.source_name, 100) &&
            text(value.title, 300) && value.title.length > 0 && text(value.url, 2048) &&
            nullableTime(value.published_at) && timestamp(value.fetched_at) && (value.published_raw === null || text(value.published_raw, 128)) &&
            (value.date_reason === null || oneOf(value.date_reason, ['missing', 'invalid', 'timezone_unknown'])) &&
            nullableTime(value.updated_at) && nullableTime(value.submitted_at) && oneOf(value.content_origin, ['publisher_feed', 'hn_submission']) &&
            (value.summary === null || text(value.summary, 600)) && (value.summary_kind === null || oneOf(value.summary_kind, ['rss_description', 'atom_summary'])) &&
            typeof value.summary_truncated === 'boolean' && (value.summary_reason === null || oneOf(value.summary_reason, ['missing', 'empty', 'summary_too_long', 'unsupported_type'])) && typeof value.title_truncated === 'boolean';
    }
    function isSource(value) {
        return record(value) && text(value.id, 32) && text(value.name, 100) && text(value.url, 2048) && oneOf(value.status, statuses) &&
            nullableReason(value.reason) && nullableTime(value.sampled_at) && nullableTime(value.last_success_at) &&
            ['item_count', 'cache_age_ms', 'fresh_for_ms', 'scanned_count', 'valid_count', 'filtered_count', 'duplicate_count', 'returned_count'].every(key => count(value[key])) &&
            typeof value.truncated === 'boolean' && (value.format === null || oneOf(value.format, ['rss2', 'atom1', 'hn']));
    }
    function requireNews(value, stream) {
        if (!record(value) || value.schema !== 1 || value.stream !== stream || !oneOf(value.status, statuses) || !nullableReason(value.reason) ||
            !nullableTime(value.sampled_at) || !Array.isArray(value.items) || value.items.length > (stream === 'ai' ? 60 : 4) || !value.items.every(isItem) ||
            !Array.isArray(value.sources) || value.sources.length !== (stream === 'ai' ? 3 : 1) || !value.sources.every(isSource) ||
            !record(value.refresh) || !oneOf(value.refresh.state, ['idle', 'pending', 'blocked']) || !nullableReason(value.refresh.reason) || !count(value.refresh.retry_after_ms))
            throw new Error('新闻响应无法识别');
        const sourceIds = stream === 'ai' ? ['google_ai', 'openai', 'huggingface'] : ['hn'];
        const seen = new Set();
        for (const source of value.sources) {
            if (!sourceIds.includes(source.id) || seen.has(source.id) || source.item_count > (stream === 'ai' ? 20 : 4) || source.returned_count !== source.item_count)
                throw new Error('新闻来源无法识别');
            seen.add(source.id);
        }
        const itemIds = new Set();
        for (const item of value.items) {
            if (!seen.has(item.source_id) || itemIds.has(item.id) || (stream === 'hn' ? item.content_origin !== 'hn_submission' || item.published_at !== null : item.content_origin !== 'publisher_feed'))
                throw new Error('新闻条目无法识别');
            itemIds.add(item.id);
        }
        for (const source of value.sources)
            if (value.items.filter(item => item.source_id === source.id).length !== source.item_count)
                throw new Error('新闻条目计数不一致');
        return { schema: 1, stream, status: value.status, reason: value.reason, sampled_at: value.sampled_at, items: value.items, sources: value.sources,
            refresh: { state: value.refresh.state, reason: value.refresh.reason, retry_after_ms: value.refresh.retry_after_ms } };
    }
    function element(tag, className = '', content = '') {
        const node = document.createElement(tag);
        node.className = className;
        node.textContent = content;
        return node;
    }
    function dom(id, tag) {
        const node = document.getElementById(id);
        if (!node || node.tagName.toLowerCase() !== tag)
            throw new Error(`新闻页面元素缺失或类型错误：${id}`);
        return node;
    }
    function safeLink(label, href) {
        try {
            if (/[\x00-\x20\\]/.test(href))
                throw new Error();
            const url = new URL(href), host = url.hostname.toLowerCase();
            if (url.protocol !== 'https:' || url.username || url.password || !host.includes('.') || host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local') || host.includes(':'))
                throw new Error();
            if (/^\d+(?:\.\d+){3}$/.test(host)) {
                const [a = 0, b = 0] = host.split('.').map(Number);
                if (a === 0 || a === 10 || a === 127 || a >= 224 || a === 169 && b === 254 || a === 172 && b >= 16 && b <= 31 || a === 192 && (b === 168 || b === 0) || a === 100 && b >= 64 && b <= 127 || a === 198 && (b === 18 || b === 19))
                    throw new Error();
            }
            const link = element('a', 'news-link', label);
            link.href = url.href;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            return link;
        }
        catch {
            return element('span', 'news-link', label + '（链接不可用）');
        }
    }
    function localDay() {
        try {
            const zone = new Intl.DateTimeFormat().resolvedOptions().timeZone;
            if (!zone)
                return null;
            const now = Date.now();
            return { zone, day: dayOf(now, zone), now };
        }
        catch {
            return null;
        }
    }
    function dayOf(time, zone) {
        const parts = new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(time));
        return ['year', 'month', 'day'].map(key => parts.find(part => part.type === key)?.value || '').join('-');
    }
    function displayTime(value, local) {
        if (!value)
            return '时间未知';
        if (!local)
            return value + '（UTC；无法判定本地日期）';
        return new Intl.DateTimeFormat('zh-CN', { timeZone: local.zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) + `（${local.zone}）`;
    }
    function group(item, local) {
        if (!item.published_at || !local)
            return 'unknown';
        const time = Date.parse(item.published_at);
        if (time > local.now)
            return 'future';
        return dayOf(time, local.zone) === local.day ? 'today' : 'earlier';
    }
    const samples = {};
    let dependencies = null;
    let mode = null, selected = 'ai', nonce = 0, inFlight = false, stopped = true;
    let pollTimer = null, deadlineTimer = null, clockTimer = null;
    let controller = null, attempts = 0;
    const warnings = {}, rechecks = {};
    let list, sources, status, preview = null;
    let refresh = null, selector = null;
    const mono = () => performance.now();
    function sampleAge(sample) {
        const wall = Date.now(), currentMono = mono();
        sample.age += Math.max(0, wall - sample.wall, currentMono - sample.mono);
        sample.wall = wall;
        sample.mono = currentMono;
        return sample.age;
    }
    function isOld(source, age) { return source.status !== 'available' || source.cache_age_ms + age >= 900000 || age >= source.fresh_for_ms; }
    function card(item, sample, age, local) {
        const source = sample.value.sources.find(row => row.id === item.source_id);
        const article = element('article', 'daily-news-card');
        article.dataset.newsId = item.id;
        const old = !!warnings[sample.value.stream] || !!rechecks[sample.value.stream] || !source || isOld(source, age);
        article.dataset.freshness = old ? 'stale' : 'available';
        const heading = element('h3');
        heading.append(safeLink(item.title, item.url));
        article.append(heading);
        const published = item.content_origin === 'hn_submission' ? `文章发布时间未知 · HN收录 ${displayTime(item.submitted_at, local)}` : `发布 ${displayTime(item.published_at, local)}`;
        article.append(element('p', 'news-meta', `${item.source_name} · ${published} · ${old ? '旧缓存或状态未确认' : '缓存有效'}`));
        if (item.title_truncated)
            article.append(element('p', 'news-meta', '标题节选'));
        if (item.published_at === null && item.content_origin !== 'hn_submission')
            article.append(element('p', 'news-meta', `出版日期未知${item.date_reason === 'timezone_unknown' ? '：来源未给出可判定时区' : ''}${item.published_raw ? ` · 来源日期：${item.published_raw}` : ''}`));
        if (item.updated_at)
            article.append(element('p', 'news-meta', `来源更新 ${displayTime(item.updated_at, local)}（不作出版时间）`));
        if (item.summary !== null && item.summary_kind !== null)
            article.append(element('p', 'news-summary', `${item.summary_truncated ? '来源摘要节选' : '来源摘要'}：${item.summary}`));
        article.append(element('p', 'news-meta', `采样 ${displayTime(item.fetched_at, local)} · 上次成功 ${displayTime(source?.last_success_at ?? null, local)}`));
        return article;
    }
    function render() {
        if (stopped)
            return;
        const local = localDay(), sample = samples[selected], warning = warnings[selected] || '', rechecking = !!rechecks[selected];
        list.replaceChildren();
        sources.replaceChildren();
        const bounded = selected === 'hn' ? 'Hacker News 前页，非默认、非AI筛选；最多4条，仅HN收录时间。' : '本次返回窗口最多20条/源、合计60条；不代表全站或全日。';
        sources.append(element('p', 'news-limit', bounded));
        if (!sample) {
            status.textContent = warning || '尚无完成样本 · 正在读取公开来源';
        }
        else {
            const age = sampleAge(sample), value = sample.value, old = value.sources.some(source => isOld(source, age));
            status.textContent = `${warning || (rechecking ? '恢复可见，状态重核中' : statusText[old && value.status === 'available' ? 'stale' : value.status])} · 最近尝试 ${displayTime(value.sampled_at, local)}${value.refresh.state === 'pending' ? ' · 正在刷新（不表示新鲜成功）' : ''}${value.refresh.state === 'blocked' ? ' · 采集已停止' : ''}${value.refresh.reason ? ` · ${reasonText[value.refresh.reason] || '状态未知'}` : ''}${value.refresh.retry_after_ms ? ` · ${Math.ceil(value.refresh.retry_after_ms / 1000)}秒后可再尝试` : ''}`;
            for (const source of value.sources)
                sources.append(element('p', 'source-chip', `${source.name} · ${isOld(source, age) ? '陈旧/不可用' : '缓存有效'} · ${source.reason ? reasonText[source.reason] || '未知原因' : statusText[source.status]} · 扫描${source.scanned_count} / 有效${source.valid_count} / 过滤${source.filtered_count} / 重复${source.duplicate_count} / 返回${source.returned_count}${source.truncated ? '（输出已截断）' : ''} · 采样${displayTime(source.sampled_at, local)} · 上次成功${displayTime(source.last_success_at, local)}`));
            if (!local)
                list.append(element('p', 'news-notice', '无法判定本地日期；以下日期不计入今日。'));
            if (!value.items.length)
                list.append(element('p', 'news-notice', value.status === 'available' ? '本次源响应为空。' : '本次没有可展示条目，来源状态见上。'));
            const groups = selected === 'hn' ? [{ key: 'hn', label: 'HN 已返回条目', items: value.items }] : [
                { key: 'today', label: '本地今日', items: value.items.filter(item => group(item, local) === 'today') },
                { key: 'earlier', label: '较早文章', items: value.items.filter(item => group(item, local) === 'earlier') },
                { key: 'unknown', label: '出版日期未知', items: value.items.filter(item => group(item, local) === 'unknown') },
                { key: 'future', label: '来源标为未来', items: value.items.filter(item => group(item, local) === 'future') },
            ];
            for (const section of groups)
                if (section.items.length) {
                    const host = element('section', 'news-day-group');
                    host.dataset.newsGroup = section.key;
                    host.append(element('h3', 'news-group-title', `${section.label} · ${section.items.length}条`));
                    for (const item of section.items)
                        host.append(card(item, sample, age, local));
                    list.append(host);
                }
        }
        if (preview) {
            preview.replaceChildren();
            const ai = samples.ai;
            if (!ai)
                preview.append(element('p', 'news-notice', 'AI来源尚无完成样本。'));
            else {
                const age = sampleAge(ai), today = ai.value.items.filter(item => group(item, local) === 'today').slice(0, 2);
                preview.append(element('p', 'news-meta', `${local ? `本地日期 ${local.day}（${local.zone}）` : '无法判定本地日期'} · ${warnings.ai || rechecks.ai || ai.value.sources.some(source => isOld(source, age)) ? '含陈旧、失败或未确认来源' : '来源缓存有效'} · 返回窗口内精选${today.length}条`));
                if (!today.length)
                    preview.append(element('p', 'news-notice', '本次已返回的文章中未见今日内容。'));
                for (const item of today)
                    preview.append(card(item, ai, age, local));
                const unknown = ai.value.items.filter(item => item.published_at === null).length;
                preview.append(element('p', 'news-meta', `日期未知${unknown}条${ai.value.sources.some(source => source.truncated) ? ' · 来源输出已截断' : ''} · 最多20条/源，不代表全网。`));
                const more = element('a', 'text-link', '查看已返回的旧文章/未知日期 →');
                more.href = '#news';
                preview.append(more);
            }
        }
        if (refresh)
            refresh.disabled = inFlight;
    }
    function clearCycle() {
        if (pollTimer !== null)
            clearTimeout(pollTimer);
        if (deadlineTimer !== null)
            clearTimeout(deadlineTimer);
        pollTimer = deadlineTimer = null;
        controller?.abort();
        controller = null;
        inFlight = false;
    }
    async function read(token, post) {
        if (!dependencies || stopped || document.hidden || token !== nonce || inFlight)
            return;
        inFlight = true;
        attempts++;
        controller = new AbortController();
        render();
        try {
            const stream = selected;
            const raw = post && 'postJson' in dependencies ? await dependencies.postJson('/api/workspace/news/refresh', { stream }, { signal: controller.signal }) :
                await dependencies.fetchJson(mode === 'legacy' ? '/api/desk/news' : stream === 'ai' ? '/api/workspace/news' : '/api/workspace/news?stream=hn', { signal: controller.signal });
            if (stopped || document.hidden || token !== nonce)
                return;
            const value = requireNews(raw, stream);
            samples[stream] = { value, age: 0, wall: Date.now(), mono: mono() };
            warnings[stream] = '';
            rechecks[stream] = false;
            if ((value.status === 'pending' || value.refresh.state === 'pending') && attempts < 10)
                pollTimer = setTimeout(() => { pollTimer = null; void read(token, false); }, 2000);
            else if (value.status !== 'pending' && value.refresh.state !== 'pending' && deadlineTimer !== null) {
                clearTimeout(deadlineTimer);
                deadlineTimer = null;
            }
        }
        catch {
            if (token !== nonce || stopped || document.hidden)
                return;
            warnings[selected] = samples[selected] ? '读取失败 · 旧结果状态未确认' : '来源不可用 · 未取得可验证结果';
            rechecks[selected] = false;
            if (deadlineTimer !== null)
                clearTimeout(deadlineTimer);
            deadlineTimer = null;
        }
        finally {
            if (token === nonce) {
                inFlight = false;
                controller = null;
                render();
            }
        }
    }
    function begin(post = false) {
        if (!dependencies || stopped || document.hidden || inFlight)
            return;
        clearCycle();
        const token = ++nonce;
        attempts = 0;
        deadlineTimer = setTimeout(() => { if (token !== nonce)
            return; nonce++; clearCycle(); warnings[selected] = '仍未取得完成结果 · 已停止本轮前端等待'; rechecks[selected] = false; render(); }, 20000);
        void read(token, post);
    }
    function tick() { if (stopped)
        return; if (!document.hidden)
        render(); clockTimer = setTimeout(tick, 60000); }
    function resume() {
        if (document.hidden || !dependencies)
            return;
        if (stopped) {
            stopped = false;
            clockTimer = setTimeout(tick, 60000);
        }
        rechecks[selected] = true;
        render();
        begin();
    }
    function start(deps, legacy) {
        if (dependencies)
            return;
        if (legacy) {
            list = dom('news-zone', 'div');
            sources = dom('legacy-news-sources', 'div');
            status = dom('legacy-news-status', 'p');
            selected = 'hn';
        }
        else {
            list = dom('news-list', 'div');
            sources = dom('news-sources', 'div');
            status = dom('news-status', 'p');
            preview = dom('news-preview', 'div');
            refresh = dom('refresh-news', 'button');
            selector = dom('news-stream', 'select');
            refresh.addEventListener('click', () => begin(true));
            selector.addEventListener('change', () => { clearCycle(); nonce++; selected = selector?.value === 'hn' ? 'hn' : 'ai'; render(); begin(); });
        }
        dependencies = deps;
        mode = legacy ? 'legacy' : 'workspace';
        stopped = false;
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                nonce++;
                clearCycle();
                rechecks[selected] = true;
            }
            else
                resume();
        });
        window.addEventListener('pageshow', resume);
        window.addEventListener('pagehide', () => { stopped = true; nonce++; clearCycle(); if (clockTimer !== null)
            clearTimeout(clockTimer); clockTimer = null; });
        clockTimer = setTimeout(tick, 60000);
        render();
        begin();
    }
    window.WorkspaceNews = { startWorkspace: dependencies => start(dependencies, false), startLegacy: dependencies => start(dependencies, true),
        onView(view) { if (mode === 'workspace' && (view === 'news' || view === 'overview') && !stopped)
            render(); } };
})();
