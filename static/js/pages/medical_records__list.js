(function () {
  var form        = document.getElementById('filter-form');
  var hidType     = document.getElementById('hid-type');
  var hidPeriod   = document.getElementById('hid-period');
  var customRow   = document.getElementById('custom-dates-row');
  var searchInput = document.getElementById('search-input');
  var searchClear = document.getElementById('search-clear');
  var typeSelect  = document.getElementById('type-select');

  typeSelect.addEventListener('change', function () {
    hidType.value = typeSelect.value;
    form.submit();
  });

  document.querySelectorAll('[data-period]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var value = btn.dataset.period;
      hidPeriod.value = value;
      document.querySelectorAll('[data-period]').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      customRow.classList.toggle('visible', value === 'custom');
      if (value !== 'custom') {
        document.getElementById('date-from').value = '';
        document.getElementById('date-to').value   = '';
        form.submit();
      }
    });
  });

  searchClear.addEventListener('click', function () {
    searchInput.value = '';
    form.submit();
  });

  searchInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); form.submit(); }
  });

  var searchTimer;
  searchInput.addEventListener('input', function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { form.submit(); }, 600);
  });
}());
