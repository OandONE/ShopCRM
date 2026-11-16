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
  const qtyField = document.getElementById('add-qty');

  let searchTimer = null;
  let lastQuery = '';

  function hideProdDropdown() {
    if (prodDropdown) { prodDropdown.style.display = 'none'; prodDropdown.innerHTML = ''; }
  }

  function renderProdDropdown(items) {
    if (!items.length) { hideProdDropdown(); return; }
    prodDropdown.innerHTML = items.map(p => {
      const safe = escapeHtml(p.name);
      const priceTxt = p.price != null ? formatNumber(p.price) + ' ت' : 'بدون قیمت';
      return `<div class="product-item" data-id="${p.id}" data-name="${safe}" data-price="${p.price ?? ''}"
                style="padding:10px 14px;cursor:pointer;border-bottom:1px solid #eee">
                <div class="d-flex justify-content-between">
                  <span>${safe}</span>
                  <small class="text-muted">${priceTxt}</small>
                </div>
              </div>`;
    }).join('');
    prodDropdown.style.display = 'block';
  }

  if (prodSearch) {
    prodSearch.addEventListener('input', () => {
      prodIdField.value = '';
      prodNameField.value = prodSearch.value.trim();
      dbPriceField.value = '';
      dbPriceField.classList.remove('field-auto');
      manualPriceField.classList.remove('field-auto');

      const q = prodSearch.value.trim();
      if (!q) { hideProdDropdown(); return; }
      if (q === lastQuery) return;
      lastQuery = q;
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
      const name = item.dataset.name;
      const pid = item.dataset.id;
      const price = item.dataset.price;
      prodSearch.value = name;
      prodNameField.value = name;
      prodIdField.value = pid;
      dbPriceField.value = price ? formatNumber(price) : '';
      dbPriceField.classList.add('field-auto');
      manualPriceField.value = price ? formatNumber(price) : '';
      manualPriceField.classList.add('field-auto');
      hideProdDropdown();
      manualPriceField.focus();
    });

    document.addEventListener('click', (e) => {
      if (!prodSearch.contains(e.target) && !prodDropdown.contains(e.target)) {
        hideProdDropdown();
      }
    });
  }

  if (manualPriceField) {
    manualPriceField.addEventListener('input', () => {
      manualPriceField.classList.remove('field-auto');
      manualPriceField.value = formatNumber(parseNumber(manualPriceField.value));
    });
  }
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
      const discount = parseNumber(discountField.value);
      const qty = parseInt(qtyField.value || '1', 10);

      if (qty < 1) return alert('تعداد حداقل ۱ است');
      if (manualPrice < 0) return alert('قیمت منفی مجاز نیست');
      if (discount < 0) return alert('تخفیف منفی مجاز نیست');
      if (discount > manualPrice) return alert('تخفیف بیشتر از قیمت است');

      const custom = {};
      form.querySelectorAll('[data-item-col]').forEach(el => {
        const v = el.value.trim();
        if (v) custom[el.dataset.itemCol] = v;
      });

      const finalPrice = manualPrice - discount;
      const existing = cart.find(it =>
        it.product_id === pid &&
        it.product_name === name &&
        it.manual_unit_price === manualPrice &&
        it.discount_per_unit === discount
      );

      if (existing) {
        existing.quantity += qty;
      } else {
        cart.push({
          product_id: pid,
          product_name: name,
          quantity: qty,
          db_unit_price: dbPrice,
          manual_unit_price: manualPrice,
          discount_per_unit: discount,
          final_unit_price: finalPrice,
          custom_values: custom,
        });
      }
      saveCart();
      renderCart();

      // reset
      prodSearch.value = '';
      prodNameField.value = '';
      prodIdField.value = '';
      dbPriceField.value = '';
      dbPriceField.classList.remove('field-auto');
      manualPriceField.value = '';
      manualPriceField.classList.remove('field-auto');
      discountField.value = '0';
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
  };

  function renderCart() {
    if (cartCountEl) cartCountEl.textContent = cart.length;

    if (!cart.length) {
      cartEmpty.classList.remove('d-none');
      cartWrap.classList.add('d-none');
      submitBtn.disabled = true;
      if (totalDiscountEl) totalDiscountEl.textContent = '۰ تومان';
      if (totalFinalEl) totalFinalEl.textContent = '۰ تومان';
      return;
    }

    cartEmpty.classList.add('d-none');
    cartWrap.classList.remove('d-none');
    submitBtn.disabled = false;

    let totalDiscount = 0, totalFinal = 0;

    cartTbody.innerHTML = cart.map((it, i) => {
      const subtotal = it.final_unit_price * it.quantity;
      totalDiscount += it.discount_per_unit * it.quantity;
      totalFinal += subtotal;
      return `<tr>
        <td>${escapeHtml(it.product_name)}</td>
        <td>${it.quantity}</td>
        <td><small>${it.db_unit_price != null ? formatNumber(it.db_unit_price) : '—'}</small></td>
        <td>${formatNumber(it.manual_unit_price)}</td>
        <td>${formatNumber(it.discount_per_unit)}</td>
        <td><strong>${formatNumber(subtotal)}</strong></td>
        <td>
          <button type="button" class="btn btn-sm btn-outline-danger"
                  onclick="window.__removeFromCart(${i})">
            <i class="bi bi-x"></i>
          </button>
        </td>
      </tr>`;
    }).join('');

    if (totalDiscountEl) totalDiscountEl.textContent = formatToman(totalDiscount);
    if (totalFinalEl) totalFinalEl.textContent = formatToman(totalFinal);
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
  form.addEventListener('submit', async (e) => {
    e.preventDefault();

    
    if (dtEditor && !dtEditor.classList.contains('d-none') && dtInput.value) {
      customDatetime = new Date(dtInput.value).toISOString();
      datetimeEdited = true;
    }

    if (!cart.length) return alert('سبد خالی است');

    const saleValues = {};
    form.querySelectorAll('[data-sale-col]').forEach(el => {
      const v = el.value.trim();
      if (v) saleValues[el.dataset.saleCol] = v;
    });

    const payload = {
      source: KIND,
      sale_values: saleValues,
      items: cart.map(it => ({
        product_id: it.product_id,
        product_name: it.product_name,
        quantity: it.quantity,
        manual_unit_price: it.manual_unit_price,
        discount_per_unit: it.discount_per_unit,
        custom_values: it.custom_values || {},
      })),
      datetime_edited: datetimeEdited,
    };
    if (datetimeEdited && customDatetime) payload.created_at = customDatetime;

    const origHTML = submitBtn.innerHTML;
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
      cart = [];
      saveCart();
    //   window.location.href = data.redirect || ('/' + KIND);
      window.open(data.invoice_url, '_blank');
    } catch (err) {
      alert('خطا: ' + err.message);
      submitBtn.disabled = false;
      submitBtn.innerHTML = origHTML;
    }
  });

  renderCart();
})();