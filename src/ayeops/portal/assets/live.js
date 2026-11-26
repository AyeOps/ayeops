(() => {
  const source = document.getElementById('aye-live');
  let spec = null;
  try { spec = JSON.parse(source ? source.textContent : 'null'); } catch (error) { spec = null; }
  const strip = document.querySelector('.live-strip');
  if (!spec || !strip) return;
  const services = spec.services || {};
  const hosts = spec.hosts || {};
  const limits = spec.disks || {};
  const T = window.ayeT || ((key) => key);
  const STALE_SECONDS = 300;

  const bytes = (amount) => {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let n = amount;
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (n >= 10 || i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  };
  const pct = (amount) => (amount < 10 ? amount.toFixed(1) : amount.toFixed(0)) + ' %';
  const ms = (seconds) => {
    const amount = seconds * 1000;
    if (amount < 10) return amount.toFixed(1) + ' ms';
    if (amount < 1000) return amount.toFixed(0) + ' ms';
    return seconds.toFixed(2) + ' s';
  };
  const age = (seconds) => {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return T('js.days', { d: days, h: hours });
    if (hours) return T('js.hours', { h: hours, m: minutes });
    return T('js.minutes', { m: minutes });
  };
  const cores = (amount) => String(Math.round(amount * 1000) / 1000);
  const cpuView = (used, limit) => {
    if (!limit) return { text: T('js.of_core', { share: pct(used * 100) }), cls: '' };
    const share = (used * 100) / limit;
    const text = T('js.of_cpus', { share: pct(share), used: cores(used), cpus: cores(limit) });
    return { text, cls: share >= 80 ? 'warn' : '' };
  };
  const html = (text) => String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

  const api = (path, params) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    const url = 'api/v1/' + path + '?' + new URLSearchParams(params).toString();
    return fetch(url, { signal: controller.signal, headers: { Accept: 'application/json' } })
      .then((response) => {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then((body) => {
        if (!body || body.status !== 'success') throw new Error((body && body.error) || 'query failed');
        return body.data.result;
      })
      .finally(() => clearTimeout(timer));
  };

  const valueOf = (series) => parseFloat(series.value[1]);
  const pick = (result, test) => (result || []).filter((series) => test(series.metric || {}));
  const numbers = (list) => list.map(valueOf).filter(Number.isFinite);
  const total = (list) => (list.length ? list.reduce((a, b) => a + b, 0) : null);
  const patterns = {};
  const containerTest = (key, svc) => {
    if (!svc.container) return () => false;
    if (!patterns[key]) {
      try { patterns[key] = new RegExp('^(?:' + svc.container + ')$'); } catch (error) { patterns[key] = null; }
    }
    const pattern = patterns[key];
    return (metric) => Boolean(pattern) && metric.host === svc.host && pattern.test(metric.name || '');
  };

  const serviceState = (key, svc, data) => {
    const up = numbers(pick(data.up, (m) => m.host === svc.host && svc.up.includes(m.instance)));
    const probe = numbers(pick(data.probe, (m) => svc.probe.includes(m.instance)));
    const inContainer = containerTest(key, svc);
    const seen = numbers(pick(data.seen, inContainer));
    const lastSeen = seen.length ? Math.min(...seen) : null;
    const reasons = [];
    if (up.some((v) => v < 1)) reasons.push(T('js.scrape_down'));
    if (probe.some((v) => v < 1)) reasons.push(T('js.probe_failing'));
    if (lastSeen !== null && lastSeen > STALE_SECONDS) reasons.push(T('js.gone', { age: age(lastSeen) }));
    const signals = up.length + probe.length + (lastSeen === null ? 0 : 1);
    const latency = numbers(pick(data.latency, (m) => svc.probe.includes(m.instance)));
    const latencySeries = pick(data.latency_range, (m) => svc.probe.includes(m.instance))[0];
    let state = 'live';
    if (!signals) state = 'nodata';
    else if (reasons.length) state = 'down';
    return {
      state,
      reasons,
      latency: latency.length ? Math.max(...latency) : null,
      latencyPoints: latencySeries ? latencySeries.values.map((p) => parseFloat(p[1])) : [],
      mem: total(numbers(pick(data.mem, inContainer))),
      cpu: total(numbers(pick(data.cpu, inContainer))),
    };
  };

  const sparkPath = (points, top) => {
    const list = points.filter(Number.isFinite);
    if (list.length < 2) return '';
    const span = top || Math.max(...list) || 1;
    const step = 80 / (list.length - 1);
    return list.map((v, i) => (i ? 'L' : 'M') + (i * step).toFixed(1) + ' ' + (21 - (Math.min(v, span) / span) * 19).toFixed(1)).join(' ');
  };
  const spark = (svg, points, top) => {
    if (!svg) return;
    const d = sparkPath(points, top);
    svg.innerHTML = d ? '<path d="' + d + '"></path>' : '';
  };
  const cell = (block, name, text, cls) => {
    const item = block.querySelector('[data-m="' + name + '"]');
    if (!item) return;
    item.hidden = text === null;
    if (text === null) return;
    item.querySelector('b').textContent = text;
    item.className = 'm' + (cls ? ' ' + cls : '');
  };
  const each = (selector, fn) => document.querySelectorAll(selector).forEach(fn);
  const quoted = (text) => '"' + CSS.escape(text) + '"';

  const showService = (key, svc, info) => {
    const label = T('js.' + info.state);
    const text = info.state === 'down' ? label + ' · ' + info.reasons.join(', ') : label;
    each('[data-svc=' + quoted(key) + ']:not(.slive)', (item) => { item.dataset.st = info.state; item.title = text; });
    each('.node[data-node=' + quoted(key) + ']', (item) => { item.dataset.st = info.state; });
    each('.slive[data-svc=' + quoted(key) + ']', (block) => {
      block.hidden = false;
      cell(block, 'status', text, info.state);
      cell(block, 'latency', info.latency === null ? null : ms(info.latency), '');
      spark(block.querySelector('[data-m="latency"] .spark'), info.latencyPoints);
      let memory = null;
      if (info.mem !== null) memory = svc.mem_limit ? T('js.of', { used: bytes(info.mem), total: bytes(svc.mem_limit) }) : bytes(info.mem);
      cell(block, 'mem', memory, '');
      const view = info.cpu === null ? null : cpuView(info.cpu, svc.cpus);
      cell(block, 'cpu', view && view.text, view ? view.cls : '');
    });
  };

  const showHost = (host, data) => {
    const one = (name) => {
      const found = numbers(pick(data[name], (m) => m.host === host));
      return found.length ? found[0] : null;
    };
    const cpu = one('host_cpu');
    const used = one('host_mem_used');
    const memory = one('host_mem_total');
    const disk = one('host_disk');
    const days = one('host_disk_days');
    const swapTotal = one('host_swap_total');
    const swapUsed = one('host_swap_used');
    const battery = one('host_battery');
    const uptime = one('host_uptime');
    const range = pick(data.host_cpu_range, (m) => m.host === host)[0];
    each('.hlive[data-host=' + quoted(host) + ']', (block) => {
      cell(block, 'cpu', cpu === null ? null : pct(cpu), '');
      spark(block.querySelector('[data-m="cpu"] .spark'), range ? range.values.map((p) => parseFloat(p[1])) : []);
      cell(block, 'mem', used === null || memory === null ? null : T('js.of', { used: bytes(used), total: bytes(memory) }), '');
      const soon = days !== null && days > 0 && days < 365 ? ' · ' + T('js.full_in', { n: Math.ceil(days) }) : '';
      cell(block, 'disk', disk === null ? null : pct(disk) + soon, soon ? daysLevel(days) : '');
      cell(block, 'swap', swapTotal ? T('js.of', { used: bytes(swapUsed || 0), total: bytes(swapTotal) }) : null, '');
      cell(block, 'battery', battery === null ? null : pct(battery), battery !== null && battery < 20 ? 'warn' : '');
      cell(block, 'uptime', uptime === null ? null : age(uptime), '');
      block.hidden = !block.querySelector('.m:not([hidden])');
    });
  };

  const level = (value, warn, crit) => {
    if (crit !== null && crit !== undefined && value >= crit) return 'crit';
    if (warn !== null && warn !== undefined && value >= warn) return 'warn';
    return '';
  };
  const daysLevel = (days) => {
    if (days < limits.days_crit) return 'crit';
    if (days < limits.days_warn) return 'warn';
    return '';
  };
  const byKey = (list, key) => {
    const found = {};
    list.forEach((series) => { found[series.metric[key]] = valueOf(series); });
    return found;
  };
  const filesystems = (host, data) => {
    const mine = (metric) => metric.host === host;
    const avail = byKey(pick(data.fs_avail, mine), 'mountpoint');
    const days = byKey(pick(data.fs_days, mine), 'mountpoint');
    const trend = {};
    pick(data.fs_used_range, mine).forEach((series) => { trend[series.metric.mountpoint] = series.values.map((p) => parseFloat(p[1])); });
    const seen = {};
    pick(data.fs_size, mine)
      .map((series) => ({ mount: series.metric.mountpoint || '', device: series.metric.device || '', fstype: series.metric.fstype || '', size: valueOf(series) }))
      .filter((fs) => fs.size > 0 && Number.isFinite(avail[fs.mount]))
      .sort((a, b) => a.mount.length - b.mount.length || a.mount.localeCompare(b.mount))
      .forEach((fs) => { if (!seen[fs.device || fs.mount]) seen[fs.device || fs.mount] = fs; });
    return Object.values(seen).sort((a, b) => a.mount.localeCompare(b.mount)).map((fs) => {
      const used = Math.max(0, fs.size - avail[fs.mount]);
      return { ...fs, used, share: (used * 100) / fs.size, days: days[fs.mount], trend: trend[fs.mount] || [] };
    });
  };
  const fsRow = (fs) => {
    const tone = level(fs.share, limits.warn, limits.crit);
    const soon = Number.isFinite(fs.days) && fs.days > 0 && fs.days < 365;
    const days = soon ? '<span class="days ' + daysLevel(fs.days) + '">' + html(T('js.full_in', { n: Math.ceil(fs.days) })) + '</span>' : '';
    const trend = sparkPath(fs.trend, 100);
    return '<div class="vol ' + tone + '"><div class="vol-h"><code>' + html(fs.mount) + '</code><span class="vol-dev">'
      + html([fs.device, fs.fstype].filter(Boolean).join(' · ')) + '</span><b>' + html(pct(fs.share)) + '</b></div>'
      + '<div class="bar"><i style="width:' + Math.min(100, fs.share).toFixed(1) + '%"></i></div>'
      + '<div class="vol-f"><span>' + html(T('js.of', { used: bytes(fs.used), total: bytes(fs.size) })) + '</span>' + days + '</div>'
      + (trend ? '<svg class="spark trend" viewBox="0 0 80 22" preserveAspectRatio="none" role="img" aria-label="' + html(T('js.disks.trend')) + '"><path d="' + trend + '"></path></svg>' : '')
      + '</div>';
  };
  const drives = (host, data) => {
    const mine = (metric) => metric.host === host;
    const found = {};
    const drive = (device) => {
      found[device] = found[device] || { device, sectors: {} };
      return found[device];
    };
    pick(data.smart_status, mine).forEach((series) => { drive(series.metric.device).ok = valueOf(series); });
    pick(data.smart_wear, mine).forEach((series) => { drive(series.metric.device).wear = valueOf(series); });
    pick(data.smart_temp, mine).forEach((series) => { drive(series.metric.device).temp = valueOf(series); });
    pick(data.smart_growth, mine).forEach((series) => { drive(series.metric.device).growth = valueOf(series); });
    pick(data.smart_sectors, mine).forEach((series) => { drive(series.metric.device).sectors[series.metric.attribute_name] = valueOf(series); });
    return Object.values(found).sort((a, b) => String(a.device).localeCompare(String(b.device)));
  };
  const driveRow = (item) => {
    const parts = [];
    const bad = Object.values(item.sectors).some((v) => v > 0);
    const growing = item.growth > 0;
    let tone = bad ? 'warn' : '';
    if (growing || item.ok === 0) tone = 'crit';
    const part = (cls, text) => parts.push('<span class="' + cls + '">' + html(text) + '</span>');
    if (item.ok !== undefined) part(item.ok === 0 ? 'crit' : '', T('js.smart') + ' ' + T(item.ok === 0 ? 'js.smart.failing' : 'js.smart.ok'));
    const reallocated = item.sectors.Reallocated_Sector_Ct;
    const pending = item.sectors.Current_Pending_Sector;
    if (reallocated !== undefined) part(reallocated > 0 ? tone : '', T('js.smart.reallocated', { n: reallocated }));
    if (pending !== undefined) part(pending > 0 ? tone : '', T('js.smart.pending', { n: pending }));
    if (growing) part('crit', T('js.smart.growing'));
    if (item.wear !== undefined) {
      const worn = level(item.wear, limits.wear_warn, null);
      if (worn && !tone) tone = worn;
      part(worn, T('js.smart.wear', { pct: pct(item.wear) }));
    }
    if (item.temp !== undefined) {
      const hot = level(item.temp, limits.temp_warn, null);
      if (hot && !tone) tone = hot;
      part(hot, T('js.smart.temp', { t: Math.round(item.temp) }));
    }
    return '<div class="drive ' + tone + '"><code>' + html(item.device) + '</code>' + parts.join('') + '</div>';
  };
  const showDisks = (host, data) => {
    const list = filesystems(host, data);
    const disks = hosts[host].smart ? drives(host, data) : [];
    each('.disks[data-host=' + quoted(host) + ']', (block) => {
      block.querySelector('.fs-list').innerHTML = list.map(fsRow).join('');
      block.querySelector('.smart-list').innerHTML = disks.map(driveRow).join('');
      const note = block.querySelector('.disks-note');
      note.hidden = list.length > 0;
      note.textContent = T('js.disks.none');
    });
  };
  const disksOff = () => each('.disks', (block) => {
    block.querySelector('.fs-list').innerHTML = '';
    block.querySelector('.smart-list').innerHTML = '';
    const note = block.querySelector('.disks-note');
    note.hidden = false;
    note.textContent = T('js.disks.off');
  });

  const setStrip = (counts, note, off) => {
    strip.hidden = false;
    strip.classList.toggle('off', off);
    strip.classList.toggle('has-down', !off && counts.down > 0);
    ['down', 'live', 'nodata'].forEach((name) => { strip.querySelector('.n.' + name + ' b').textContent = counts[name]; });
    strip.querySelector('.why').textContent = note;
  };
  const clear = () => {
    each('[data-st]', (item) => { delete item.dataset.st; item.removeAttribute('title'); });
    each('.slive, .hlive', (block) => { block.hidden = true; });
  };

  let last = null;
  const paint = () => {
    if (!last) return;
    if (last.off) {
      clear();
      disksOff();
      setStrip({ down: 0, live: 0, nodata: 0 }, T('js.unavailable', { reason: last.reason || T('js.nothing') }), true);
      document.dispatchEvent(new Event('aye:live'));
      return;
    }
    const { data } = last;
    const counts = { down: 0, live: 0, nodata: 0 };
    Object.keys(services).sort().forEach((key) => {
      const info = serviceState(key, services[key], data);
      counts[info.state] += 1;
      showService(key, services[key], info);
    });
    Object.keys(hosts).forEach((host) => {
      if (!hosts[host].node) return;
      showHost(host, data);
      showDisks(host, data);
    });
    const partial = last.failed ? T('js.failed', { failed: last.failed, total: last.total }) : '';
    setStrip(counts, T('js.updated', { time: last.time, interval: spec.interval }) + partial, false);
    document.dispatchEvent(new Event('aye:live'));
  };
  document.addEventListener('aye:lang', paint);

  const poll = async () => {
    const names = Object.keys(spec.queries || {});
    const rangeNames = Object.keys(spec.ranges || {});
    const end = Math.floor(Date.now() / 1000);
    const jobs = names.map((name) => api('query', { query: spec.queries[name] })).concat(
      rangeNames.map((name) => {
        const range = spec.ranges[name];
        return api('query_range', { query: range.query, start: end - range.seconds, end, step: range.step });
      }),
    );
    const settled = await Promise.allSettled(jobs);
    if (!jobs.length || settled.every((item) => item.status === 'rejected')) {
      last = { off: true, reason: jobs.length ? settled[0].reason.message : '' };
      paint();
      return;
    }
    const data = {};
    names.forEach((name, i) => { data[name] = settled[i].status === 'fulfilled' ? settled[i].value : []; });
    rangeNames.forEach((name, i) => {
      const item = settled[names.length + i];
      data[name + '_range'] = item.status === 'fulfilled' ? item.value : [];
    });
    const failed = settled.filter((item) => item.status === 'rejected').length;
    last = { off: false, data, failed, total: jobs.length, time: new Date().toLocaleTimeString() };
    paint();
  };

  let timer = null;
  let busy = false;
  const run = () => {
    clearTimeout(timer);
    if (busy || document.hidden) return;
    busy = true;
    poll().catch(() => {}).finally(() => {
      busy = false;
      if (!document.hidden) timer = setTimeout(run, spec.interval * 1000);
    });
  };
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(timer);
    else run();
  });
  run();
})();
