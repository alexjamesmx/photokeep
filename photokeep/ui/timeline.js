'use strict';
/* Date scrubber on the right edge, like Google Photos: years sit where they are in the scroll,
   a marker shows the current position with a date bubble, and click or drag jumps. */
function createTimeline(scroller, rail) {
  const PAD = 14;
  const ticks = document.createElement('div');
  const thumb = document.createElement('div');
  const bubble = document.createElement('div');
  ticks.className = 'ticks';
  thumb.className = 'thumb';
  bubble.className = 'bubble';
  rail.replaceChildren(ticks, thumb, bubble);

  let sections = [];       // [{node, label, year}] in scroll order
  let dragging = false, hovering = false, hideTimer = null, queued = false;

  const maxScroll = () => Math.max(1, scroller.scrollHeight - scroller.clientHeight);
  const span = () => Math.max(1, rail.clientHeight - PAD * 2);
  const toY = top => PAD + Math.min(1, top / maxScroll()) * span();
  const toTop = y => Math.max(0, Math.min(1, (y - PAD) / span())) * maxScroll();
  function sectionAt(top) {
    let lo = 0, hi = sections.length - 1, hit = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (sections[mid].node.offsetTop <= top + 2) { hit = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return sections[hit];
  }
  function show(y, label) {
    bubble.textContent = label;
    bubble.style.top = `${y}px`;
    rail.classList.add('active');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => { if (!hovering && !dragging) rail.classList.remove('active'); }, 1000);
  }
  function sync() {
    queued = false;
    if (!sections.length) return;
    const y = toY(scroller.scrollTop);
    thumb.style.top = `${y}px`;
    if (!hovering || dragging) show(y, sectionAt(scroller.scrollTop).label);
  }
  function layout() {
    const els = [];
    let lastYear = null, lastY = -99;
    for (const s of sections) {
      const y = toY(Math.min(s.node.offsetTop, maxScroll()));
      els.push(Object.assign(document.createElement('i'), { style: `top:${y}px` }));
      // Years closer than a label's height would overlap, so only the first is drawn.
      if (s.year !== lastYear && y - lastY >= 16) {
        const label = Object.assign(document.createElement('span'), { textContent: s.year, style: `top:${y}px` });
        label.dataset.top = s.node.offsetTop;
        els.push(label);
        lastY = y;
      }
      lastYear = s.year;
    }
    ticks.replaceChildren(...els);
    sync();
  }
  const yOf = e => e.clientY - rail.getBoundingClientRect().top;

  scroller.addEventListener('scroll', () => { if (!queued) { queued = true; requestAnimationFrame(sync); } }, { passive: true });
  rail.addEventListener('pointerdown', e => {
    if (e.target.dataset.top) { scroller.scrollTop = +e.target.dataset.top; return; }
    dragging = true;
    rail.setPointerCapture(e.pointerId);
    scroller.scrollTop = toTop(yOf(e));
  });
  rail.addEventListener('pointermove', e => {
    hovering = true;
    const top = toTop(yOf(e));
    if (dragging) scroller.scrollTop = top;
    else show(yOf(e), sectionAt(top).label);
  });
  rail.addEventListener('pointerup', () => { dragging = false; });
  rail.addEventListener('pointerleave', () => { hovering = false; if (!dragging) sync(); });

  return {
    /* sections: [{node, label, year}], or [] to hide the rail and bring back the native scrollbar. */
    set(list) {
      sections = list;
      rail.hidden = !list.length;
      scroller.classList.toggle('rail-on', list.length > 0);
      if (list.length) requestAnimationFrame(layout);
    },
  };
}
