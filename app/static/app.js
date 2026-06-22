  const $ = id => document.getElementById(id);
  const esc = s => String(s||'').replace(/[&<>"']/g,
    c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  const STATUSES = ['running','exited','paused','restarting','dead'];

  // ── Tabs ────────────────────────────────────────────────────────────────────

  function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === name)
    );
    document.querySelectorAll('.tab-panel').forEach(p =>
      p.classList.toggle('active', p.id === 'tab-' + name)
    );
    try { localStorage.setItem('dboard-tab', name); } catch(_) {}
  }

  // Restore last active tab
  switchTab((() => { try { return localStorage.getItem('dboard-tab') || 'containers'; } catch(_) { return 'containers'; } })());

  // ── Render helpers ──────────────────────────────────────────────────────────

  function badge(status) {
    const cls = STATUSES.includes(status) ? status : 'dead';
    return `<span class="badge badge-${cls}"><span class="badge-dot"></span>${esc(status)}</span>`;
  }

  function barColor(pct) {
    if (pct > 80) return '#f87171';
    if (pct > 55) return '#fb923c';
    return '#818cf8';
  }

  function statCell(val, pct, unit, spark) {
    if (val == null) return '<td class="text-gray-700 mono text-xs">—</td>';
    const color = barColor(pct);
    const w = Math.min(Math.max(pct||0, 0), 100).toFixed(1);
    const sp = (spark && spark.length >= 2)
      ? '<div style="margin-top:3px">' + sparkline(spark, color, 52, 10) + '</div>'
      : '';
    return `<td><div class="stat-val" style="color:${color}">${val}<span class="text-gray-600" style="font-size:.65rem">${unit}</span></div><div class="stat-bar-wrap"><div class="stat-bar-fill" style="width:${w}%;background:${color}"></div></div>${sp}</td>`;
  }

  function memCell(c) {
    if (c.mem_mb == null) return '<td class="text-gray-700 mono text-xs">—</td>';
    const lbl = (c.mem_limit_mb > 0 && c.mem_limit_mb < 999999)
      ? `${c.mem_mb}<span style="color:#4b5563;font-size:.65rem">/${c.mem_limit_mb}</span>`
      : `${c.mem_mb}`;
    const color = barColor(c.mem_percent);
    const w = Math.min(c.mem_percent||0, 100).toFixed(1);
    const sp = (c.mem_spark && c.mem_spark.length >= 2)
      ? '<div style="margin-top:3px">' + sparkline(c.mem_spark, color, 52, 10) + '</div>'
      : '';
    return `<td><div class="stat-val" style="color:${color}">${lbl}<span class="text-gray-600" style="font-size:.65rem"> MB</span></div><div class="stat-bar-wrap"><div class="stat-bar-fill" style="width:${w}%;background:${color}"></div></div>${sp}</td>`;
  }

  function fmtBytes(b) {
    if (b == null) return '—';
    if (b < 1024)             return b + ' B';
    if (b < 1048576)          return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824)       return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
  }

  function netCell(rx, tx) {
    if (rx == null) return '<td class="text-gray-700 mono text-xs">—</td>';
    return `<td><div class="mono" style="font-size:.68rem;line-height:1.6"><span style="color:#34d399">↓${fmtBytes(rx)}</span><br><span style="color:#60a5fa">↑${fmtBytes(tx)}</span></div></td>`;
  }

  function uptimeBadge(uptime) {
    return uptime
      ? `<span class="mono text-xs text-gray-400">↑${esc(uptime)}</span>`
      : '<span class="text-gray-700 mono text-xs">—</span>';
  }

  function healthBadge(health) {
    return health
      ? `<span class="mono text-xs h-${esc(health)}">${esc(health)}</span>`
      : '<span class="text-gray-700 mono text-xs">—</span>';
  }

  function rowLevel(c) {
    if (c.status !== 'running') return '';
    const m = Math.max(c.cpu_percent || 0, c.mem_percent || 0);
    if (m >= 90) return 'row-crit';
    if (m >= 75) return 'row-warn';
    return '';
  }

  function renderProxied(rows) {
    if (!rows.length) return '<tr class="empty-row"><td colspan="9">No matching containers</td></tr>';
    return rows.map(c => {
      const chips = (c.domains||[])
        .map(d => `<a href="https://${esc(d)}" target="_blank" class="chip">${esc(d)}</a>`)
        .join('') || '<span class="text-gray-700 text-xs mono">—</span>';
      return `<tr class="${rowLevel(c)} row-click" data-cname="${esc(c.name)}">
        <td>${badge(c.status)}</td>
        <td><span class="font-semibold text-white text-sm">${esc(c.name)}</span></td>
        <td style="max-width:260px">${chips}</td>
        <td class="hidden sm:table-cell mono text-xs text-gray-500" style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${esc(c.image)}">${esc(c.image)}</td>
        <td>${healthBadge(c.health)}</td>
        <td>${uptimeBadge(c.uptime)}</td>
        ${c.cpu_percent != null ? statCell(c.cpu_percent, c.cpu_percent, '%', c.cpu_spark) : '<td class="text-gray-700 mono text-xs">—</td>'}
        ${memCell(c)}
        ${netCell(c.net_rx, c.net_tx)}
      </tr>`;
    }).join('');
  }

  function renderOthers(rows) {
    if (!rows.length) return '<tr class="empty-row"><td colspan="8">No matching containers</td></tr>';
    return rows.map(c => `<tr class="${rowLevel(c)} row-click" data-cname="${esc(c.name)}">
      <td>${badge(c.status)}</td>
      <td><span class="font-semibold text-white text-sm">${esc(c.name)}</span></td>
      <td class="mono text-xs text-gray-500" style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${esc(c.image)}">${esc(c.image)}</td>
      <td>${healthBadge(c.health)}</td>
      <td>${uptimeBadge(c.uptime)}</td>
      ${c.cpu_percent != null ? statCell(c.cpu_percent, c.cpu_percent, '%', c.cpu_spark) : '<td class="text-gray-700 mono text-xs">—</td>'}
      ${memCell(c)}
      ${netCell(c.net_rx, c.net_tx)}
    </tr>`).join('');
  }

  // ── System stats ────────────────────────────────────────────────────────────

  function sysBarColor(pct) {
    if (pct > 85) return '#f87171';
    if (pct > 65) return '#fb923c';
    return '#34d399';
  }

  function fmtMb(mb) {
    return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb + ' MB';
  }

  function thresholdClass(pct, warnAt, critAt) {
    warnAt = warnAt || 70; critAt = critAt || 85;
    if (pct >= critAt) return 'crit';
    if (pct >= warnAt) return 'warn';
    return '';
  }

  function sparkline(data, color, w, h) {
    w = w || 80; h = h || 18;
    if (!data || data.length < 2) return '';
    const max = Math.max.apply(null, data.concat(0.001));
    const pts = data.map(function(v, i) {
      const x = (i / (data.length - 1)) * w;
      const y = h - (v / max) * h * 0.88 + h * 0.06;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    return '<svg width="' + w + '" height="' + h + '" style="display:block;overflow:visible" viewBox="0 0 ' + w + ' ' + h + '">' +
      '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.5" ' +
      'stroke-linejoin="round" stroke-linecap="round" opacity=".65"/></svg>';
  }

  function sparklineDual(d1, c1, d2, c2, w, h) {
    w = w || 80; h = h || 22;
    const all = (d1 || []).concat(d2 || []);
    if (all.length < 2) return '';
    const max = Math.max.apply(null, all.concat(0.001));
    function pts(data) {
      if (!data || data.length < 2) return '';
      return data.map(function(v, i) {
        const x = (i / (data.length - 1)) * w;
        const y = h - (v / max) * h * 0.88 + h * 0.06;
        return x.toFixed(1) + ',' + y.toFixed(1);
      }).join(' ');
    }
    let svg = '<svg width="' + w + '" height="' + h + '" style="display:block;overflow:visible" viewBox="0 0 ' + w + ' ' + h + '">';
    const p1 = pts(d1), p2 = pts(d2);
    if (p1) svg += '<polyline points="' + p1 + '" fill="none" stroke="' + c1 + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" opacity=".65"/>';
    if (p2) svg += '<polyline points="' + p2 + '" fill="none" stroke="' + c2 + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" opacity=".65"/>';
    svg += '</svg>';
    return svg;
  }

  function sysCard(label, valueHtml, pct, sub, spark, sparkColor, warnAt, critAt) {
    const color = sysBarColor(pct);
    const w = Math.min(pct || 0, 100).toFixed(1);
    const level = thresholdClass(pct, warnAt, critAt);
    const sparkHtml = spark
      ? '<div style="margin:.25rem 0 .1rem">' + sparkline(spark, sparkColor || color) + '</div>'
      : '';
    return `<div class="sys-card ${level}">
      <div class="sys-card-label">${label}</div>
      <div class="sys-card-value" style="color:${color}">${valueHtml}</div>
      <div class="sys-bar-wrap"><div class="sys-bar-fill" style="width:${w}%;background:${color}"></div></div>
      ${sparkHtml}
      <div class="sys-card-sub">${sub}</div>
    </div>`;
  }

  function renderSystem(s) {
    lastSys = s;
    let html = '';
    const sp = s.sparklines || {};

    // CPU
    const loadStr = s.load_avg
      ? `load ${s.load_avg[0]} ${s.load_avg[1]} ${s.load_avg[2]}`
      : '';
    html += sysCard(
      'CPU',
      `${s.cpu_percent}<span style="font-size:.7rem;color:#6b7280">%</span>`,
      s.cpu_percent,
      `${s.cpu_phys} cores (${s.cpu_count} threads)${loadStr ? ' · ' + loadStr : ''}`,
      sp.cpu, null, 70, 90
    );

    // RAM
    html += sysCard(
      'RAM',
      `<span style="font-size:.95rem">${fmtMb(s.mem_used_mb)}</span>`,
      s.mem_percent,
      `${s.mem_percent}% of ${fmtMb(s.mem_total_mb)}`,
      sp.mem, null, 80, 92
    );

    // CPU Temperature
    if (s.cpu_temp != null) {
      const tempPct = Math.min(s.cpu_temp, 100);
      const tempSub = s.cpu_temp >= 85 ? 'critical' : s.cpu_temp >= 70 ? 'warm' : 'nominal';
      html += sysCard(
        'CPU Temp',
        `${s.cpu_temp}<span style="font-size:.7rem;color:#6b7280">°C</span>`,
        tempPct,
        tempSub,
        sp.temp, '#fb923c', 70, 85
      );
    }

    // Swap (only if exists)
    if (s.swap_total_mb > 0) {
      html += sysCard(
        'Swap',
        `<span style="font-size:.95rem">${fmtMb(s.swap_used_mb)}</span>`,
        s.swap_percent,
        `${s.swap_percent}% of ${fmtMb(s.swap_total_mb)}`
      );
    }

    // Disks
    for (const d of (s.disks || [])) {
      html += sysCard(
        `Disk ${esc(d.mount)}`,
        `<span style="font-size:.9rem">${d.used_gb} <span style="font-size:.65rem;color:#6b7280">/ ${d.total_gb} GB</span></span>`,
        d.percent,
        `${d.percent}% · ${d.free_gb} GB free`,
        null, null, 80, 92
      );
    }

    // Net I/O rate
    if (s.net_rate) {
      const netSpark = (sp.net_rx || sp.net_tx)
        ? '<div style="margin:.25rem 0 .1rem">' + sparklineDual(sp.net_rx, '#34d399', sp.net_tx, '#60a5fa') + '</div>'
        : '';
      html += `<div class="sys-card">
        <div class="sys-card-label">Net I/O</div>
        <div class="sys-card-value" style="font-size:.85rem;padding-top:.15rem;line-height:1.7">
          <span style="color:#34d399">↓${fmtBytes(s.net_rate.rx_bps)}/s</span><br>
          <span style="color:#60a5fa">↑${fmtBytes(s.net_rate.tx_bps)}/s</span>
        </div>
        ${netSpark}
        <div class="sys-card-sub">all interfaces · live</div>
      </div>`;
    }

    // Disk I/O rate
    if (s.disk_rate) {
      const diskSpark = (sp.disk_r || sp.disk_w)
        ? '<div style="margin:.25rem 0 .1rem">' + sparklineDual(sp.disk_r, '#f59e0b', sp.disk_w, '#a78bfa') + '</div>'
        : '';
      html += `<div class="sys-card">
        <div class="sys-card-label">Disk I/O</div>
        <div class="sys-card-value" style="font-size:.85rem;padding-top:.15rem;line-height:1.7">
          <span style="color:#f59e0b">↓${fmtBytes(s.disk_rate.read_bps)}/s</span><br>
          <span style="color:#a78bfa">↑${fmtBytes(s.disk_rate.write_bps)}/s</span>
        </div>
        ${diskSpark}
        <div class="sys-card-sub">all disks · live</div>
      </div>`;
    }

    // Uptime card (no bar)
    if (s.uptime) {
      html += `<div class="sys-card">
        <div class="sys-card-label">Uptime</div>
        <div class="sys-card-value" style="color:#818cf8;font-size:1rem;padding-top:.2rem">${esc(s.uptime)}</div>
        <div class="sys-bar-wrap" style="background:transparent"></div>
        <div class="sys-card-sub">since last boot</div>
      </div>`;
    }

    $('sys-grid').innerHTML = html;
  }

  function xhrJson(url) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('GET', url);
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try { resolve(JSON.parse(xhr.responseText)); }
          catch(e) { reject(new Error('JSON parse error')); }
        } else {
          reject(new Error(`HTTP ${xhr.status}`));
        }
      };
      xhr.onerror   = () => reject(new Error('network error'));
      xhr.ontimeout = () => reject(new Error('timeout'));
      xhr.timeout = 10000;
      xhr.send();
    });
  }

  // ── Sort & filter ───────────────────────────────────────────────────────────

  const rawData = { proxied: [], others: [] };
  let sampleInterval = 5;   // seconds between samples (from the API)
  let lastSys = null;       // latest /api/system payload (for the system overlay)
  const sortState = {
    proxied: { col: null, dir: 1 },
    others:  { col: null, dir: 1 },
  };

  function uptimeSecs(s) {
    if (!s) return -1;
    let t = 0;
    const d = s.match(/(\d+)d/); if (d) t += +d[1] * 86400;
    const h = s.match(/(\d+)h/); if (h) t += +h[1] * 3600;
    const m = s.match(/(\d+)m/); if (m) t += +m[1] * 60;
    return t;
  }

  const colKey = {
    status:  c => c.status,
    name:    c => c.name.toLowerCase(),
    domains: c => (c.domains||[]).join(',').toLowerCase(),
    image:   c => (c.image||'').toLowerCase(),
    health:  c => c.health || '',
    uptime:  c => uptimeSecs(c.uptime),
    cpu:     c => c.cpu_percent ?? -1,
    ram:     c => c.mem_mb ?? -1,
    net:     c => (c.net_rx||0) + (c.net_tx||0),
  };

  function applyFilter(rows, q) {
    if (!q) return rows;
    const lq = q.toLowerCase();
    return rows.filter(c =>
      c.name.toLowerCase().includes(lq) ||
      (c.image||'').toLowerCase().includes(lq) ||
      (c.status||'').toLowerCase().includes(lq) ||
      (c.health||'').toLowerCase().includes(lq) ||
      (c.domains||[]).some(d => d.toLowerCase().includes(lq))
    );
  }

  function applySort(rows, table) {
    const { col, dir } = sortState[table];
    if (!col) return rows;
    const fn = colKey[col];
    return [...rows].sort((a, b) => {
      const av = fn(a), bv = fn(b);
      return av < bv ? -dir : av > bv ? dir : 0;
    });
  }

  function sortTable(table, col) {
    if (sortState[table].col === col) {
      sortState[table].dir *= -1;
    } else {
      sortState[table].col = col;
      sortState[table].dir = 1;
    }
    document.querySelectorAll(`th[data-table="${table}"]`).forEach(th => {
      const active = th.dataset.col === col;
      th.classList.toggle('th-active', active);
      const icon = th.querySelector('.sort-icon');
      if (icon) icon.textContent = active ? (sortState[table].dir === 1 ? ' ↑' : ' ↓') : '';
    });
    redraw(table);
  }

  function redraw(table) {
    const q = ($(`filter-${table}`) || {value:''}).value.trim();
    let rows = applyFilter(rawData[table], q);
    rows = applySort(rows, table);
    $(`${table}-body`).innerHTML = (table === 'proxied' ? renderProxied : renderOthers)(rows);
  }

  // ── Tokens ──────────────────────────────────────────────────────────────────

  function renderTokens(tokens) {
    if (!tokens.length) {
      $('tok-grid').innerHTML = '<span style="color:#374151;font-size:.8rem;font-style:italic">No tokens configured</span>';
      return;
    }
    tokens = tokens.filter(t => t.configured);
    const configured = tokens;
    const valid = configured.filter(t => t.valid).length;
    $('lbl-tokens').textContent = configured.length
      ? `${valid} valid / ${configured.length} configured`
      : 'none configured';
    $('badge-tokens').textContent = configured.length
      ? `${valid} / ${configured.length}`
      : '';

    $('tok-grid').innerHTML = tokens.map(t => {
      const cls = !t.configured ? 'unconfigured' : t.valid ? 'valid' : 'invalid';
      const statusLabel = !t.configured ? 'not set' : t.valid ? 'valid' : 'invalid';
      const detail = t.error || t.detail || '—';
      const extras = t.extras || [];
      const checkedAt = t.checked_at
        ? `checked ${new Date(t.checked_at).toLocaleTimeString()}`
        : '';
      const extrasHtml = extras.length
        ? `<div class="tok-extras">${extras.map(e =>
            `<span class="tok-extra-label">${esc(e.label)}</span><span class="tok-extra-value">${esc(e.value)}</span>`
          ).join('')}</div>`
        : '';
      return `<div class="tok-card ${cls}">
        <div class="tok-name">
          ${esc(t.name)}
          <span class="tok-status-dot ${cls}" title="${statusLabel}"></span>
        </div>
        ${t.key_hint ? `<div class="tok-hint">${esc(t.key_hint)}</div>` : ''}
        <div class="tok-detail ${cls}">${esc(detail)}</div>
        ${extrasHtml}
        ${checkedAt ? `<div class="tok-checked">${checkedAt}</div>` : ''}
      </div>`;
    }).join('');
  }

  async function refreshTokens(force = false) {
    const btn = $('tok-refresh-btn');
    if (btn) btn.disabled = true;
    try {
      const data = await xhrJson(`/api/tokens${force ? '?refresh=true' : ''}`);
      renderTokens(data.tokens || []);
    } catch(e) {
      console.error('tokens fetch error', e);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── Error / XHR ─────────────────────────────────────────────────────────────

  function showError(msg) {
    $('error-text').textContent = msg;
    $('error-banner').style.display = 'flex';
  }
  function hideError() { $('error-banner').style.display = 'none'; }

  // ── Container detail overlay ────────────────────────────────────────────────
  let _gid = 0;
  let activeDetail = null;    // container name when detailKind === 'container'
  let detailKind = null;      // null | 'container' | 'system'
  let detailRange = 'live';   // 'live' (ring buffer) | seconds (DB history)
  let lastHistLoad = 0;

  function fmtSpan(sec) {
    sec = Math.round(sec);
    if (sec >= 90) return '-' + Math.round(sec / 60) + 'm';
    return '-' + sec + 's';
  }

  // Area/line chart with an X (time) axis. `series` = [{data, color}, ...];
  // `interval` is the seconds between samples (for the axis labels).
  function chart(series, interval, h) {
    const w = 320; h = h || 66;
    const valid = series
      .map(s => ({ data: (s.data || []).filter(v => typeof v === 'number'), color: s.color }))
      .filter(s => s.data.length >= 2);
    if (!valid.length) {
      return `<div class="mono" style="height:${h + 16}px;display:flex;align-items:center;color:#374151;font-size:.7rem">no history yet</div>`;
    }
    // Shared scale across all series: min–max with headroom so a near-constant
    // series sits centred as a line instead of filling the card to the top.
    const all = valid.reduce((a, s) => a.concat(s.data), []);
    const lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    const pad = (hi - lo) * 0.25 || Math.abs(hi) * 0.25 || 1;
    const ymin = lo - pad, span = (hi + pad) - ymin || 1;
    const Y = v => (h - ((v - ymin) / span) * (h * 0.82) - h * 0.09).toFixed(1);
    const X = (i, n) => ((i / (n - 1)) * w).toFixed(1);

    let svg = `<svg width="100%" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="display:block;overflow:visible">`;
    // X-axis baseline + a mid gridline
    svg += `<line x1="0" y1="${h - 0.5}" x2="${w}" y2="${h - 0.5}" stroke="#ffffff" stroke-opacity="0.10" stroke-width="1" vector-effect="non-scaling-stroke"/>`;
    svg += `<line x1="${(w / 2).toFixed(1)}" y1="0" x2="${(w / 2).toFixed(1)}" y2="${h}" stroke="#ffffff" stroke-opacity="0.05" stroke-width="1" vector-effect="non-scaling-stroke"/>`;
    valid.forEach(s => {
      const n = s.data.length, id = 'ovg' + (_gid++);
      const line = s.data.map((v, i) => X(i, n) + ',' + Y(v)).join(' ');
      const area = 'M' + s.data.map((v, i) => X(i, n) + ' ' + Y(v)).join(' L ') + ` L ${w} ${h} L 0 ${h} Z`;
      svg += `<defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">`
        + `<stop offset="0" stop-color="${s.color}" stop-opacity="0.28"/>`
        + `<stop offset="1" stop-color="${s.color}" stop-opacity="0"/></linearGradient></defs>`
        + `<path d="${area}" fill="url(#${id})"/>`
        + `<polyline points="${line}" fill="none" stroke="${s.color}" stroke-width="2" `
        + `stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>`;
    });
    svg += `</svg>`;

    const maxN = Math.max.apply(null, valid.map(s => s.data.length));
    const spanSec = (maxN - 1) * (interval || 5);
    const axis = `<div class="ov-axis"><span>${fmtSpan(spanSec)}</span><span>${fmtSpan(spanSec / 2)}</span><span>now</span></div>`;
    return `<div class="ov-chart-wrap">${svg}${axis}</div>`;
  }

  function findContainer(name) {
    return rawData.proxied.concat(rawData.others).find(c => c.name === name) || null;
  }

  function detailMetric(label, valHtml, chart, cls) {
    return `<div class="ov-metric ${cls || ''}">
      <div class="ov-metric-label">${label}</div>
      <div class="ov-metric-val">${valHtml}</div>
      <div class="ov-chart">${chart}</div></div>`;
  }

  // `s` holds the chart series for the selected range:
  // { cpu, mem, net_rx, net_tx, interval }
  function renderDetail(c, s) {
    const statusCls = STATUSES.includes(c.status) ? c.status : 'dead';
    const dot = { running:'#4ade80', exited:'#f87171', paused:'#fbbf24', restarting:'#60a5fa', dead:'#6b7280' }[statusCls];
    const cpuColor = barColor(c.cpu_percent || 0), memColor = barColor(c.mem_percent || 0);

    const memVal = (c.mem_limit_mb > 0 && c.mem_limit_mb < 999999)
      ? `<span style="color:${memColor}">${c.mem_mb}<small> / ${c.mem_limit_mb} MB · ${c.mem_percent ?? '–'}%</small></span>`
      : `<span style="color:${memColor}">${c.mem_mb ?? '—'}<small> MB</small></span>`;

    const sub = [esc(c.image || ''), c.uptime ? '↑' + esc(c.uptime) : null]
      .filter(Boolean)
      .map(s => `<span>${s}</span>`)
      .join('<span style="color:#374151">·</span>');

    const chips = (c.domains || []).length
      ? `<div class="ov-chips">${c.domains.map(d => `<a href="https://${esc(d)}" target="_blank" class="chip">${esc(d)}</a>`).join('')}</div>`
      : '';

    const iv = s.interval || sampleInterval;
    const cpuCard = (c.cpu_percent != null)
      ? detailMetric('CPU', `<span style="color:${cpuColor}">${c.cpu_percent}<small>%</small></span>`,
          chart([{ data: s.cpu, color: cpuColor }], iv), thresholdClass(c.cpu_percent, 75, 90))
      : detailMetric('CPU', '<span class="text-gray-600">—</span>', '');
    const memCard = (c.mem_mb != null)
      ? detailMetric('Memory', memVal, chart([{ data: s.mem, color: memColor }], iv), thresholdClass(c.mem_percent || 0, 80, 92))
      : detailMetric('Memory', '<span class="text-gray-600">—</span>', '');

    const rxRate = c.net_rx_rate != null ? fmtBytes(c.net_rx_rate) + '/s' : '—';
    const txRate = c.net_tx_rate != null ? fmtBytes(c.net_tx_rate) + '/s' : '—';
    const netChart = chart([
      { data: s.net_rx, color: '#34d399' },
      { data: s.net_tx, color: '#60a5fa' },
    ], iv);
    const netCard = `<div class="ov-metric span2">
      <div class="ov-metric-label" style="display:flex;justify-content:space-between;align-items:center">
        <span>Network I/O</span>
        <span class="ov-legend"><span style="color:#34d399">● rx</span><span style="color:#60a5fa">● tx</span></span>
      </div>
      <div class="ov-net"><span style="color:#34d399">↓ ${rxRate}</span><span style="color:#60a5fa">↑ ${txRate}</span></div>
      <div class="ov-chart">${netChart}</div>
      <div class="ov-net-total mono">total ↓ ${fmtBytes(c.net_rx)} · ↑ ${fmtBytes(c.net_tx)}</div></div>`;

    const details = [
      ['Status', `<span style="color:${dot}">${esc(c.status)}</span>`],
      ['Health', c.health ? esc(c.health) : '—'],
      ['Uptime', c.uptime ? '↑' + esc(c.uptime) : '—'],
      ['Image', esc(c.image || '—')],
    ];
    const detailsHtml = `<div class="ov-details">${details.map(d =>
      `<div><div class="ov-detail-k">${d[0]}</div><div class="ov-detail-v">${d[1]}</div></div>`).join('')}</div>`;

    return `<div class="ov-head"><div>
        <div class="ov-title"><span class="ov-dot" style="background:${dot};box-shadow:0 0 8px ${dot}99"></span><h2>${esc(c.name)}</h2></div>
        <div class="ov-sub">${sub}</div>${chips}
      </div>
      <button class="ov-close" aria-label="Close">×</button></div>
      ${rangeSelector()}
      <div class="ov-metrics">${cpuCard}${memCard}${netCard}</div>
      ${detailsHtml}`;
  }

  function rangeSelector() {
    const ranges = [['live', 'live'], ['3600', '1h'], ['21600', '6h'], ['86400', '24h']];
    return `<div class="ov-ranges">${ranges.map(([v, l]) =>
      `<button class="ov-range-btn ${String(detailRange) === v ? 'active' : ''}" data-range="${v}">${l}</button>`).join('')}</div>`;
  }

  // ── System overlay ──────────────────────────────────────────────────────────
  function systemLiveSeries(sys) {
    const sp = sys.sparklines || {};
    return {
      cpu: sp.cpu, mem: sp.mem, temp: sp.temp,
      net_rx: sp.net_rx, net_tx: sp.net_tx, disk_r: sp.disk_r, disk_w: sp.disk_w,
      interval: sampleInterval,
    };
  }

  function renderSystemDetail(sys, s) {
    const iv = s.interval || sampleInterval;
    const cpuColor = sysBarColor(sys.cpu_percent || 0);
    const memColor = sysBarColor(sys.mem_percent || 0);

    const cpuCard = detailMetric('CPU',
      `<span style="color:${cpuColor}">${sys.cpu_percent}<small>%</small></span>`,
      chart([{ data: s.cpu, color: cpuColor }], iv), thresholdClass(sys.cpu_percent || 0, 70, 90));

    const memCard = detailMetric('Memory',
      `<span style="color:${memColor}">${fmtMb(sys.mem_used_mb)}<small> / ${fmtMb(sys.mem_total_mb)} · ${sys.mem_percent}%</small></span>`,
      chart([{ data: s.mem, color: memColor }], iv), thresholdClass(sys.mem_percent || 0, 80, 92));

    let tempCard = '';
    if (sys.cpu_temp != null) {
      const tColor = sys.cpu_temp >= 85 ? '#f87171' : sys.cpu_temp >= 70 ? '#fb923c' : '#34d399';
      tempCard = detailMetric('CPU Temp',
        `<span style="color:${tColor}">${sys.cpu_temp}<small>°C</small></span>`,
        chart([{ data: s.temp, color: '#fb923c' }], iv), thresholdClass(sys.cpu_temp, 70, 85));
    }

    const nr = sys.net_rate || {}, dr = sys.disk_rate || {};
    const ioCard = (label, aLbl, aVal, aColor, bLbl, bVal, bColor, da, db) => `<div class="ov-metric span2">
      <div class="ov-metric-label" style="display:flex;justify-content:space-between;align-items:center">
        <span>${label}</span>
        <span class="ov-legend"><span style="color:${aColor}">● ${aLbl}</span><span style="color:${bColor}">● ${bLbl}</span></span>
      </div>
      <div class="ov-net"><span style="color:${aColor}">${aVal}</span><span style="color:${bColor}">${bVal}</span></div>
      <div class="ov-chart">${chart([{ data: da, color: aColor }, { data: db, color: bColor }], iv)}</div></div>`;

    const netCard = ioCard('Network I/O',
      'rx', '↓ ' + (nr.rx_bps != null ? fmtBytes(nr.rx_bps) + '/s' : '—'), '#34d399',
      'tx', '↑ ' + (nr.tx_bps != null ? fmtBytes(nr.tx_bps) + '/s' : '—'), '#60a5fa',
      s.net_rx, s.net_tx);
    const diskCard = ioCard('Disk I/O',
      'read', 'R ' + (dr.read_bps != null ? fmtBytes(dr.read_bps) + '/s' : '—'), '#f59e0b',
      'write', 'W ' + (dr.write_bps != null ? fmtBytes(dr.write_bps) + '/s' : '—'), '#a78bfa',
      s.disk_r, s.disk_w);

    const details = [];
    if (sys.uptime) details.push(['Uptime', esc(sys.uptime)]);
    details.push(['CPU', `${sys.cpu_phys} cores / ${sys.cpu_count} threads`]);
    if (sys.load_avg) details.push(['Load avg', sys.load_avg.join('  ')]);
    if (sys.swap_total_mb > 0) details.push(['Swap', `${fmtMb(sys.swap_used_mb)} / ${fmtMb(sys.swap_total_mb)} (${sys.swap_percent}%)`]);
    for (const d of (sys.disks || [])) details.push([`Disk ${esc(d.mount)}`, `${d.used_gb} / ${d.total_gb} GB (${d.percent}%)`]);
    const detailsHtml = `<div class="ov-details">${details.map(d =>
      `<div><div class="ov-detail-k">${d[0]}</div><div class="ov-detail-v">${d[1]}</div></div>`).join('')}</div>`;

    const sub = [`${sys.cpu_phys} cores`, sys.uptime ? '↑' + esc(sys.uptime) : null]
      .filter(Boolean).map(x => `<span>${x}</span>`).join('<span style="color:#374151">·</span>');

    return `<div class="ov-head"><div>
        <div class="ov-title"><span class="ov-dot" style="background:#a78bfa;box-shadow:0 0 8px #a78bfa99"></span><h2>System</h2></div>
        <div class="ov-sub">${sub}</div>
      </div>
      <button class="ov-close" aria-label="Close">×</button></div>
      ${rangeSelector()}
      <div class="ov-metrics">${cpuCard}${memCard}${tempCard}${netCard}${diskCard}</div>
      ${detailsHtml}`;
  }

  // Live series straight from the in-memory ring buffers (~10 min, auto-updating).
  function liveSeries(c) {
    return {
      cpu: c.cpu_spark, mem: c.mem_spark,
      net_rx: c.net_rx_spark, net_tx: c.net_tx_spark,
      interval: sampleInterval,
    };
  }

  function renderContainerInto(c, s) { $('detail-panel').innerHTML = renderDetail(c, s); }
  function renderSystemInto(sys, s) { $('detail-panel').innerHTML = renderSystemDetail(sys, s); }

  function showOverlay() {
    const ov = $('detail-overlay');
    ov.classList.add('open');
    ov.setAttribute('aria-hidden', 'false');
  }

  // Fetch a longer container range from SQLite (downsampled) and render it.
  async function loadHistory() {
    const c = findContainer(activeDetail);
    if (!c) return;
    try {
      const h = await xhrJson(`/api/history?name=${encodeURIComponent(activeDetail)}&range=${detailRange}`);
      if (detailKind === 'container' && activeDetail === h.name) {
        renderContainerInto(c, { cpu: h.cpu, mem: h.mem, net_rx: h.net_rx, net_tx: h.net_tx, interval: h.interval });
        lastHistLoad = Date.now();
      }
    } catch (e) { /* keep last render */ }
  }

  async function loadSystemHistory() {
    try {
      const h = await xhrJson(`/api/system-history?range=${detailRange}`);
      if (detailKind === 'system' && lastSys) {
        renderSystemInto(lastSys, h);          // h already has the series keys + interval
        lastHistLoad = Date.now();
      }
    } catch (e) { /* keep last render */ }
  }

  function setRange(r) {
    detailRange = r;
    if (detailKind === 'container') {
      if (r === 'live') { const c = findContainer(activeDetail); if (c) renderContainerInto(c, liveSeries(c)); }
      else loadHistory();
    } else if (detailKind === 'system') {
      if (r === 'live') { if (lastSys) renderSystemInto(lastSys, systemLiveSeries(lastSys)); }
      else loadSystemHistory();
    }
  }

  function openDetail(name) {
    const c = findContainer(name);
    if (!c) return;
    detailKind = 'container';
    activeDetail = name;
    detailRange = 'live';
    renderContainerInto(c, liveSeries(c));
    showOverlay();
  }

  function openSystemDetail() {
    if (!lastSys) return;
    detailKind = 'system';
    activeDetail = null;
    detailRange = 'live';
    renderSystemInto(lastSys, systemLiveSeries(lastSys));
    showOverlay();
  }

  function updateDetail() {
    if (detailKind === 'container') {
      const c = findContainer(activeDetail);
      if (!c) return;
      if (detailRange === 'live') renderContainerInto(c, liveSeries(c));
      else if (Date.now() - lastHistLoad > 15000) loadHistory();
    } else if (detailKind === 'system') {
      if (!lastSys) return;
      if (detailRange === 'live') renderSystemInto(lastSys, systemLiveSeries(lastSys));
      else if (Date.now() - lastHistLoad > 15000) loadSystemHistory();
    }
  }

  function closeDetail() {
    detailKind = null;
    activeDetail = null;
    const ov = $('detail-overlay');
    ov.classList.remove('open');
    ov.setAttribute('aria-hidden', 'true');
  }

  async function refresh() {
    let data, sys;
    try {
      [data, sys] = await Promise.all([
        xhrJson('/api/containers'),
        xhrJson('/api/system'),
      ]);
    } catch(e) {
      console.error('dboard fetch error', e);
      showError(e.message);
      return;
    }

    hideError();
    if (data.error) { showError(data.error); return; }

    renderSystem(sys);

    rawData.proxied = data.proxied || [];
    rawData.others  = data.others  || [];
    if (data.sample_interval) sampleInterval = data.sample_interval;

    redraw('proxied');
    redraw('others');
    updateDetail();   // keep the open overlay live

    $('lbl-proxied').textContent = `${data.running_proxied} running / ${rawData.proxied.length} total`;
    $('lbl-others').textContent  = `${data.running_others} running / ${rawData.others.length} total`;
    $('summary').textContent     = `${data.running_proxied} proxied running · ${data.running_others} other running`;
    const totalRunning = data.running_proxied + data.running_others;
    const totalAll = rawData.proxied.length + rawData.others.length;
    $('badge-containers').textContent = `${totalRunning} / ${totalAll}`;
    try { $('clock').textContent = new Date(data.updated_at).toLocaleTimeString(); }
    catch(_) { $('clock').textContent = data.updated_at; }
  }

  // ── Event wiring (CSP-safe: no inline handlers) ──────────────────────────────
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.addEventListener('click', () => switchTab(b.dataset.tab)));
  document.querySelectorAll('th.sortable[data-col]').forEach(th =>
    th.addEventListener('click', () => sortTable(th.dataset.table, th.dataset.col)));
  ['proxied','others'].forEach(t => {
    const inp = document.getElementById('filter-' + t);
    if (inp) inp.addEventListener('input', () => redraw(t));
  });
  const _refreshBtn = document.getElementById('tok-refresh-btn');
  if (_refreshBtn) _refreshBtn.addEventListener('click', () => refreshTokens(true));

  // Open the detail overlay on row click (ignore clicks on domain links).
  ['proxied', 'others'].forEach(t => {
    const body = document.getElementById(t + '-body');
    if (body) body.addEventListener('click', (e) => {
      if (e.target.closest('a')) return;
      const tr = e.target.closest('tr[data-cname]');
      if (tr) openDetail(tr.dataset.cname);
    });
  });
  // Open the system overlay on any system card click.
  const _sysGrid = document.getElementById('sys-grid');
  if (_sysGrid) _sysGrid.addEventListener('click', () => openSystemDetail());

  // Close on backdrop click or the × button.
  const _ov = document.getElementById('detail-overlay');
  if (_ov) _ov.addEventListener('click', (e) => {
    if (e.target === _ov || e.target.closest('.ov-close')) { closeDetail(); return; }
    const rb = e.target.closest('.ov-range-btn');
    if (rb) setRange(rb.dataset.range === 'live' ? 'live' : parseInt(rb.dataset.range, 10));
  });
  // Close on Escape.
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDetail(); });

  refresh();
  setInterval(refresh, 5000);

  refreshTokens();
  setInterval(refreshTokens, 30000);

  // Register the service worker (app shell — installable, instant/offline UI).
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js')
        .catch(err => console.warn('SW registration failed:', err));
    });
  }
