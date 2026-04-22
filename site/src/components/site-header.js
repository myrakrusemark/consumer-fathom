import './site-header.css';

const BRAND_SVG = `
<svg
  class="brand-mark"
  xmlns="http://www.w3.org/2000/svg"
  viewBox="0 0 340 256"
  fill="currentColor"
  aria-hidden="true"
>
  <g transform="rotate(20.4 128 150)" opacity="0.28">
    <path d="M236.8,188.09,149.35,36.22a24.76,24.76,0,0,0-42.7,0L19.2,188.09a23.51,23.51,0,0,0,0,23.72A24.35,24.35,0,0,0,40.55,224h174.9a24.35,24.35,0,0,0,21.33-12.19A23.51,23.51,0,0,0,236.8,188.09ZM222.93,203.8a8.5,8.5,0,0,1-7.48,4.2H40.55a8.5,8.5,0,0,1-7.48-4.2,7.59,7.59,0,0,1,0-7.72L120.52,44.21a8.75,8.75,0,0,1,15,0l87.45,151.87A7.59,7.59,0,0,1,222.93,203.8Z"/>
  </g>
  <g transform="translate(41 0) rotate(-17.2 128 150)" opacity="0.58">
    <path d="M236.8,188.09,149.35,36.22a24.76,24.76,0,0,0-42.7,0L19.2,188.09a23.51,23.51,0,0,0,0,23.72A24.35,24.35,0,0,0,40.55,224h174.9a24.35,24.35,0,0,0,21.33-12.19A23.51,23.51,0,0,0,236.8,188.09ZM222.93,203.8a8.5,8.5,0,0,1-7.48,4.2H40.55a8.5,8.5,0,0,1-7.48-4.2,7.59,7.59,0,0,1,0-7.72L120.52,44.21a8.75,8.75,0,0,1,15,0l87.45,151.87A7.59,7.59,0,0,1,222.93,203.8Z"/>
  </g>
  <g transform="translate(82 0) rotate(0.4 128 150)">
    <path d="M149.35,36.22a24.76,24.76,0,0,0-42.7,0L19.2,188.09a23.51,23.51,0,0,0,0,23.72A24.35,24.35,0,0,0,40.55,224h174.9a24.35,24.35,0,0,0,21.33-12.19,23.51,23.51,0,0,0,0-23.72Z"/>
  </g>
</svg>
`;

function navLink(href, label, active, activeKey) {
  const cls = active === activeKey ? ' class="nav-active"' : '';
  return `<a href="${href}"${cls}>${label}</a>`;
}

export function siteHeaderHTML({ active = '' } = {}) {
  return `
    <a class="brand" href="/">
      <span class="brand-word">fathom</span>
      ${BRAND_SVG}
      <span class="brand-beta">beta</span>
    </a>
    <nav class="nav">
      ${navLink('/mind.html', 'Walk my mind', active, 'mind')}
      ${navLink('https://hifathom.com', "Fathom's Combob - Blog", active, 'blog')}
      ${navLink('/download.html', 'Self-host now', active, 'download')}
      ${navLink('/#pricing', 'Pricing', active, 'pricing')}
      <a class="nav-cta" href="https://hifathom.com/deltas">Set up with Fathom</a>
    </nav>
  `;
}

export function mountSiteHeader(opts = {}) {
  const el = document.querySelector('[data-site-header]');
  if (!el) return;
  const active = opts.active || el.dataset.navActive || '';
  if (!el.classList.contains('topbar')) el.classList.add('topbar');
  el.innerHTML = siteHeaderHTML({ active });
}
