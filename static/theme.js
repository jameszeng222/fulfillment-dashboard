(() => {
  const storageKey = 'fulfillment-theme-accent';
  const themes = new Set(['moss', 'fog', 'iris', 'gray']);
  const root = document.documentElement;
  const url = new URL(window.location.href);
  const requested = url.searchParams.get('theme');
  let saved;
  try { saved = localStorage.getItem(storageKey); } catch {}
  root.dataset.accent = themes.has(requested) ? requested : themes.has(saved) ? saved : 'moss';

  document.addEventListener('DOMContentLoaded', () => {
    const picker = document.getElementById('themeAccent');
    if (!picker) return;
    picker.value = root.dataset.accent;
    picker.addEventListener('change', () => {
      const theme = picker.value;
      if (!themes.has(theme) || theme === root.dataset.accent) return;
      root.dataset.accent = theme;
      try { localStorage.setItem(storageKey, theme); } catch {}
      const current = new URL(window.location.href);
      if (current.searchParams.has('theme')) {
        current.searchParams.delete('theme');
        history.replaceState(history.state, '', current);
      }
      document.dispatchEvent(new Event('accentchange'));
    });
  });
})();
