(() => {
  document.querySelectorAll('.stop-section').forEach(section => {
  const find = name => section.querySelector(`[data-stop="${name}"]`);
  const table = find('table');
  if (!table) return;
  const body = table.tBodies[0], all = Array.from(body.rows);
  const search = find('search'), filter = find('filter');
  const size = find('size'), prev = find('prev'), next = find('next');
  let page = 1, column = 0, direction = -1, kind = 'number';
  const value = row => {
    const cell = row.cells[column], raw = cell.dataset.value ?? cell.textContent.trim();
    if (raw === '' || raw === '—') return null;
    return kind === 'number' ? Number(raw) : raw.toLowerCase();
  };
  function render() {
    const query = search.value.trim().toLowerCase(), selected = filter.value;
    const rows = all.filter(row => row.dataset.ticker.includes(query) &&
      (selected === 'all' || row.dataset.status === selected ||
       (selected === 'hit' && row.dataset.hit === 'yes') ||
       (selected === 'missing' && row.dataset.missing === 'yes')));
    rows.sort((a,b) => {
      const left = value(a), right = value(b);
      if (left === null || right === null) return left === right ? 0 : left === null ? 1 : -1;
      return (left < right ? -1 : left > right ? 1 : 0) * direction;
    });
    const limit = Number(size.value), pages = Math.max(1, Math.ceil(rows.length / limit));
    page = Math.max(1, Math.min(page, pages));
    body.replaceChildren(...rows.slice((page - 1) * limit, page * limit));
    find('page').textContent = `Page ${page} of ${pages} · ${rows.length} bets`;
    prev.disabled = page === 1; next.disabled = page === pages;
  }
  table.querySelectorAll('thead button').forEach((button,index) => button.addEventListener('click', () => {
    direction = column === index ? -direction : 1; column = index; kind = button.dataset.kind; page = 1;
    table.querySelectorAll('th').forEach(th => th.removeAttribute('aria-sort'));
    button.parentElement.setAttribute('aria-sort', direction === 1 ? 'ascending' : 'descending');
    render();
  }));
  [search, filter, size].forEach(control => control.addEventListener('input', () => {page = 1; render();}));
  prev.addEventListener('click', () => {page--; render();});
  next.addEventListener('click', () => {page++; render();});
  render();
  });
})();
