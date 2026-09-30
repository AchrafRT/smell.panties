(() => {
  const flashes = document.querySelectorAll('.flash');
  if (flashes.length) {
    window.setTimeout(() => flashes.forEach((el) => el.classList.add('flash-fade')), 5000);
  }

  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm || 'Are you sure?')) event.preventDefault();
    });
  });

  document.querySelectorAll('input[type="file"]').forEach((input) => {
    input.addEventListener('change', () => {
      const label = input.closest('label');
      if (!label || !input.files || !input.files[0]) return;
      let note = label.querySelector('.file-picked');
      if (!note) {
        note = document.createElement('small');
        note.className = 'file-picked';
        label.appendChild(note);
      }
      note.textContent = `Selected: ${input.files[0].name}`;
    });
  });
})();
