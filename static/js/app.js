'use strict';
const menu = document.querySelector('.menu-button');
const sidebar = document.querySelector('.sidebar');
if (menu && sidebar) {
  const closeMenu = () => {
    document.body.classList.remove('menu-open');
    menu.setAttribute('aria-expanded', 'false');
    menu.setAttribute('aria-label', '開啟導覽選單');
  };
  menu.addEventListener('click', () => {
    const open = !document.body.classList.contains('menu-open');
    document.body.classList.toggle('menu-open', open);
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', open ? '關閉導覽選單' : '開啟導覽選單');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('menu-open')) {
      closeMenu(); menu.focus();
    }
  });
  document.addEventListener('click', (event) => {
    if (!sidebar.contains(event.target) && !menu.contains(event.target)) closeMenu();
  });
  window.matchMedia('(min-width: 901px)').addEventListener('change', closeMenu);
}
