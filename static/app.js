/* ShopCRM - shared frontend logic */

function formatNumber(num) {
    return num.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

async function updatePriceWords(input) {
    const raw = input.value.replace(/,/g, "");
    const wordsDiv = input.parentElement.querySelector('.price-words');
    if (!raw || isNaN(raw)) {
        input.dataset.raw = "";
        if (wordsDiv) wordsDiv.textContent = "";
        return;
    }
    input.dataset.raw = raw;
    input.value = formatNumber(raw);
    try {
        const res = await fetch(`/api/price-to-words?price=${encodeURIComponent(raw)}`);
        const data = await res.json();
        if (wordsDiv) wordsDiv.textContent = data.words || "";
    } catch {}
}

// Prodouct autocomplete
function bindProductSearch(input) {
    const wrapper = input.closest('.product-wrapper');
    if (!wrapper) return;
    const dropdown = wrapper.querySelector('.product-dropdown');
    const hidden = wrapper.querySelector('.product-value');
    if (!dropdown || !hidden) return;

    let timer = null;
    let lastQ = null;

    function hide() {
        dropdown.classList.add('d-none');
        dropdown.innerHTML = '';
        lastQ = null;
    }

    function show(list) {
        if (!list.length) { hide(); return; }
        dropdown.innerHTML = list.map(p => {
            const safe = String(p.name)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
            return `<button type="button" class="list-group-item list-group-item-action"
                            data-name="${safe}">${safe}</button>`;
        }).join('');
        dropdown.classList.remove('d-none');
    }

    input.addEventListener('input', () => {
        hidden.value = '';
        const q = input.value.trim();
        if (!q) { hide(); return; }
        if (q === lastQ) return;
        lastQ = q;
        clearTimeout(timer);
        timer = setTimeout(async () => {
            try {
                const res = await fetch('/api/search-products?q=' + encodeURIComponent(q));
                const list = await res.json();
                if (input.value.trim() === q) show(list);
            } catch { hide(); }
        }, 120);
    });

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') hide();
    });

    dropdown.addEventListener('mousedown', (e) => {
        const btn = e.target.closest('[data-name]');
        if (!btn) return;
        e.preventDefault();
        input.value = btn.dataset.name;
        hidden.value = btn.dataset.name;
        hide();
        input.focus();
    });

    document.addEventListener('click', (e) => {
        if (!wrapper.contains(e.target)) hide();
    });
}

// Attach on DOM ready
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.product-search').forEach(bindProductSearch);

    const form = document.getElementById('sale-form');
    if (form) {
        form.addEventListener('submit', () => {
            document.querySelectorAll('.price-input').forEach(input => {
                input.value = input.dataset.raw || input.value.replace(/,/g, "");
            });
            document.querySelectorAll('.product-wrapper').forEach(w => {
                const visible = w.querySelector('.product-search');
                const hidden = w.querySelector('.product-value');
                if (visible && hidden && !hidden.value && visible.value.trim()) {
                    hidden.value = visible.value.trim();
                }
            });
        });
    }
});