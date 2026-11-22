/* ShopCRM keyboard navigation and page shortcuts */
(() => {
  'use strict';

  const routeShortcuts = new Map([
    ['Digit1', '/'], ['Numpad1', '/'],
    ['Digit2', '/quick'], ['Numpad2', '/quick'],
    ['Digit3', '/full'], ['Numpad3', '/full'],
    ['Digit4', '/product-search'], ['Numpad4', '/product-search'],
    ['Digit5', '/search'], ['Numpad5', '/search'],
    ['Digit6', '/sales'], ['Numpad6', '/sales'],
    ['Digit7', '/returns'], ['Numpad7', '/returns'],
    ['Digit8', '/reports'], ['Numpad8', '/reports'],
    ['Digit9', '/admin'], ['Numpad9', '/admin'],
    ['KeyH', '/shortcuts'],
  ]);

  const announce = (message) => {
    const region = document.getElementById('shortcut-announcer');
    if (region) {
      region.textContent = '';
      window.setTimeout(() => { region.textContent = message; }, 20);
    }
  };

  const focusElement = (selector, message) => {
    const el = document.querySelector(selector);
    if (!el || el.disabled || el.hidden) return false;
    el.focus();
    if (typeof el.select === 'function' && el.tagName === 'INPUT') el.select();
    if (message) announce(message);
    return true;
  };

  function closeOpenMenus() {
    document.querySelectorAll('.dropdown-menu.show').forEach(menu => {
      const parent = menu.closest('.dropdown');
      const toggle = parent?.querySelector('[data-bs-toggle="dropdown"]');
      if (toggle && window.bootstrap?.Dropdown) {
        window.bootstrap.Dropdown.getOrCreateInstance(toggle).hide();
        toggle.focus();
      }
    });
    const nav = document.getElementById('nav');
    if (nav?.classList.contains('show') && window.bootstrap?.Collapse) {
      window.bootstrap.Collapse.getOrCreateInstance(nav).hide();
      document.querySelector('[data-bs-target="#nav"]')?.focus();
    }
    document.dispatchEvent(new CustomEvent('shopcrm:escape'));
  }

  function runPageShortcut(event) {
    const target = event.target;
    const inInvoice = Boolean(document.getElementById('invoice-form'));
    const inReturnForm = Boolean(document.getElementById('submit-return'));

    // F2 always moves to the primary input of the current workflow
    if (event.code === 'F2' && !event.ctrlKey && !event.altKey && !event.metaKey) {
      let selector = '#product-search-input';
      let message = 'جستجوی کالا';
      if (inInvoice) {
        selector = '#add-product-search';
        message = 'جستجوی کالا برای افزودن به فاکتور';
      } else if (document.getElementById('filter-value')) {
        selector = '#filter-value';
        message = 'مقدار فیلتر';
      } else if (inReturnForm) {
        selector = '.return-qty:not(:disabled)';
        message = 'تعداد مرجوعی اولین قلم';
      }
      if (focusElement(selector, message)) event.preventDefault();
      return;
    }

    if (inInvoice) {
      if (event.ctrlKey && event.altKey && event.shiftKey && !event.metaKey) {
        const invoiceFocusShortcuts = new Map([
          ['KeyP', ['#add-product-search', 'جستجوی کالا']],
          ['KeyM', ['#add-manual-price', 'قیمت فروش']],
          ['KeyQ', ['#add-qty', 'تعداد کالا']],
          ['KeyD', ['#add-discount', 'تخفیف قلم']],
          ['KeyI', ['#invoice-discount-amount', 'تخفیف کل فاکتور']],
        ]);
        const action = invoiceFocusShortcuts.get(event.code);
        if (action) {
          if (focusElement(action[0], action[1])) event.preventDefault();
          return;
        }
      }
      if (event.ctrlKey && event.key === 'Enter' && !event.shiftKey && !event.altKey && !event.metaKey) {
        const dropdownOpen = document.getElementById('add-product-dropdown')?.style.display === 'block';
        const inItemEntry = target?.closest('.product-wrapper')
          || ['add-manual-price', 'add-discount-mode', 'add-discount', 'add-qty'].includes(target?.id)
          || target?.hasAttribute('data-item-col');
        if (inItemEntry && !dropdownOpen && !target?.matches('textarea') && !target?.isContentEditable) {
          const addButton = document.getElementById('add-to-cart');
          if (addButton && !addButton.disabled) {
            event.preventDefault();
            addButton.click();
          }
        }
        return;
      }
      if (event.ctrlKey && event.altKey && event.shiftKey && event.key === 'Enter' && !event.metaKey) {
        const dropdownOpen = document.getElementById('add-product-dropdown')?.style.display === 'block';
        if (!dropdownOpen && !target?.matches('textarea, select, #datetime-input') && !target?.isContentEditable) {
          const submit = document.getElementById('submit-invoice');
          if (submit && !submit.disabled) {
            event.preventDefault();
            submit.click();
          }
        }
        return;
      }
    }

    if (inReturnForm && event.ctrlKey && event.altKey && event.shiftKey && event.key === 'Enter' && !event.metaKey) {
      if (!target?.matches('textarea, select') && !target?.isContentEditable) {
        event.preventDefault();
        document.getElementById('submit-return')?.click();
      }
      return;
    }

    // Search pages: Enter adds a condition, Ctrl+Enter runs the search
    if (document.getElementById('filter-value')) {
      if (event.key === 'Enter' && !event.ctrlKey && !event.altKey && !event.metaKey && target?.id === 'filter-value') {
        event.preventDefault();
        if (typeof window.addFilter === 'function') window.addFilter();
      } else if (event.ctrlKey && event.key === 'Enter' && !event.shiftKey && !event.altKey && !event.metaKey) {
        if (typeof window.applyFilters === 'function') {
          event.preventDefault(); window.applyFilters();
        } else if (typeof window.doSearch === 'function') {
          event.preventDefault(); window.doSearch();
        }
      }
    }
  }

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeOpenMenus();
      return;
    }

    // Three modifiers avoid the common Alt+Shift keyboard-layout switch chord
    if (event.ctrlKey && event.altKey && event.shiftKey && !event.metaKey) {
      const destination = routeShortcuts.get(event.code);
      if (destination) {
        const allowedLink = [...document.querySelectorAll('a[href]')]
          .some(link => link.getAttribute('href') === destination);
        if (allowedLink || destination === '/shortcuts') {
          event.preventDefault();
          window.location.href = destination;
        } else {
          event.preventDefault();
          announce('این بخش برای حساب کاربری شما در دسترس نیست.');
        }
        return;
      }
    }

    runPageShortcut(event);
  });
})();