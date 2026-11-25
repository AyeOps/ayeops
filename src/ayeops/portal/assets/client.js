(() => {
  const root = document.documentElement;
  const store = {
    get: (key) => { try { return localStorage.getItem(key); } catch (error) { return null; } },
    set: (key, value) => { try { localStorage.setItem(key, value); } catch (error) { return; } },
  };
  let I18N = {};
  try { I18N = JSON.parse(document.getElementById('aye-i18n')?.textContent || '{}') || {}; } catch (error) { I18N = {}; }
  const lang = () => (I18N[root.dataset.lang] ? root.dataset.lang : 'en');
  const T = (key, vars) => {
    const table = I18N[lang()] || {};
    let text = table[key] ?? (I18N.en || {})[key] ?? key;
    Object.entries(vars || {}).forEach(([name, value]) => { text = text.split('{' + name + '}').join(String(value)); });
    return text;
  };
  window.ayeT = T;
  const parse = (text) => { try { return JSON.parse(text); } catch (error) { return null; } };
  const applyLang = (next) => {
    root.lang = next;
    root.dataset.lang = next;
    document.querySelectorAll('[data-i18n]').forEach((item) => {
      Object.entries(parse(item.dataset.i18n) || {}).forEach(([name, texts]) => {
        const value = texts[next] ?? texts.en;
        if (value !== undefined) item.setAttribute(name, value);
      });
    });
    document.querySelectorAll('[data-i18n-text]').forEach((item) => {
      const texts = parse(item.dataset.i18nText);
      if (texts) item.textContent = texts[next] ?? texts.en ?? item.textContent;
    });
    document.querySelectorAll('[data-set-lang]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.setLang === next)));
    document.dispatchEvent(new Event('aye:lang'));
  };
  document.querySelectorAll('[data-set-lang]').forEach((button) => button.addEventListener('click', () => {
    store.set('aye-lang', button.dataset.setLang);
    applyLang(button.dataset.setLang);
  }));
  applyLang(lang());
  const isDark = () => (root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches);
  document.querySelector('.theme')?.addEventListener('click', () => {
    root.dataset.theme = isDark() ? 'light' : 'dark';
    store.set('aye-theme', root.dataset.theme);
  });

  document.addEventListener('click', (event) => {
    const copy = event.target.closest('.copy');
    if (!copy || !navigator.clipboard) return;
    navigator.clipboard.writeText(copy.textContent).then(() => {
      copy.classList.add('copied');
      setTimeout(() => copy.classList.remove('copied'), 1200);
    });
  });

  const frames = [...document.querySelectorAll('.frame')];
  const center = (frame) => {
    const canvas = frame.querySelector('.canvas');
    canvas.scrollLeft = (canvas.scrollWidth - canvas.clientWidth) / 2;
    canvas.scrollTop = (canvas.scrollHeight - canvas.clientHeight) / 2;
  };
  const setSize = (actual) => frames.forEach((frame) => {
    const canvas = frame.querySelector('.canvas');
    const share = {
      x: (canvas.scrollLeft + canvas.clientWidth / 2) / (canvas.scrollWidth || 1),
      y: (canvas.scrollTop + canvas.clientHeight / 2) / (canvas.scrollHeight || 1),
    };
    frame.classList.toggle('actual', actual);
    frame.classList.remove('zoomed');
    frame.querySelector('[data-act="size"]').setAttribute('aria-pressed', actual);
    frame.querySelector('[data-act="size"]').textContent = actual ? T('diagram.fit') : T('diagram.size');
    canvas.scrollLeft = share.x * canvas.scrollWidth - canvas.clientWidth / 2;
    canvas.scrollTop = share.y * canvas.scrollHeight - canvas.clientHeight / 2;
  });
  const setFull = (frame, on) => {
    frame.classList.toggle('full', on);
    const button = frame.querySelector('[data-act="full"]');
    button.setAttribute('aria-pressed', on);
    button.textContent = on ? T('diagram.exit') : T('diagram.full');
    center(frame);
  };
  const closeFull = (frame) => {
    setFull(frame, false);
    if (document.fullscreenElement === frame) document.exitFullscreen();
  };
  frames.forEach((frame) => { frame.querySelector('.tools').hidden = false; });
  setSize(store.get('aye-diagram-size') === 'actual');
  document.addEventListener('click', (event) => {
    const act = event.target.closest('.tools button')?.dataset.act;
    const frame = event.target.closest('.frame');
    if (act === 'size') {
      const actual = !frame.classList.contains('actual');
      store.set('aye-diagram-size', actual ? 'actual' : 'fit');
      setSize(actual);
    } else if (act === 'full') {
      if (frame.classList.contains('full')) {
        closeFull(frame);
      } else {
        setFull(frame, true);
        frame.requestFullscreen?.().then(() => center(frame)).catch(() => {});
      }
    }
  });
  document.addEventListener('fullscreenchange', () => {
    frames.forEach((frame) => { if (frame.classList.contains('full') && document.fullscreenElement !== frame) closeFull(frame); });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') frames.forEach((frame) => { if (frame.classList.contains('full')) closeFull(frame); });
  });
  const mac = /Mac|iP(hone|ad|od)/.test(navigator.platform || navigator.userAgent);
  const labelTools = () => frames.forEach((frame) => {
    frame.querySelector('[data-act="size"]').textContent = frame.classList.contains('actual') ? T('diagram.fit') : T('diagram.size');
    frame.querySelector('[data-act="full"]').textContent = frame.classList.contains('full') ? T('diagram.exit') : T('diagram.full');
    frame.querySelector('.hint').textContent = mac ? T('diagram.hint.mac') : T('diagram.hint');
  });
  labelTools();
  document.addEventListener('aye:lang', labelTools);
  const zoomTo = (frame, factor, x, y) => {
    const canvas = frame.querySelector('.canvas');
    const svg = canvas.querySelector('.diagram');
    const width = parseFloat(frame.style.getPropertyValue('--w')) || 1;
    const before = svg.getBoundingClientRect();
    if (!before.width) return;
    const fx = (x - before.left) / before.width;
    const fy = (y - before.top) / before.height;
    const next = Math.min(4, Math.max(0.2, (before.width / width) * factor));
    frame.style.setProperty('--z', String(next));
    frame.classList.add('zoomed');
    const after = svg.getBoundingClientRect();
    canvas.scrollLeft += after.left + fx * after.width - x;
    canvas.scrollTop += after.top + fy * after.height - y;
  };
  frames.forEach((frame) => {
    const canvas = frame.querySelector('.canvas');
    canvas.addEventListener('wheel', (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      zoomTo(frame, Math.exp(-event.deltaY * 0.0015), event.clientX, event.clientY);
    }, { passive: false });
    const points = new Map();
    let drag = null;
    let pinch = null;
    const pannable = () => canvas.scrollWidth > canvas.clientWidth || canvas.scrollHeight > canvas.clientHeight;
    const pair = () => {
      const [a, b] = [...points.values()];
      return { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    };
    canvas.addEventListener('pointerdown', (event) => {
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      points.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (points.size === 2 && event.pointerType !== 'mouse') {
        drag = null;
        pinch = pair();
        points.forEach((point, id) => { try { canvas.setPointerCapture(id); } catch (error) { return; } });
        return;
      }
      if (points.size === 1) {
        drag = { id: event.pointerId, x: event.clientX, y: event.clientY, left: canvas.scrollLeft, top: canvas.scrollTop, moved: false, touch: event.pointerType !== 'mouse' };
      }
    });
    canvas.addEventListener('pointermove', (event) => {
      if (!points.has(event.pointerId)) return;
      points.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pinch && points.size === 2) {
        const now = pair();
        zoomTo(frame, now.dist / pinch.dist, now.x, now.y);
        canvas.scrollLeft -= now.x - pinch.x;
        canvas.scrollTop -= now.y - pinch.y;
        pinch = now;
        canvas.classList.add('drag');
        return;
      }
      if (!drag || drag.id !== event.pointerId) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!drag.moved) {
        if (Math.abs(dx) + Math.abs(dy) < 6) return;
        const swipe = drag.touch && !frame.classList.contains('full') && Math.abs(dy) >= Math.abs(dx);
        if (swipe || !pannable()) { drag = null; return; }
        canvas.setPointerCapture(event.pointerId);
        drag.moved = true;
        canvas.classList.add('drag');
      }
      canvas.scrollLeft = drag.left - dx;
      canvas.scrollTop = drag.top - dy;
    });
    let dragged = false;
    canvas.addEventListener('click', (event) => {
      if (!dragged) return;
      dragged = false;
      event.preventDefault();
      event.stopPropagation();
    }, true);
    const stop = (event) => {
      points.delete(event.pointerId);
      if (points.size < 2) pinch = null;
      if (drag && drag.id !== event.pointerId) return;
      dragged = Boolean(drag?.moved);
      if (dragged) setTimeout(() => { dragged = false; }, 0);
      drag = null;
      if (!pinch) canvas.classList.remove('drag');
    };
    canvas.addEventListener('pointerup', stop);
    canvas.addEventListener('pointercancel', stop);
  });

  const query = document.getElementById('q');
  const quick = [...document.querySelectorAll('.qf')];
  const cards = [...document.querySelectorAll('.svc')];
  const empty = document.querySelector('.qf-empty');
  const strip = document.querySelector('.live-strip');
  const GROUPS = ['filter', 'host', 'kind'];
  const LIVE = ['down', 'nodata', 'live'];
  const chosen = { filter: 'all', host: '', kind: '' };
  const liveOf = (card) => card.querySelector('h3')?.dataset.st || '';
  const STATES = {
    all: () => true,
    red: (card) => card.classList.contains('red'),
    described: (card) => !card.classList.contains('red'),
    down: (card) => liveOf(card) === 'down',
    nodata: (card) => liveOf(card) === 'nodata',
    live: (card) => liveOf(card) === 'live',
    planned: (card) => card.dataset.state === 'planned',
    disabled: (card) => card.dataset.state === 'disabled',
  };
  const words = () => (query ? query.value.toLowerCase().split(' ').filter(Boolean) : []);
  const holds = (card, group, value) => {
    if (group === 'filter') return (STATES[value] || STATES.all)(card);
    return !value || card.dataset[group] === value;
  };
  const fits = (card, terms, group, value) => terms.every((word) => card.dataset.search.includes(word))
    && GROUPS.every((name) => holds(card, name, name === group ? value : chosen[name]));
  const writeHash = () => {
    const params = new URLSearchParams();
    GROUPS.forEach((name) => { if (chosen[name] && chosen[name] !== 'all') params.set(name, chosen[name]); });
    if (query && query.value) params.set('q', query.value);
    const text = params.toString();
    history.replaceState(null, '', text ? '#' + text : location.pathname + location.search);
  };
  const readHash = () => {
    const text = location.hash.slice(1);
    if (!text.includes('=')) return false;
    const params = new URLSearchParams(text);
    GROUPS.forEach((name) => { chosen[name] = params.get(name) || (name === 'filter' ? 'all' : ''); });
    if (!STATES[chosen.filter]) chosen.filter = 'all';
    if (query) query.value = params.get('q') || '';
    return true;
  };
  const applyFilters = () => {
    if (!cards.length) return;
    const terms = words();
    const active = terms.length || chosen.filter !== 'all' || chosen.host || chosen.kind;
    cards.forEach((card) => { card.hidden = !fits(card, terms); });
    document.querySelectorAll('.group').forEach((group) => { group.hidden = Boolean(active) && !group.querySelector('.svc:not([hidden])'); });
    if (empty) empty.hidden = cards.some((card) => !card.hidden);
    const live = Boolean(strip) && !strip.hidden && !strip.classList.contains('off');
    quick.forEach((chip) => {
      const { group, value } = chip.dataset;
      chip.setAttribute('aria-pressed', String(chosen[group] === value));
      chip.querySelector('b').textContent = cards.filter((card) => fits(card, terms, group, value)).length;
      if (group === 'filter' && LIVE.includes(value)) chip.hidden = !live && chosen.filter !== value;
    });
  };
  quick.forEach((chip) => chip.addEventListener('click', () => {
    const { group, value } = chip.dataset;
    if (group === 'filter') chosen.filter = value;
    else chosen[group] = chosen[group] === value ? '' : value;
    applyFilters();
    writeHash();
  }));
  document.querySelector('.qf-clear')?.addEventListener('click', () => {
    chosen.filter = 'all';
    chosen.host = '';
    chosen.kind = '';
    if (query) query.value = '';
    applyFilters();
    writeHash();
    query?.focus();
  });
  query?.addEventListener('input', () => { applyFilters(); writeHash(); });
  window.addEventListener('hashchange', () => { if (readHash()) applyFilters(); });
  document.addEventListener('aye:live', applyFilters);
  if (readHash()) applyFilters();

  document.querySelectorAll('svg.diagram').forEach((svg) => {
    const edges = [...svg.querySelectorAll('.edge')];
    const nodes = [...svg.querySelectorAll('.node')];
    const dots = [...svg.querySelectorAll('.dot')];
    const off = () => {
      svg.classList.remove('focus');
      svg.querySelectorAll('.on').forEach((item) => item.classList.remove('on'));
    };
    nodes.forEach((node) => {
      const id = node.dataset.node;
      const on = () => {
        svg.classList.add('focus');
        node.classList.add('on');
        edges.filter((edge) => edge.dataset.a === id || edge.dataset.b === id).forEach((edge) => {
          edge.classList.add('on');
          const path = edge.querySelector('path').id;
          dots.filter((dot) => dot.dataset.e === path).forEach((dot) => dot.classList.add('on'));
          nodes.filter((other) => other.dataset.node === edge.dataset.a || other.dataset.node === edge.dataset.b)
            .forEach((other) => other.classList.add('on'));
        });
      };
      node.addEventListener('mouseenter', on);
      node.addEventListener('mouseleave', off);
      node.addEventListener('focus', on);
      node.addEventListener('blur', off);
    });
  });

  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const diagrams = document.querySelectorAll('svg.diagram');
  if (reduced) {
    diagrams.forEach((svg) => svg.pauseAnimations?.());
  } else if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries) => entries.forEach((entry) => {
      entry.target[entry.isIntersecting ? 'unpauseAnimations' : 'pauseAnimations']?.();
    }));
    diagrams.forEach((svg) => observer.observe(svg));
  }

  const waiversLeft = () => {
    const now = new Date();
    const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
    document.querySelectorAll('[data-until]').forEach((item) => {
      const [year, month, day] = item.dataset.until.split('-').map(Number);
      const left = Math.round((Date.UTC(year, month - 1, day) - today) / 86400000);
      if (Number.isNaN(left)) return;
      if (left < 0) item.textContent = T('js.waiver.over', { n: -left });
      else if (left === 0) item.textContent = T('js.waiver.today');
      else item.textContent = T('js.waiver.left', { n: left });
    });
  };
  waiversLeft();
  document.addEventListener('aye:lang', waiversLeft);
})();
