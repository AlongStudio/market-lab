/* K 线渲染公共模块:日/周/月K + 分钟K,含复权切换、命中日 markPoint、分钟按需加载。
 * 导出 window.KlineRender.mountKline(container, opts) -> controller
 * controller: { switchTab, setPeriod, setAdjust, setMinuteDay, getCode, dispose }
 * 同一时刻每个 mountKline 调用持有独立 ECharts 实例,dispose 时 chart.dispose() + 移除事件。
 */
(function () {
  const DEFAULT_RANGE_DAYS = 180;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function defaultApi(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers, {
      'Authorization': 'Bearer ' + (localStorage.getItem('token') || ''),
    });
    return fetch(path, opts).then(r => {
      if (r.status === 401) {
        localStorage.removeItem('token');
        location.replace('/login');
        throw new Error('未授权');
      }
      const refreshed = r.headers.get('X-Refresh-Token');
      if (refreshed) localStorage.setItem('token', refreshed);
      if (!r.ok) throw new Error(r.text());
      return r.json();
    });
  }

  function buildKlineOption(k, hitDay) {
    if (!k || !k.length) return null;
    const dates = k.map(d => d.trading_date);
    const values = k.map(d => [d.open, d.close, d.low, d.high]);
    const volumes = k.map(d => d.volume);
    const hit = hitDay ? k.find(d => d.trading_date === hitDay) : undefined;
    return {
      animation: false,
      color: ['#2f7cf6'],
      grid: [
        { left: '10%', right: '8%', top: '10%', height: '55%' },
        { left: '10%', right: '8%', top: '70%', height: '15%' },
      ],
      xAxis: [
        { type: 'category', data: dates, scale: true, boundaryGap: false,
          axisLine: { onZero: false }, splitLine: { show: false },
          min: 'dataMin', max: 'dataMax' },
        { type: 'category', gridIndex: 1, data: dates, scale: true, boundaryGap: false,
          axisLine: { onZero: false }, axisTick: { show: false },
          splitLine: { show: false }, axisLabel: { show: false },
          min: 'dataMin', max: 'dataMax' },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false },
          axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '5%', start: 0, end: 100 },
      ],
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      series: [
        {
          name: 'K线', type: 'candlestick', data: values,
          itemStyle: { color: '#ef5350', color0: '#26a69a',
                       borderColor: '#ef5350', borderColor0: '#26a69a' },
          markPoint: hit ? {
            symbolSize: 45,
            label: { formatter: '命中', fontSize: 10, color: '#fff' },
            itemStyle: { color: '#faad14' },
            data: [{ coord: [hitDay, hit.low], value: '命中' }],
          } : undefined,
        },
        {
          name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes,
          itemStyle: { color: (p) => k[p.dataIndex].close >= k[p.dataIndex].open ? '#ef5350' : '#26a69a' },
        },
      ],
    };
  }

  function buildMinuteOption(m) {
    if (!m || !m.length) return null;
    const times = m.map(d => {
      const dt = new Date(d.minute_time);
      return String(dt.getHours()).padStart(2, '0') + ':' + String(dt.getMinutes()).padStart(2, '0');
    });
    const values = m.map(d => [d.open, d.close, d.low, d.high]);
    const amounts = m.map(d => d.amount);
    return {
      animation: false,
      grid: [
        { left: '10%', right: '8%', top: '10%', height: '55%' },
        { left: '10%', right: '8%', top: '70%', height: '15%' },
      ],
      xAxis: [
        { type: 'category', data: times, scale: true, boundaryGap: false,
          axisLine: { onZero: false }, splitLine: { show: false } },
        { type: 'category', gridIndex: 1, data: times, scale: true, boundaryGap: false,
          axisLine: { onZero: false }, axisTick: { show: false },
          splitLine: { show: false }, axisLabel: { show: false } },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false },
          axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '5%', start: 0, end: 100 },
      ],
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      series: [
        {
          name: '分钟K', type: 'candlestick', data: values,
          itemStyle: { color: '#ef5350', color0: '#26a69a',
                       borderColor: '#ef5350', borderColor0: '#26a69a' },
        },
        {
          name: '成交额', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: amounts,
          itemStyle: { color: (p) => m[p.dataIndex].close >= m[p.dataIndex].open ? '#ef5350' : '#26a69a' },
        },
      ],
    };
  }

  // 注入工具栏 + 图表 DOM 到 container
  function injectDom(container, opts) {
    container.innerHTML = `
      <div class="kr-tabs">
        <div class="kr-tab active" data-tab="kline">日/周/月K</div>
        <div class="kr-tab" data-tab="minute">分钟K</div>
      </div>
      <div class="kr-toolbar kr-kline-toolbar">
        <div class="kr-btn-group kr-period-group">
          <button data-period="daily" class="active">日K</button>
          <button data-period="weekly">周K</button>
          <button data-period="monthly">月K</button>
        </div>
        <div class="kr-btn-group kr-adjust-group">
          <button data-adjust="">不复权</button>
          <button data-adjust="qfq" class="active">前复权</button>
          <button data-adjust="hfq">后复权</button>
        </div>
      </div>
      <div class="kr-toolbar kr-minute-toolbar" style="display:none">
        <label class="kr-muted">交易日:</label>
        <input class="kr-minute-day" type="date">
        <span class="kr-muted kr-minute-dates-count"></span>
      </div>
      <div class="kr-chart"></div>
      <div class="kr-empty" style="display:none">暂无数据</div>
    `;
    return {
      tabs: container.querySelectorAll('.kr-tab'),
      klineToolbar: container.querySelector('.kr-kline-toolbar'),
      minuteToolbar: container.querySelector('.kr-minute-toolbar'),
      periodBtns: container.querySelectorAll('.kr-period-group button'),
      adjustBtns: container.querySelectorAll('.kr-adjust-group button'),
      minuteDay: container.querySelector('.kr-minute-day'),
      minuteDatesCount: container.querySelector('.kr-minute-dates-count'),
      chart: container.querySelector('.kr-chart'),
      empty: container.querySelector('.kr-empty'),
    };
  }

  function mountKline(container, opts) {
    opts = opts || {};
    const code = opts.code;
    const hitDay = opts.hitDay || null;
    const apiFn = opts.api || defaultApi;
    const rangeDays = opts.defaultRangeDays || DEFAULT_RANGE_DAYS;
    const initialTab = opts.initialTab || 'kline';   // 'kline' | 'minute'
    const initialPeriod = opts.initialPeriod || 'daily';

    const els = injectDom(container, opts);
    let chart = null;
    const state = {
      period: initialPeriod,
      adjust: 'qfq',
      klineData: [],
      minuteDates: [],
      minuteDay: '',
      minuteData: [],
      activeTab: initialTab,
      minuteLoaded: false,
    };

    function renderChart() {
      const option = state.activeTab === 'kline'
        ? buildKlineOption(state.klineData, hitDay)
        : buildMinuteOption(state.minuteData);
      if (!option) {
        els.chart.style.display = 'none';
        els.empty.style.display = 'block';
        return;
      }
      els.empty.style.display = 'none';
      els.chart.style.display = 'block';
      if (!chart) chart = echarts.init(els.chart);
      chart.setOption(option, true);
    }

    function loadKline() {
      const startDay = (() => {
        const def = new Date(Date.now() - rangeDays * 86400000);
        const defStr = def.toISOString().slice(0, 10);
        if (hitDay && hitDay < defStr) {
          const d = new Date(hitDay);
          d.setDate(d.getDate() - 30);
          return d.toISOString().slice(0, 10);
        }
        return defStr;
      })();
      const q = new URLSearchParams({ code, adjust: state.adjust, start: startDay });
      return apiFn(`/api/kline/${state.period}?${q}`).then(res => {
        state.klineData = res.data || [];
        renderChart();
      });
    }

    function loadMinuteDates() {
      return apiFn(`/api/kline/minute/dates?code=${encodeURIComponent(code)}`).then(res => {
        state.minuteDates = res.dates || [];
        els.minuteDatesCount.textContent = `共 ${state.minuteDates.length} 个交易日`;
        if (state.minuteDates.length) {
          els.minuteDay.min = state.minuteDates[state.minuteDates.length - 1];
          els.minuteDay.max = state.minuteDates[0];
          state.minuteDay = state.minuteDates[0];
          els.minuteDay.value = state.minuteDay;
        }
      });
    }

    function loadMinute() {
      if (!state.minuteDay) return Promise.resolve();
      return apiFn(`/api/kline/minute/day?code=${encodeURIComponent(code)}&day=${state.minuteDay}`).then(res => {
        state.minuteData = res.data || [];
        renderChart();
      });
    }

    function switchTab(name) {
      if (state.activeTab === name) return;
      state.activeTab = name;
      els.tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === name));
      els.klineToolbar.style.display = name === 'kline' ? 'flex' : 'none';
      els.minuteToolbar.style.display = name === 'minute' ? 'flex' : 'none';
      if (name === 'minute') {
        if (!state.minuteLoaded) {
          state.minuteLoaded = true;
          return loadMinuteDates().then(loadMinute);
        }
        return loadMinute();
      }
      return loadKline();
    }

    function setPeriod(p) {
      state.period = p;
      els.periodBtns.forEach(b => b.classList.toggle('active', b.dataset.period === p));
      return loadKline();
    }

    function setAdjust(a) {
      state.adjust = a;
      els.adjustBtns.forEach(b => b.classList.toggle('active', b.dataset.adjust === a));
      return loadKline();
    }

    function onMinuteDayChange() {
      const v = els.minuteDay.value;
      if (v && state.minuteDates.length && !state.minuteDates.includes(v)) {
        alert('该日无分钟数据');
        els.minuteDay.value = state.minuteDay;
        return;
      }
      state.minuteDay = v;
      loadMinute();
    }

    function loadInitial() {
      if (initialTab === 'minute') {
        state.activeTab = 'minute';
        els.tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === 'minute'));
        els.klineToolbar.style.display = 'none';
        els.minuteToolbar.style.display = 'flex';
        state.minuteLoaded = true;
        return loadMinuteDates().then(loadMinute);
      }
      return loadKline();
    }

    function dispose() {
      if (chart) { chart.dispose(); chart = null; }
      container.innerHTML = '';
    }

    // 绑定事件
    els.tabs.forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
    els.periodBtns.forEach(b => b.addEventListener('click', () => setPeriod(b.dataset.period)));
    els.adjustBtns.forEach(b => b.addEventListener('click', () => setAdjust(b.dataset.adjust)));
    els.minuteDay.addEventListener('change', onMinuteDayChange);
    const onResize = () => { if (chart) chart.resize(); };
    window.addEventListener('resize', onResize);

    return {
      switchTab,
      setPeriod,
      setAdjust,
      getCode: () => code,
      loadInitial,
      dispose: () => {
        window.removeEventListener('resize', onResize);
        dispose();
      },
    };
  }

  window.KlineRender = { mountKline, esc };
})();
