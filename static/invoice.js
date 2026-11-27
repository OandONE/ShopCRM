/* ShopCRM - invoice cart logic (shared between quick and full) */
(function () {
  'use strict';

  const form = document.getElementById('invoice-form');
  if (!form) return;

  const KIND = form.dataset.kind || 'quick';
  const CART_KEY = 'cart_' + KIND;
  const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';

  // number helpers
  const formatNumber = (n) => {
    const s = String(n).replace(/[^\d-]/g, '');
    return s.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  };
  const parseNumber = (s) => {
    const n = parseInt(String(s).replace(/[^\d-]/g, ''), 10);
    return isNaN(n) ? 0 : n;
  };
  const formatToman = (n) => formatNumber(n) + ' تومان';
  const escapeHtml = (s) =>
    String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
             .replace(/>/g,'&gt;').replace(/"/g,'&quot;');

  const formatJalali = (iso) => {
    if (!iso) return '—';
    try {
      return new Intl.DateTimeFormat('fa-IR', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        timeZone: 'Asia/Tehran',
      }).format(new Date(iso));
    } catch { return iso; }
  };

  // cart storage
  let cart = [];
  try { cart = JSON.parse(sessionStorage.getItem(CART_KEY) || '[]'); }
  catch { cart = []; }

  function saveCart() {
    try { sessionStorage.setItem(CART_KEY, JSON.stringify(cart)); } catch {}
  }

  // datetime
  const dtDisplay = document.getElementById('datetime-jalali');
  const dtDisplayRow = document.getElementById('datetime-display');
  const dtEditor = document.getElementById('datetime-editor');
  const dtInput = document.getElementById('datetime-input');
  const dtPreview = document.getElementById('datetime-jalali-preview');
  const dtEditBtn = document.getElementById('datetime-edit');
  const dtApplyBtn = document.getElementById('datetime-apply');
  const dtCancelBtn = document.getElementById('datetime-cancel');
  const dtBadge = document.getElementById('datetime-edited-badge');

  let datetimeEdited = false;
  let customDatetime = null;
  let clockInterval = null;

  function tickClock() {
    if (datetimeEdited) return;
    dtDisplay.textContent = formatJalali(new Date().toISOString());
  }
  function startClock() {
    if (clockInterval) clearInterval(clockInterval);
    tickClock();
    clockInterval = setInterval(tickClock, 1000);
  }
  function stopClock() {
    if (clockInterval) { clearInterval(clockInterval); clockInterval = null; }
  }
  function updateDtPreview() {
    if (!dtInput.value) { dtPreview.textContent = ''; return; }
    try {
      const iso = new Date(dtInput.value).toISOString();
      dtPreview.textContent = 'معادل شمسی: ' + formatJalali(iso);
    } catch { dtPreview.textContent = ''; }
  }

  if (dtEditBtn) {
    dtEditBtn.addEventListener('click', () => {
      stopClock();
      dtDisplayRow.classList.add('d-none');
      dtEditor.classList.remove('d-none');
      const d = customDatetime ? new Date(customDatetime) : new Date();
      const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
      dtInput.value = local.toISOString().slice(0, 16);
      updateDtPreview();
      dtInput.focus();
    });
  }
  if (dtInput) dtInput.addEventListener('input', updateDtPreview);
  if (dtApplyBtn) {
    dtApplyBtn.addEventListener('click', () => {
      if (!dtInput.value) return;
      customDatetime = new Date(dtInput.value).toISOString();
      datetimeEdited = true;
      dtDisplay.textContent = formatJalali(customDatetime);
      dtEditor.classList.add('d-none');
      dtDisplayRow.classList.remove('d-none');
      if (dtBadge) dtBadge.classList.remove('d-none');
    });
  }
  if (dtCancelBtn) {
    dtCancelBtn.addEventListener('click', () => {
      dtEditor.classList.add('d-none');
      dtDisplayRow.classList.remove('d-none');
      if (!datetimeEdited) startClock();
    });
  }
  startClock();

  // product autocomplete
  const prodSearch = document.getElementById('add-product-search');
  const prodDropdown = document.getElementById('add-product-dropdown');
  const prodIdField = document.getElementById('add-product-id');
  const prodNameField = document.getElementById('add-product-name');
  const dbPriceField = document.getElementById('add-db-price');
  const manualPriceField = document.getElementById('add-manual-price');
  const discountField = document.getElementById('add-discount');
  const discountModeField = document.getElementById('add-discount-mode');
  const discountLabel = document.getElementById('add-discount-label');
  const qtyField = document.getElementById('add-qty');
  const stockWarning = document.getElementById('add-stock-warning');

  let selectedProductStock = null;
  let searchTimer = null;
  let lastQuery = '';
  let activeProductIndex = -1;

  if (prodSearch) {
    prodSearch.setAttribute('role', 'combobox');
    prodSearch.setAttribute('aria-autocomplete', 'list');
    prodSearch.setAttribute('aria-controls', 'add-product-dropdown');
    prodSearch.setAttribute('aria-expanded', 'false');
  }

  function hideProdDropdown() {
    activeProductIndex = -1;
    if (prodDropdown) {
      prodDropdown.style.display = 'none';
      prodDropdown.innerHTML = '';
      prodSearch?.setAttribute('aria-expanded', 'false');
      prodSearch?.removeAttribute('aria-activedescendant');
    }
  }

  function updateActiveProduct() {
    const options = [...(prodDropdown?.querySelectorAll('.product-item') || [])];
    options.forEach((option, index) => {
      const active = index === activeProductIndex;
      option.setAttribute('aria-selected', active ? 'true' : 'false');
      if (active) {
        prodSearch.setAttribute('aria-activedescendant', option.id);
        option.scrollIntoView({block: 'nearest'});
      }
    });
    if (activeProductIndex < 0) prodSearch?.removeAttribute('aria-activedescendant');
  }

  function selectProductItem(item) {
    if (!item) return;
    const name = item.dataset.name;
    const pid = item.dataset.id;
    const price = item.dataset.price;
    lastQuery = '';
    selectedProductStock = Number(item.dataset.stock ?? 0);
    prodSearch.value = name;
    prodNameField.value = name;
    prodIdField.value = pid;
    dbPriceField.value = price ? formatNumber(price) : '';
    dbPriceField.classList.add('field-auto');
    manualPriceField.value = price ? formatNumber(price) : '';
    manualPriceField.classList.add('field-auto');
    hideProdDropdown();
    updateStockWarning();
    manualPriceField.focus();
  }

  function renderProdDropdown(items) {
    if (!items.length) { hideProdDropdown(); return; }
    activeProductIndex = -1;
    prodDropdown.setAttribute('role', 'listbox');
    prodDropdown.innerHTML = items.map((p, index) => {
      const safe = escapeHtml(p.name);
      const priceTxt = p.price != null ? formatNumber(p.price) + ' ت' : 'بدون قیمت';
      const stock = Number(p.stock_quantity ?? 0);
      const stockTxt = `موجودی: ${formatNumber(stock)}`;
      return `<div id="add-product-option-${index}" class="product-item" role="option" aria-selected="false" data-id="${p.id}" data-name="${safe}"
                data-price="${p.price ?? ''}" data-stock="${stock}"
                style="padding:10px 14px;cursor:pointer;border-bottom:1px solid #eee">
                <div class="d-flex justify-content-between gap-2">
                  <span>${safe}</span>
                  <small class="text-muted">${priceTxt}</small>
                </div>
                <small class="${stock <= 0 ? 'text-danger' : 'text-secondary'}">${stockTxt}</small>
              </div>`;
    }).join('');
    prodDropdown.style.display = 'block';
    prodSearch.setAttribute('aria-expanded', 'true');
  }

  if (prodSearch) {
    prodSearch.addEventListener('keydown', (event) => {
      const options = [...(prodDropdown?.querySelectorAll('.product-item') || [])];
      if (event.key === 'ArrowDown' && options.length) {
        event.preventDefault();
        activeProductIndex = (activeProductIndex + 1) % options.length;
        updateActiveProduct();
      } else if (event.key === 'ArrowUp' && options.length) {
        event.preventDefault();
        activeProductIndex = activeProductIndex <= 0 ? options.length - 1 : activeProductIndex - 1;
        updateActiveProduct();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        if (options.length) selectProductItem(options[activeProductIndex >= 0 ? activeProductIndex : 0]);
      } else if (event.key === 'Escape' && prodDropdown?.style.display === 'block') {
        event.preventDefault();
        hideProdDropdown();
      }
    });

    prodSearch.addEventListener('input', () => {
      prodIdField.value = '';
      prodNameField.value = prodSearch.value.trim();
      dbPriceField.value = '';
      selectedProductStock = null;
      if (stockWarning) stockWarning.textContent = '';
      dbPriceField.classList.remove('field-auto');
      manualPriceField.classList.remove('field-auto');

      const q = prodSearch.value.trim();
      if (!q) { hideProdDropdown(); lastQuery = ''; return; }
      if (q === lastQuery) return;
      lastQuery = q;
      hideProdDropdown();
      clearTimeout(searchTimer);
      searchTimer = setTimeout(async () => {
        try {
          const res = await fetch('/api/search-products?q=' + encodeURIComponent(q));
          const items = await res.json();
          if (prodSearch.value.trim() === q) renderProdDropdown(items);
        } catch { hideProdDropdown(); }
      }, 120);
    });

    prodDropdown.addEventListener('mousedown', (e) => {
      const item = e.target.closest('.product-item');
      if (!item) return;
      e.preventDefault();
      selectProductItem(item);
    });

    document.addEventListener('click', (e) => {
      if (!prodSearch.contains(e.target) && !prodDropdown.contains(e.target)) {
        hideProdDropdown();
      }
    });
  }

  function updateStockWarning() {
    if (!stockWarning) return;
    if (selectedProductStock === null || !prodIdField.value) {
      stockWarning.textContent = '';
      return;
    }
    const requested = Math.max(1, parseInt(qtyField.value || '1', 10) || 1);
    const remaining = selectedProductStock - requested;
    if (remaining < 0) {
      stockWarning.className = 'small mt-2 text-warning-emphasis';
      stockWarning.textContent = `⚠️ موجودی کافی نیست؛ موجودی فعلی ${formatNumber(selectedProductStock)} است. ثبت فروش انجام می‌شود و موجودی منفی خواهد شد.`;
    } else {
      stockWarning.className = 'small mt-2 text-success';
      stockWarning.textContent = `موجودی فعلی: ${formatNumber(selectedProductStock)} عدد`;
    }
  }

  if (qtyField) qtyField.addEventListener('input', updateStockWarning);

  if (manualPriceField) {
    manualPriceField.addEventListener('input', () => {
      manualPriceField.classList.remove('field-auto');
      manualPriceField.value = formatNumber(parseNumber(manualPriceField.value));
    });
  }
  function updateDiscountModeLabel() {
    if (discountLabel && discountModeField) {
      discountLabel.textContent = discountModeField.value === 'line' ? 'تخفیف کل این ردیف' : 'تخفیف هر واحد';
    }
  }
  if (discountModeField) discountModeField.addEventListener('change', updateDiscountModeLabel);
  updateDiscountModeLabel();
  if (discountField) {
    discountField.addEventListener('input', () => {
      discountField.value = formatNumber(parseNumber(discountField.value));
    });
  }

  // add to cart
  const addBtn = document.getElementById('add-to-cart');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      const name = (prodNameField.value || prodSearch.value || '').trim();
      if (!name) return alert('نام کالا را وارد کنید');

      const pidRaw = prodIdField.value;
      const pid = pidRaw ? parseInt(pidRaw, 10) : null;
      const dbPrice = dbPriceField.value ? parseNumber(dbPriceField.value) : null;
      const manualPrice = parseNumber(manualPriceField.value);
      const enteredDiscount = parseNumber(discountField.value);
      const qty = parseInt(qtyField.value || '1', 10);
      const discountMode = discountModeField?.value || 'unit';
      const discountPerUnit = discountMode === 'unit' ? enteredDiscount : 0;
      const discountTotal = discountMode === 'line' ? enteredDiscount : 0;

      if (qty < 1) return alert('تعداد حداقل ۱ است');
      if (manualPrice < 0) return alert('قیمت منفی مجاز نیست');
      if (enteredDiscount < 0) return alert('تخفیف منفی مجاز نیست');
      if (discountPerUnit > manualPrice) return alert('تخفیف هر واحد بیشتر از قیمت است');
      if (discountTotal > (manualPrice - discountPerUnit) * qty) return alert('تخفیف کل ردیف بیشتر از مبلغ آن است');

      const custom = {};
      form.querySelectorAll('[data-item-col]').forEach(el => {
        const v = el.value.trim();
        if (v) custom[el.dataset.itemCol] = v;
      });

      const finalPrice = manualPrice - discountPerUnit;
      const existing = discountTotal === 0 ? cart.find(it =>
        it.product_id === pid &&
        it.product_name === name &&
        it.manual_unit_price === manualPrice &&
        it.discount_per_unit === discountPerUnit &&
        Number(it.discount_total || 0) === 0
      ) : null;

      if (existing) {
        existing.quantity += qty;
      } else {
        cart.push({
          product_id: pid,
          product_name: name,
          quantity: qty,
          db_unit_price: dbPrice,
          manual_unit_price: manualPrice,
          discount_per_unit: discountPerUnit,
          discount_total: discountTotal,
          final_unit_price: finalPrice,
          custom_values: custom,
        });
      }
      saveCart();
      renderCart();

      // reset
      prodSearch.value = '';
      lastQuery = '';
      clearTimeout(searchTimer);
      prodNameField.value = '';
      prodIdField.value = '';
      dbPriceField.value = '';
      selectedProductStock = null;
      if (stockWarning) stockWarning.textContent = '';
      dbPriceField.classList.remove('field-auto');
      manualPriceField.value = '';
      manualPriceField.classList.remove('field-auto');
      discountField.value = '0';
      if (discountModeField) discountModeField.value = 'unit';
      updateDiscountModeLabel();
      qtyField.value = '1';
      form.querySelectorAll('[data-item-col]').forEach(el => el.value = '');
      prodSearch.focus();
    });
  }

  // render cart
  const cartTbody = document.getElementById('cart-tbody');
  const cartEmpty = document.getElementById('cart-empty');
  const cartWrap = document.getElementById('cart-table-wrap');
  const totalDiscountEl = document.getElementById('total-discount');
  const totalFinalEl = document.getElementById('total-final');
  const cartCountEl = document.getElementById('cart-count');
  const submitBtn = document.getElementById('submit-invoice');

  window.__removeFromCart = function (i) {
    cart.splice(i, 1);
    saveCart();
    renderCart();
    window.requestAnimationFrame(() => {
      const removeButtons = cartTbody?.querySelectorAll('button[aria-label^="حذف"]') || [];
      const nextButton = removeButtons[Math.min(i, removeButtons.length - 1)];
      (nextButton || prodSearch)?.focus();
    });
  };

  const invoiceDiscountField = document.getElementById('invoice-discount-amount');
  const invoiceDiscountError = document.getElementById('invoice-discount-error');

  const customerSelect = document.getElementById('invoice-customer-id');
  const paymentMethodSelect = document.getElementById('invoice-payment-method');
  const cashPartWrap = document.getElementById('invoice-cash-part-wrap');
  const cashPartInput = document.getElementById('invoice-cash-part');
  const customerBalanceEl = document.getElementById('invoice-customer-balance');
  const cashPreviewEl = document.getElementById('invoice-cash-preview');
  const walletPreviewEl = document.getElementById('invoice-wallet-preview');
  const walletWarningEl = document.getElementById('invoice-wallet-warning');
  const paymentErrorEl = document.getElementById('invoice-payment-error');
  let customersById = new Map();
  let paymentValues = {cash: 0, wallet: 0, balanceAfter: 0};
  let mixedCashTouched = false;

  async function loadInvoiceCustomers() {
    if (!customerSelect) return;
    try {
      const response = await fetch('/api/customers/options');
      if (!response.ok) throw new Error('بارگذاری مشتری‌ها ناموفق بود');
      const customers = await response.json();
      customersById = new Map(customers.map(c => [String(c.id), c]));
      customerSelect.innerHTML = '<option value="">بدون اتصال به مشتری</option>' + customers.map(c =>
        `<option value="${c.id}">${escapeHtml(c.full_name)}${c.phone ? ' · ' + escapeHtml(c.phone) : ''}</option>`
      ).join('');
      updatePaymentUI();
    } catch (err) {
      if (customerBalanceEl) customerBalanceEl.textContent = 'بارگذاری فهرست مشتری‌ها انجام نشد؛ فاکتور نقدی همچنان قابل ثبت است.';
    }
  }

  function getPayableNow() {
    const subtotal = cart.reduce((sum, it) => sum + Math.max(0, Number(it.final_unit_price || 0) * Number(it.quantity || 0) - Number(it.discount_total || 0)), 0);
    return Math.max(0, subtotal - parseNumber(invoiceDiscountField?.value || '0'));
  }

  function updatePaymentUI(payable = getPayableNow()) {
    const method = paymentMethodSelect?.value || 'cash';
    const customerId = customerSelect?.value || '';
    const customer = customersById.get(customerId);
    let cash = 0, wallet = 0;
    if (method === 'wallet') {
      wallet = payable;
    } else if (method === 'mixed') {
      cashPartWrap?.classList.remove('d-none');
      if (!mixedCashTouched && cashPartInput) cashPartInput.value = formatNumber(payable);
      cash = Math.max(0, parseNumber(cashPartInput?.value || '0'));
      wallet = Math.max(0, payable - cash);
    } else {
      cash = payable;
    }
    if (method !== 'mixed') cashPartWrap?.classList.add('d-none');
    const balance = customer ? Number(customer.balance || 0) : 0;
    const balanceAfter = balance - wallet;
    paymentValues = {cash, wallet, balanceAfter};
    if (cashPreviewEl) cashPreviewEl.textContent = formatToman(cash);
    if (walletPreviewEl) walletPreviewEl.textContent = formatToman(wallet);
    if (customerBalanceEl) {
      if (customer) {
        const debtText = balance < 0 ? `بدهی فعلی: ${formatToman(Math.abs(balance))}` : `موجودی فعلی: ${formatToman(balance)}`;
        customerBalanceEl.textContent = debtText + (wallet > 0 ? ` · پس از خرید: ${balanceAfter < 0 ? 'بدهی ' + formatToman(Math.abs(balanceAfter)) : formatToman(balanceAfter)}` : '');
      } else {
        customerBalanceEl.textContent = wallet > 0 ? 'برای استفاده از کیف پول، مشتری را انتخاب کنید.' : 'برای فاکتور نقدی، انتخاب مشتری اختیاری است.';
      }
    }
    walletWarningEl?.classList.toggle('d-none', !(wallet > 0 && customer && balanceAfter < 0));
    let error = '';
    if (cash > payable) error = 'مبلغ نقدی نمی‌تواند از مبلغ قابل پرداخت بیشتر باشد.';
    else if (wallet > 0 && !customer) error = 'برای پرداخت از کیف پول، انتخاب مشتری الزامی است.';
    if (paymentErrorEl) paymentErrorEl.textContent = error;
    return !error;
  }

  customerSelect?.addEventListener('change', () => updatePaymentUI());
  paymentMethodSelect?.addEventListener('change', () => {
    mixedCashTouched = false;
    updatePaymentUI();
  });
  cashPartInput?.addEventListener('input', () => {
    mixedCashTouched = true;
    const raw = cashPartInput.value;
    cashPartInput.value = formatNumber(parseNumber(raw));
    updatePaymentUI();
  });
  loadInvoiceCustomers();

  function renderCart() {
    if (cartCountEl) cartCountEl.textContent = cart.length;
    let itemDiscount = 0;
    let subtotal = 0;
    cart.forEach(it => {
      const qty = Number(it.quantity || 0);
      const unitDiscount = Number(it.discount_per_unit || 0) * qty;
      const lineDiscount = Number(it.discount_total || 0);
      itemDiscount += unitDiscount + lineDiscount;
      subtotal += Math.max(0, Number(it.final_unit_price || 0) * qty - lineDiscount);
    });
    const invoiceDiscount = parseNumber(invoiceDiscountField?.value || '0');
    const invalidInvoiceDiscount = invoiceDiscount > subtotal;
    const payable = Math.max(0, subtotal - invoiceDiscount);
    if (invoiceDiscountError) invoiceDiscountError.classList.toggle('d-none', !invalidInvoiceDiscount);
    if (invoiceDiscountField) invoiceDiscountField.classList.toggle('is-invalid', invalidInvoiceDiscount);

    if (!cart.length) {
      cartEmpty.classList.remove('d-none');
      cartWrap.classList.add('d-none');
      submitBtn.disabled = true;
      if (totalDiscountEl) totalDiscountEl.textContent = '۰ تومان';
      if (totalFinalEl) totalFinalEl.textContent = '۰ تومان';
      updatePaymentUI(0);
      return;
    }

    cartEmpty.classList.add('d-none');
    cartWrap.classList.remove('d-none');
    submitBtn.disabled = invalidInvoiceDiscount;

    cartTbody.innerHTML = cart.map((it, i) => {
      const qty = Number(it.quantity || 0);
      const subtotal = Math.max(0, Number(it.final_unit_price || 0) * qty - Number(it.discount_total || 0));
      const discountText = Number(it.discount_total || 0) > 0
        ? `کل ردیف: ${formatNumber(it.discount_total)}`
        : `هر واحد: ${formatNumber(Number(it.discount_per_unit || 0))}`;
      return `<tr>
        <td>${escapeHtml(it.product_name)}</td>
        <td>${qty}</td>
        <td><small>${it.db_unit_price != null ? formatNumber(it.db_unit_price) : '—'}</small></td>
        <td>${formatNumber(it.manual_unit_price)}</td>
        <td>${discountText}</td>
        <td><strong>${formatNumber(subtotal)}</strong></td>
        <td>
          <button type="button" class="btn btn-sm btn-outline-danger"
                  aria-label="حذف ${escapeHtml(it.product_name)} از سبد" title="حذف این قلم"
                  onclick="window.__removeFromCart(${i})">
            <i class="bi bi-x" aria-hidden="true"></i>
          </button>
        </td>
      </tr>`;
    }).join('');

    if (totalDiscountEl) totalDiscountEl.textContent = formatToman(itemDiscount + invoiceDiscount);
    if (totalFinalEl) totalFinalEl.textContent = formatToman(payable);
    updatePaymentUI(payable);
  }
  if (invoiceDiscountField) {
    invoiceDiscountField.addEventListener('input', () => {
      invoiceDiscountField.value = formatNumber(parseNumber(invoiceDiscountField.value));
      renderCart();
    });
  }

  // clear cart
  const clearBtn = document.getElementById('clear-cart');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      if (!cart.length) return;
      if (!confirm('سبد پاک شود؟')) return;
      cart = [];
      saveCart();
      renderCart();
    });
  }

  // submit
  let submitting = false;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (submitting) return;

    if (dtEditor && !dtEditor.classList.contains('d-none') && dtInput.value) {
      customDatetime = new Date(dtInput.value).toISOString();
      datetimeEdited = true;
    }

    if (!cart.length) return alert('سبد خالی است');
    const currentSubtotal = cart.reduce((sum, it) => sum + Math.max(0, Number(it.final_unit_price || 0) * Number(it.quantity || 0) - Number(it.discount_total || 0)), 0);
    if (parseNumber(invoiceDiscountField?.value || '0') > currentSubtotal) {
      return alert('تخفیف کل فاکتور نمی‌تواند از مبلغ سبد بیشتر باشد.');
    }

    const invoiceDiscountAmount = parseNumber(invoiceDiscountField?.value || '0');
    const payableAmount = Math.max(0, currentSubtotal - invoiceDiscountAmount);
    if (!updatePaymentUI(payableAmount)) return alert(paymentErrorEl?.textContent || 'اطلاعات پرداخت نامعتبر است.');
    const paymentSummary = `نقدی: ${formatNumber(paymentValues.cash)} تومان، کیف پول: ${formatNumber(paymentValues.wallet)} تومان`;
    const debtSummary = paymentValues.wallet > 0 && paymentValues.balanceAfter < 0
      ? `\nهشدار: موجودی کیف پول منفی و بدهی ${formatNumber(Math.abs(paymentValues.balanceAfter))} تومان خواهد شد؛ ادمین باید آن را بررسی کند.` : '';
    const confirmMessage = `فاکتور ${cart.length} قلمی با مبلغ ${formatNumber(payableAmount)} تومان ثبت شود؟\n${paymentSummary}${debtSummary}`;
    if (!window.confirm(confirmMessage)) return;

    const saleValues = {};
    form.querySelectorAll('[data-sale-col]').forEach(el => {
      const v = el.value.trim();
      if (v) saleValues[el.dataset.saleCol] = v;
    });

    const payload = {
      source: KIND,
      customer_id: customerSelect?.value || null,
      payment_method: paymentMethodSelect?.value || 'cash',
      cash_paid: paymentValues.cash,
      wallet_paid: paymentValues.wallet,
      sale_values: saleValues,
      items: cart.map(it => ({
        product_id: it.product_id,
        product_name: it.product_name,
        quantity: it.quantity,
        manual_unit_price: it.manual_unit_price,
        discount_per_unit: it.discount_per_unit,
        discount_total: Number(it.discount_total || 0),
        custom_values: it.custom_values || {},
      })),
      invoice_discount_amount: parseNumber(invoiceDiscountField?.value || '0'),
      datetime_edited: datetimeEdited,
    };
    if (datetimeEdited && customDatetime) payload.created_at = customDatetime;

    const origHTML = submitBtn.innerHTML;
    submitting = true;
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> در حال ثبت...';

    try {
      const res = await fetch('/api/sale/invoice', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': CSRF,
        },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let err = 'خطا در ثبت';
        try { const j = await res.json(); err = j.detail || err; } catch {}
        throw new Error(err);
      }
      const data = await res.json();
      if (paymentValues.wallet > 0 && customerSelect?.value && data.wallet_balance_after != null) {
        const chosenCustomer = customersById.get(String(customerSelect.value));
        if (chosenCustomer) chosenCustomer.balance = Number(data.wallet_balance_after);
      }
      cart = [];
      saveCart();
      if (invoiceDiscountField) invoiceDiscountField.value = '0';
      renderCart();
      submitBtn.innerHTML = origHTML;
      submitting = false;
      submitBtn.disabled = false;
      window.open(data.invoice_url, '_blank');
      if (data.wallet_warning) {
        alert(`فاکتور ثبت شد. موجودی کیف پول «${data.wallet_customer || 'مشتری'}» منفی شد و به ${formatNumber(Math.abs(Number(data.wallet_balance_after || 0)))} تومان بدهی رسید. ادمین باید صحت این بدهی را بررسی کند.`);
      }
      if (Array.isArray(data.stock_warnings) && data.stock_warnings.length) {
        const details = data.stock_warnings.map(w =>
          `«${w.product_name}»: موجودی ${formatNumber(w.previous_stock)}، فروش ${formatNumber(w.sold_quantity)}، موجودی نهایی ${formatNumber(w.new_stock)}`
        ).join('\n');
        alert('فاکتور ثبت شد، اما موجودی برخی کالاها منفی شد:\n' + details);
      }
    } catch (err) {
      alert('خطا: ' + err.message);
      submitBtn.disabled = false;
      submitBtn.innerHTML = origHTML;
      submitting = false;
    }
  });

  renderCart();
})();