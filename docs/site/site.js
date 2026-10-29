const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

// All three answers remain readable when JavaScript is unavailable.
const controls = document.querySelector('.question-controls');
const tabs = [...document.querySelectorAll('[data-question]')];
const answers = [...document.querySelectorAll('.answer')];
function selectQuestion(tab, focus = false) {
  tabs.forEach(item => {
    const selected = item === tab;
    item.setAttribute('aria-selected', String(selected));
    item.tabIndex = selected ? 0 : -1;
  });
  answers.forEach(answer => { answer.hidden = answer.id !== tab.getAttribute('aria-controls'); });
  if (focus) tab.focus();
}
if (controls && tabs.length) {
  controls.hidden = false;
  answers.forEach(answer => {
    answer.setAttribute('role', 'tabpanel');
    answer.setAttribute('aria-labelledby', `tab-${answer.id.slice(7)}`);
    answer.tabIndex = 0;
  });
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectQuestion(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); selectQuestion(tabs[next], true); }
    });
  });
  selectQuestion(tabs[0]);
}

document.querySelectorAll('[data-copy]').forEach(button => {
  button.hidden = false;
  button.addEventListener('click', async () => {
    const source = document.getElementById(button.dataset.copy);
    const label = button.innerHTML;
    try {
      await navigator.clipboard.writeText(source.textContent);
      button.textContent = 'Copied';
      document.getElementById('copy-status').textContent = 'Command copied to clipboard.';
      window.setTimeout(() => { button.innerHTML = label; }, 1800);
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(source);
      selection.removeAllRanges(); selection.addRange(range);
      document.getElementById('copy-status').textContent = 'Commands selected. Use your browser’s Copy command.';
      button.textContent = 'Selected — copy text';
      window.setTimeout(() => { button.innerHTML = label; }, 2400);
    }
  });
});

document.querySelectorAll('.mobile-nav a').forEach(link => link.addEventListener('click', () => {
  document.querySelector('.mobile-nav').open = false;
}));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    const menu = document.querySelector('.mobile-nav');
    if (menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
  }
});

// Motion never hides content: if scripting or an observer fails, the page is still complete.
if (!reducedMotion.matches && 'IntersectionObserver' in window) {
  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      entry.target.style.setProperty('--arrival-delay', `${Number(entry.target.dataset.delay || 0)}ms`);
      entry.target.classList.add('is-arriving');
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.1 });
  document.querySelectorAll('[data-enter], .tool-list > div').forEach((element, index) => {
    element.dataset.delay = String((index % 3) * 70);
    observer.observe(element);
  });
  document.querySelectorAll('.hero .tile').forEach((element, index) => {
    element.animate([{ opacity: 0.4, transform: 'translateY(24px)' }, { opacity: 1, transform: 'translateY(0)' }], { duration: 700, delay: index * 65, easing: 'cubic-bezier(.22,1,.36,1)' });
  });
}
