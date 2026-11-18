(() => {
  document.querySelectorAll('.funding-table-widget').forEach(widget => {
    const table = widget.querySelector('table'), body = table.tBodies[0];
    const all = Array.from(body.querySelectorAll('[data-entry]'));
    const size = widget.querySelector('[data-page-size]'), search = widget.querySelector('[data-search]');
    const event = widget.querySelector('[data-event]'), funding = widget.querySelector('[data-funding-filter]');
    const metric = widget.querySelector('[data-sort-metric]');
    const prev = widget.querySelector('[data-prev]'), next = widget.querySelector('[data-next]');
    let page = 1, key = widget.dataset.defaultSort, direction = 1;
    const order = document.createElement('button');
    order.type = 'button';
    order.className = 'funding-sort-order';
    order.setAttribute('aria-label', 'Toggle sort direction');
    widget.querySelector('.funding-table-tools').append(order);
    function render() {
      const query = (search?.value || '').trim().toLowerCase();
      order.textContent = direction === 1 ? 'Ascending ↑' : 'Descending ↓';
      const rows = all.filter(row => {
        const d = row.dataset;
        return (!query || (d.search || '').toLowerCase().includes(query)) &&
          (!event?.value || (event.value === 'exit' ? d.event !== 'Buy' : d.event === event.value)) &&
          (!funding?.value || Number(d[funding.value]) > 0);
      });
      rows.sort((a,b) => {
        const rawA = a.dataset[key], rawB = b.dataset[key];
        const missingA = rawA == null || rawA === '', missingB = rawB == null || rawB === '';
        if (missingA || missingB) return missingA === missingB ? 0 : missingA ? 1 : -1;
        const text = key === 'date' || key === 'stock';
        const left = text ? rawA.toLowerCase() : Number(rawA), right = text ? rawB.toLowerCase() : Number(rawB);
        return (left < right ? -1 : left > right ? 1 : 0) * direction;
      });
      const limit = Number(size.value), pages = Math.max(1, Math.ceil(rows.length / limit));
      page = Math.min(Math.max(1,page),pages);
      body.replaceChildren(...rows.slice((page-1)*limit,page*limit));
      if (!rows.length) {
        const tr = document.createElement('tr'), td = document.createElement('td');
        td.colSpan = table.tHead.rows[0].cells.length;
        td.textContent = 'No rows match the selected filters.'; td.className = 'empty';
        tr.append(td); body.append(tr);
      }
      widget.querySelector('[data-page-info]').textContent = `${rows.length ? (page-1)*limit+1 : 0}–${Math.min(page*limit,rows.length)} of ${rows.length} rows · Page ${page} of ${pages}`;
      prev.disabled = page === 1; next.disabled = page === pages;
      table.querySelectorAll('[data-sort]').forEach(button => {
        button.parentElement.setAttribute('aria-sort', button.dataset.sort === key ? (direction === 1 ? 'ascending' : 'descending') : 'none');
      });
    }
    table.querySelectorAll('[data-sort]').forEach(button => button.addEventListener('click', () => {
      direction = key === button.dataset.sort ? -direction : 1; key = button.dataset.sort;
      if (metric) metric.value = key;
      page = 1; render();
    }));
    metric?.addEventListener('change', () => {key = metric.value; direction = 1; page = 1; render();});
    order.addEventListener('click', () => {direction *= -1; page = 1; render();});
    [size,search,event,funding].filter(Boolean).forEach(control => control.addEventListener('input', () => {page = 1; render();}));
    prev.addEventListener('click', () => {page--; render();});
    next.addEventListener('click', () => {page++; render();});
    render();
  });
})();
