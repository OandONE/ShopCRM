(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { fields: [], metrics: [], lastConfig: null, lastResult: null, chart: null, saved: [] };

  const OP_LABELS = {
    eq: 'برابر است با', ne: 'برابر نیست با', contains: 'شامل عبارت', starts: 'شروع می‌شود با',
    gt: 'بزرگ‌تر از', gte: 'بزرگ‌تر یا مساوی', lt: 'کوچک‌تر از', lte: 'کوچک‌تر یا مساوی',
    in: 'یکی از مقادیر', between: 'بین دو مقدار', is_null: 'خالی است', not_null: 'خالی نیست'
  };

  const dateIso = (date) => {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
  };

  function setDefaultDates() {
    const today = new Date();
    const from = new Date(today);
    from.setDate(from.getDate() - 29);
    $('#date-from').value = dateIso(from);
    $('#date-to').value = dateIso(today);
    const compareTo = new Date(from);
    compareTo.setDate(compareTo.getDate() - 1);
    const compareFrom = new Date(compareTo);
    compareFrom.setDate(compareFrom.getDate() - 29);
    $('#compare-from').value = dateIso(compareFrom);
    $('#compare-to').value = dateIso(compareTo);
  }

  async function requestJson(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.method && options.method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRF-Token'] = window.CSRF_TOKEN || '';
    }
    const response = await fetch(url, { ...options, headers });
    let payload;
    try { payload = await response.json(); }
    catch { payload = {}; }
    if (!response.ok) {
      const message = payload.detail || payload.message || `خطای درخواست (${response.status})`;
      throw new Error(message);
    }
    return payload;
  }

  function setStatus(message, isError = false) {
    const el = $('#analysis-status');
    el.textContent = message || '';
    el.classList.toggle('text-danger', Boolean(isError));
    el.classList.toggle('text-body-secondary', !isError);
  }

  function fillMetricOptions() {
    const select = $('#metric');
    const previous = select.value;
    select.replaceChildren();
    state.metrics.forEach(metric => {
      const option = document.createElement('option');
      option.value = metric.id;
      option.textContent = metric.label;
      select.append(option);
    });
    if (state.metrics.some(x => x.id === previous)) select.value = previous;
    else if (state.metrics.some(x => x.id === 'sales_count')) select.value = 'sales_count';
  }

  function updateGroupOptions() {
    const select = $('#group-by');
    const isItems = $('#dataset').value === 'items';
    $$('option[data-items-only="1"], option[data-scope="item"]', select).forEach(option => {
      option.hidden = !isItems;
      option.disabled = !isItems;
    });
    if (!isItems && (select.value === 'product_name' || select.selectedOptions[0]?.dataset.scope === 'item')) select.value = 'month';
  }

  function fillCustomGroupOptions() {
    const select = $('#group-by');
    $$('optgroup[data-custom-group="1"]', select).forEach(group => group.remove());
    const saleGroup = document.createElement('optgroup');
    saleGroup.label = 'گروه‌بندی بر اساس فیلد سفارشی فاکتور';
    saleGroup.dataset.customGroup = '1';
    const itemGroup = document.createElement('optgroup');
    itemGroup.label = 'گروه‌بندی بر اساس فیلد سفارشی کالا';
    itemGroup.dataset.customGroup = '1';
    state.fields.filter(field => field.id.startsWith('sale_col:')).forEach(field => {
      const option = new Option(field.label, `field:${field.id.split(':')[1]}`);
      option.dataset.scope = 'sale';
      saleGroup.append(option);
    });
    state.fields.filter(field => field.id.startsWith('item_col:')).forEach(field => {
      const option = new Option(field.label, `field:${field.id.split(':')[1]}`);
      option.dataset.scope = 'item';
      itemGroup.append(option);
    });
    if (saleGroup.children.length) select.append(saleGroup);
    if (itemGroup.children.length) select.append(itemGroup);
  }

  function fieldOptions(selected = '') {
    const fragment = document.createDocumentFragment();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'انتخاب فیلد...';
    fragment.append(placeholder);

    const groupLabels = { sale: 'اطلاعات فاکتور', item: 'اطلاعات اقلام' };
    ['sale', 'item'].forEach(scope => {
      const group = document.createElement('optgroup');
      group.label = groupLabels[scope];
      state.fields.filter(field => field.scope === scope).forEach(field => {
        const option = document.createElement('option');
        option.value = field.id;
        option.textContent = field.label;
        option.dataset.type = field.type;
        if (field.id === selected) option.selected = true;
        group.append(option);
      });
      if (group.children.length) fragment.append(group);
    });
    return fragment;
  }

  function createGroup(container, { logic = 'AND', nested = false, negate = false } = {}) {
    const group = document.createElement('div');
    group.className = `analytics-filter-group${nested ? ' is-nested' : ''}`;
    group.dataset.node = 'group';
    group.dataset.logic = logic;

    const header = document.createElement('div');
    header.className = 'analytics-filter-group-head';

    const logicSelect = document.createElement('select');
    logicSelect.className = 'form-select form-select-sm';
    logicSelect.style.width = '125px';
    logicSelect.setAttribute('aria-label', 'منطق گروه');
    [['AND', 'همهٔ شرط‌ها (AND)'], ['OR', 'حداقل یک شرط (OR)']].forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      logicSelect.append(option);
    });
    logicSelect.value = logic;
    logicSelect.addEventListener('change', () => { group.dataset.logic = logicSelect.value; });
    header.append(logicSelect);

    const negateLabel = document.createElement('label');
    negateLabel.className = 'form-check small d-flex align-items-center gap-2 mb-0';
    const negateInput = document.createElement('input');
    negateInput.type = 'checkbox';
    negateInput.className = 'form-check-input mt-0';
    negateInput.checked = negate;
    negateInput.setAttribute('aria-label', 'معکوس‌کردن نتیجهٔ گروه');
    negateInput.addEventListener('change', () => { group.dataset.negate = negateInput.checked ? '1' : '0'; });
    group.dataset.negate = negate ? '1' : '0';
    negateLabel.append(negateInput, document.createTextNode('معکوس (NOT)'));
    header.append(negateLabel);

    if (nested) {
      const removeGroup = document.createElement('button');
      removeGroup.type = 'button';
      removeGroup.className = 'btn btn-sm btn-outline-danger me-auto';
      removeGroup.innerHTML = '<i class="bi bi-x-lg"></i> حذف گروه';
      removeGroup.addEventListener('click', () => group.remove());
      header.append(removeGroup);
    }

    const children = document.createElement('div');
    children.className = 'analytics-filter-children';
    group.append(header, children);

    const actions = document.createElement('div');
    actions.className = 'analytics-filter-actions';
    const addRule = document.createElement('button');
    addRule.type = 'button';
    addRule.className = 'btn btn-sm btn-outline-primary';
    addRule.innerHTML = '<i class="bi bi-plus"></i> افزودن شرط';
    addRule.addEventListener('click', () => createRule(children));
    const addNested = document.createElement('button');
    addNested.type = 'button';
    addNested.className = 'btn btn-sm btn-outline-secondary';
    addNested.innerHTML = '<i class="bi bi-diagram-2"></i> افزودن گروه';
    addNested.addEventListener('click', () => createGroup(children, { nested: true }));
    actions.append(addRule, addNested);
    group.append(actions);
    container.append(group);
    return group;
  }

  function createRule(container, initial = {}) {
    const rule = document.createElement('div');
    rule.className = 'analytics-filter-rule';
    rule.dataset.node = 'rule';

    const field = document.createElement('select');
    field.className = 'form-select form-select-sm field-select';
    field.setAttribute('aria-label', 'فیلد شرط');
    field.append(fieldOptions(initial.field || ''));

    const op = document.createElement('select');
    op.className = 'form-select form-select-sm op-select';
    op.setAttribute('aria-label', 'عملگر شرط');

    const valueWrap = document.createElement('div');
    valueWrap.className = 'value-wrap';

    const makeValueInput = (type = 'text', value = '') => {
      const input = document.createElement('input');
      input.className = 'form-control form-control-sm filter-value';
      input.type = type;
      input.value = value;
      input.placeholder = type === 'date' ? 'تاریخ' : 'مقدار';
      return input;
    };

    function updateOperatorOptions() {
      const selected = field.options[field.selectedIndex];
      const type = selected?.dataset.type || 'text';
      const oldValue = op.value;
      op.replaceChildren();
      Object.entries(OP_LABELS).forEach(([value, label]) => {
        if (['contains', 'starts'].includes(value) && type !== 'text') return;
        if (['gt', 'gte', 'lt', 'lte', 'between'].includes(value) && type === 'text') return;
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        op.append(option);
      });
      if ([...op.options].some(option => option.value === oldValue)) op.value = oldValue;
      else op.value = 'eq';
      updateValueInputs();
    }

    function updateValueInputs() {
      const operator = op.value;
      const selected = field.options[field.selectedIndex];
      const type = selected?.dataset.type || 'text';
      valueWrap.replaceChildren();
      if (['is_null', 'not_null'].includes(operator)) return;
      if (operator === 'between') {
        valueWrap.append(makeValueInput(type === 'number' ? 'number' : type === 'date' ? 'date' : 'text', initial.value?.[0] || ''));
        valueWrap.append(makeValueInput(type === 'number' ? 'number' : type === 'date' ? 'date' : 'text', initial.value?.[1] || ''));
      } else {
        const inputType = operator === 'in' ? 'text' : type === 'number' ? 'number' : type === 'date' ? 'date' : 'text';
        valueWrap.append(makeValueInput(inputType, typeof initial.value === 'string' ? initial.value : Array.isArray(initial.value) ? initial.value.join(', ') : ''));
        if (operator === 'in') valueWrap.firstElementChild.placeholder = 'مقادیر را با ویرگول جدا کنید';
      }
    }

    field.addEventListener('change', updateOperatorOptions);
    op.addEventListener('change', updateValueInputs);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn btn-sm btn-outline-danger';
    remove.innerHTML = '<i class="bi bi-x"></i>';
    remove.title = 'حذف شرط';
    remove.setAttribute('aria-label', 'حذف شرط');
    remove.addEventListener('click', () => rule.remove());

    rule.append(field, op, valueWrap, remove);
    container.append(rule);
    updateOperatorOptions();
    if (initial.op && [...op.options].some(option => option.value === initial.op)) {
      op.value = initial.op;
      updateValueInputs();
    }
    return rule;
  }

  function readFilterGroup(group) {
    const childrenContainer = $('.analytics-filter-children', group);
    const children = [...childrenContainer.children].map(child => {
      if (child.dataset.node === 'group') return readFilterGroup(child);
      const field = $('.field-select', child).value;
      const op = $('.op-select', child).value;
      if (!field) throw new Error('برای یکی از شرط‌ها فیلد انتخاب نشده است.');
      const inputs = $$('.filter-value', child).map(input => input.value.trim());
      let value;
      if (op === 'between') {
        if (inputs.length !== 2 || inputs.some(x => x === '')) throw new Error('برای شرط «بین» باید هر دو مقدار را وارد کنید.');
        value = inputs;
      } else if (op === 'in') {
        value = (inputs[0] || '').split(',').map(x => x.trim()).filter(Boolean);
      } else if (!['is_null', 'not_null'].includes(op)) {
        value = inputs[0] || '';
        if (!value) throw new Error('مقدار یکی از شرط‌ها خالی است.');
      }
      return { field, op, value };
    }).filter(Boolean);
    return { logic: group.dataset.logic || 'AND', negate: group.dataset.negate === '1', children };
  }

  function buildConfig() {
    const dateFrom = $('#date-from').value;
    const dateTo = $('#date-to').value;
    if (!dateFrom || !dateTo) throw new Error('بازهٔ زمانی را کامل انتخاب کنید.');
    if (dateTo < dateFrom) throw new Error('تاریخ پایان نمی‌تواند قبل از شروع باشد.');
    const compareEnabled = $('#compare-enabled').checked;
    if (compareEnabled && (!$('#compare-from').value || !$('#compare-to').value || $('#compare-to').value < $('#compare-from').value)) {
      throw new Error('بازهٔ مقایسه را به‌درستی وارد کنید.');
    }
    return {
      dataset: $('#dataset').value,
      metric: $('#metric').value,
      group_by: $('#group-by').value,
      chart_type: $('#chart-type').value,
      date_from: dateFrom,
      date_to: dateTo,
      compare_enabled: compareEnabled,
      compare_from: compareEnabled ? $('#compare-from').value : null,
      compare_to: compareEnabled ? $('#compare-to').value : null,
      filters: readFilterGroup($('#filter-root').firstElementChild)
    };
  }

  function formatNumber(value) {
    const number = Number(value || 0);
    return new Intl.NumberFormat('fa-IR', { maximumFractionDigits: 2 }).format(number);
  }

  function updateWarnings(warnings) {
    const container = $('#analysis-warnings');
    container.replaceChildren();
    (warnings || []).forEach(message => {
      const alert = document.createElement('div');
      alert.className = 'alert alert-warning py-2 mb-2';
      alert.textContent = message;
      container.append(alert);
    });
  }

  function renderTable(currentRows, compareRows, isComparison) {
    const body = $('#analysis-table-body');
    body.replaceChildren();
    $('#compare-table-heading').hidden = !isComparison;
    const currentMap = new Map(currentRows.map(row => [row.label, row.value]));
    const compareMap = new Map((compareRows || []).map(row => [row.label, row.value]));
    const labels = [...new Set([...currentRows.map(row => row.label), ...(compareRows || []).map(row => row.label)])];
    if (!labels.length) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = isComparison ? 3 : 2;
      cell.className = 'text-center text-body-secondary py-4';
      cell.textContent = 'برای این بازه داده‌ای پیدا نشد.';
      row.append(cell); body.append(row); return;
    }
    labels.forEach(label => {
      const row = document.createElement('tr');
      const cells = [label, currentMap.has(label) ? formatNumber(currentMap.get(label)) : '—'];
      if (isComparison) cells.push(compareMap.has(label) ? formatNumber(compareMap.get(label)) : '—');
      cells.forEach(text => {
        const cell = document.createElement('td');
        cell.textContent = text;
        row.append(cell);
      });
      body.append(row);
    });
  }

  function renderChart(currentRows, compareRows, chartType) {
    const canvas = $('#analysis-chart');
    if (state.chart) state.chart.destroy();
    const currentMap = new Map(currentRows.map(row => [row.label, row.value]));
    const compareMap = new Map((compareRows || []).map(row => [row.label, row.value]));
    const labels = [...new Set([...currentRows.map(row => row.label), ...(compareRows || []).map(row => row.label)])];
    const currentData = labels.map(label => currentMap.has(label) ? currentMap.get(label) : null);
    const compareData = labels.map(label => compareMap.has(label) ? compareMap.get(label) : null);
    const datasets = [{ label: 'بازهٔ فعلی', data: currentData, borderWidth: 2, tension: 0.25 }];
    if (compareRows) datasets.push({ label: 'بازهٔ مقایسه', data: compareData, borderWidth: 2, tension: 0.25 });

    let data;
    if (chartType === 'scatter') {
      data = { datasets: datasets.map(dataset => ({
        ...dataset,
        data: dataset.data.map((value, index) => value === null ? null : ({ x: index + 1, y: Number(value) })).filter(Boolean),
      })) };
    } else if (chartType === 'bubble') {
      data = { datasets: datasets.map(dataset => ({
        ...dataset,
        data: dataset.data.map((value, index) => value === null ? null : ({ x: index + 1, y: Number(value), r: Math.max(4, Math.min(22, Math.sqrt(Math.abs(Number(value)) || 1))) })).filter(Boolean),
      })) };
    } else {
      data = { labels, datasets };
    }

    const options = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: Boolean(compareRows) || ['pie', 'doughnut', 'polarArea'].includes(chartType), position: 'bottom' } },
      scales: ['pie', 'doughnut', 'polarArea', 'radar'].includes(chartType) ? {} : { y: { beginAtZero: true } }
    };
    if (chartType === 'scatter' || chartType === 'bubble') {
      options.scales = { x: { type: 'linear', position: 'bottom', title: { display: true, text: 'شمارهٔ گروه' } }, y: { beginAtZero: true } };
    }
    state.chart = new Chart(canvas.getContext('2d'), { type: chartType, data, options });
  }

  function renderResult(result) {
    state.lastResult = result;
    $('#analysis-results').hidden = false;
    $('#current-period-label').textContent = `بازهٔ فعلی: ${result.current.date_from} تا ${result.current.date_to}`;
    $('#current-total').textContent = formatNumber(result.current.total);
    $('#metric-label').textContent = result.metric.label;
    $('#group-count').textContent = formatNumber(result.current.group_count);
    const comparison = result.comparison;
    $('#compare-kpi-card').hidden = !comparison;
    if (comparison) {
      $('#compare-period-label').textContent = `بازهٔ مقایسه: ${comparison.date_from} تا ${comparison.date_to}`;
      $('#compare-total').textContent = formatNumber(comparison.total);
      const percent = comparison.percentage_change;
      const change = $('#compare-change');
      if (percent === null) {
        change.textContent = `تغییر مطلق: ${formatNumber(comparison.difference)}`;
      } else {
        const sign = percent > 0 ? '+' : '';
        change.textContent = `تغییر: ${sign}${formatNumber(percent)}٪ · مقدار: ${sign}${formatNumber(comparison.difference)}`;
      }
      change.className = `small ${comparison.difference > 0 ? 'text-success' : comparison.difference < 0 ? 'text-danger' : 'text-body-secondary'}`;
    }
    updateWarnings(result.warnings);
    renderTable(result.current.rows, comparison?.rows || null, Boolean(comparison));
    renderChart(result.current.rows, comparison?.rows || null, result.config.chart_type);
    $('#analysis-meta').textContent = `ساخته‌شده در ${result.generated_at} · ${result.schema} · تعداد گروه‌های محاسبه‌شده: ${formatNumber(result.current.group_count)}`;
    $('#export-json-btn').disabled = false;
  }

  async function runAnalysis() {
    const button = $('#run-analysis-btn');
    button.disabled = true;
    $('#export-json-btn').disabled = true;
    setStatus('در حال محاسبه...');
    try {
      const config = buildConfig();
      const result = await requestJson('/api/analytics/run', { method: 'POST', body: JSON.stringify(config) });
      state.lastConfig = config;
      renderResult(result);
      setStatus('تحلیل با موفقیت اجرا شد.');
    } catch (error) {
      setStatus(error.message || 'اجرای تحلیل ناموفق بود.', true);
    } finally {
      button.disabled = false;
    }
  }

  async function exportJson() {
    if (!state.lastConfig) return;
    const button = $('#export-json-btn');
    button.disabled = true;
    try {
      const response = await fetch('/api/analytics/export.json', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.CSRF_TOKEN || '' },
        body: JSON.stringify(state.lastConfig)
      });
      if (!response.ok) {
        let error = {};
        try { error = await response.json(); } catch { /* ignore */ }
        throw new Error(error.detail || `خطای دریافت JSON (${response.status})`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'shopcrm-analytics.json';
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setStatus('فایل JSON آماده شد.');
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function loadSavedAnalyses() {
    const select = $('#saved-analysis-select');
    const current = select.value;
    try {
      state.saved = await requestJson('/api/analytics/saved');
      select.replaceChildren(new Option('تحلیل ذخیره‌شده...', ''));
      state.saved.forEach(item => select.add(new Option(item.name, item.id)));
      if (state.saved.some(item => String(item.id) === current)) select.value = current;
      $('#delete-saved-btn').disabled = !select.value;
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function saveAnalysis() {
    try {
      const name = window.prompt('برای این تحلیل یک نام وارد کنید:');
      if (name === null) return;
      const config = buildConfig();
      const selectedId = $('#saved-analysis-select').value;
      const saved = await requestJson('/api/analytics/saved', {
        method: 'POST', body: JSON.stringify({ id: selectedId || null, name, config })
      });
      await loadSavedAnalyses();
      $('#saved-analysis-select').value = String(saved.id);
      $('#delete-saved-btn').disabled = false;
      setStatus('تحلیل ذخیره شد.');
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function loadSavedAnalysis(id) {
    if (!id) return;
    try {
      const saved = await requestJson(`/api/analytics/saved/${encodeURIComponent(id)}`);
      applyConfig(saved.config);
      setStatus(`«${saved.name}» بارگذاری شد. برای دیدن نتیجه، تحلیل را اجرا کنید.`);
      $('#delete-saved-btn').disabled = false;
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function deleteSavedAnalysis() {
    const id = $('#saved-analysis-select').value;
    if (!id || !window.confirm('این تحلیل ذخیره‌شده حذف شود؟')) return;
    try {
      await requestJson(`/api/analytics/saved/${encodeURIComponent(id)}`, { method: 'DELETE' });
      await loadSavedAnalyses();
      setStatus('تحلیل ذخیره‌شده حذف شد.');
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  function applyConfig(config) {
    $('#dataset').value = config.dataset || 'sales';
    updateGroupOptions();
    fillMetricOptions();
    $('#metric').value = config.metric || 'sales_count';
    $('#group-by').value = config.group_by || 'month';
    $('#chart-type').value = config.chart_type || 'bar';
    $('#date-from').value = config.date_from || '';
    $('#date-to').value = config.date_to || '';
    $('#compare-enabled').checked = Boolean(config.compare_enabled);
    $('#compare-fields').hidden = !config.compare_enabled;
    $('#compare-from').value = config.compare_from || '';
    $('#compare-to').value = config.compare_to || '';
    $('#filter-root').replaceChildren();
    const root = createGroup($('#filter-root'), { logic: config.filters?.logic || 'AND', negate: Boolean(config.filters?.negate) });
    addFilterChildren(root, config.filters?.children || []);
  }

  function addFilterChildren(group, children) {
    const container = $('.analytics-filter-children', group);
    children.forEach(node => {
      if (node.children) {
        const childGroup = createGroup(container, { logic: node.logic || 'AND', nested: true, negate: Boolean(node.negate) });
        addFilterChildren(childGroup, node.children);
      } else {
        createRule(container, node);
      }
    });
  }

  function applyPreset(name) {
    const config = {
      dataset: 'sales', metric: 'sales_count', group_by: 'month', chart_type: 'line',
      date_from: $('#date-from').value, date_to: $('#date-to').value,
      compare_enabled: false, compare_from: null, compare_to: null,
      filters: { logic: 'AND', children: [] }
    };
    switch (name) {
      case 'sales_month': config.metric = 'net_revenue'; config.group_by = 'month'; config.chart_type = 'line'; break;
      case 'sales_day': config.metric = 'sales_count'; config.group_by = 'day'; config.chart_type = 'bar'; break;
      case 'top_products': config.dataset = 'items'; config.metric = 'quantity'; config.group_by = 'product_name'; config.chart_type = 'bar'; break;
      case 'gender': config.metric = 'sales_count'; config.group_by = 'gender'; config.chart_type = 'doughnut'; break;
      case 'age': config.metric = 'sales_count'; config.group_by = 'age'; config.chart_type = 'bar'; break;
      case 'refunds': config.metric = 'refund_total'; config.group_by = 'month'; config.chart_type = 'bar'; break;
      default: return;
    }
    applyConfig(config);
    setStatus('تحلیل آماده بارگذاری شد؛ اجرای تحلیل را بزنید.');
  }

  async function initialize() {
    setDefaultDates();
    createGroup($('#filter-root'));
    $('#compare-enabled').addEventListener('change', event => { $('#compare-fields').hidden = !event.target.checked; });
    $('#dataset').addEventListener('change', updateGroupOptions);
    $('#run-analysis-btn').addEventListener('click', runAnalysis);
    $('#export-json-btn').addEventListener('click', exportJson);
    $('#save-analysis-btn').addEventListener('click', saveAnalysis);
    $('#saved-analysis-select').addEventListener('change', event => {
      $('#delete-saved-btn').disabled = !event.target.value;
      if (event.target.value) loadSavedAnalysis(event.target.value);
    });
    $('#delete-saved-btn').addEventListener('click', deleteSavedAnalysis);
    $('#clear-filters').addEventListener('click', () => {
      $('#filter-root').replaceChildren();
      createGroup($('#filter-root'));
    });
    $$('#preset-menu [data-preset]').forEach(button => button.addEventListener('click', () => applyPreset(button.dataset.preset)));

    try {
      const catalog = await requestJson('/api/analytics/fields');
      state.fields = catalog.fields || [];
      state.metrics = catalog.metrics || [];
      fillMetricOptions();
      fillCustomGroupOptions();
      updateGroupOptions();
      await loadSavedAnalyses();
    } catch (error) {
      setStatus(error.message || 'بارگذاری فیلدهای تحلیل ناموفق بود.', true);
    }
  }

  document.addEventListener('DOMContentLoaded', initialize);
})();
