function isCalendarDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000-')) return false;
  const date = new Date(value + 'T00:00:00Z');
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

function createDateCalendar() {
  const beijingDay = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  });
  const calendarDay = value => new Date(value + 'T00:00:00Z');
  const dateKey = date => date.toISOString().slice(0, 10);
  const dayStep = 86_400_000;
  const panel = el('div', 'date-calendar', `
    <div class="calendar-heading">
      <button type="button" data-month="-1" aria-label="上个月">${icon('right')}</button>
      <strong id="filterCalendarMonth" aria-live="polite"></strong>
      <button type="button" data-month="1" aria-label="下个月">${icon('right')}</button>
    </div>
    <div class="calendar-weekdays" aria-hidden="true">${'日一二三四五六'.split('').map(day => `<span>${day}</span>`).join('')}</div>
    <div class="calendar-days" role="group" aria-label="月历日期"></div>
    <form class="calendar-entry">
      <input type="text" spellcheck="false" autocomplete="off" maxlength="10" aria-label="输入日期" placeholder="YYYY-MM-DD" required>
      <button type="submit" class="btn sm">选择</button>
    </form>
    <div class="calendar-actions">
      <button type="button" data-calendar-action="today">今天</button>
      <button type="button" data-calendar-action="clear">清除</button>
    </div>`);
  panel.id = 'filterCalendar';
  panel.setAttribute('popover', 'auto');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-labelledby', 'filterCalendarMonth');
  document.body.append(panel);

  const title = panel.querySelector('#filterCalendarMonth');
  const days = panel.querySelector('.calendar-days');
  const entry = panel.querySelector('input');
  let active, focused, today;

  function paint(focusDay = false) {
    const first = new Date(focused);
    first.setUTCDate(1);
    const month = first.getUTCMonth();
    const current = dateKey(focused);
    const [start, end] = active.range();
    const weekStart = first.getTime() - first.getUTCDay() * dayStep;
    title.textContent = `${first.getUTCFullYear()} 年 ${month + 1} 月`;
    const lastDay = new Date(first);
    lastDay.setUTCMonth(month + 1, 0);
    const slots = Math.ceil((first.getUTCDay() + lastDay.getUTCDate()) / 7) * 7;
    days.innerHTML = Array.from({length: slots}, (_, index) => {
      const day = new Date(weekStart + index * dayStep);
      const key = dateKey(day);
      const selected = Boolean(key === start || key === end);
      return `<button type="button" data-day="${key}" aria-label="${key}"
        tabindex="${key === current ? '0' : '-1'}" aria-pressed="${selected}"
        ${key === today ? 'aria-current="date"' : ''}
        class="${day.getUTCMonth() === month ? '' : 'outside-month'}${start && end && key > start && key < end ? ' in-range' : ''}">${day.getUTCDate()}</button>`;
    }).join('');
    if (focusDay) days.querySelector('[tabindex="0"]')?.focus({preventScroll:true});
  }

  function position() {
    if (active) positionControlPopover(panel, active.trigger);
  }

  function moveMonth(offset, focusDay) {
    const day = focused.getUTCDate();
    focused.setUTCDate(1);
    focused.setUTCMonth(focused.getUTCMonth() + offset);
    const end = new Date(focused);
    end.setUTCMonth(end.getUTCMonth() + 1, 0);
    focused.setUTCDate(Math.min(day, end.getUTCDate()));
    paint(focusDay);
    position();
  }

  function close(value, apply = false) {
    const {trigger, commit} = active;
    panel.hidePopover();
    if (apply) commit(value);
    trigger.focus({preventScroll:true});
  }

  panel.addEventListener('beforetoggle', event => {
    if (event.newState === 'closed' && active) {
      active.trigger.setAttribute('aria-expanded', 'false');
      active = null;
    }
  });
  panel.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (button?.dataset.day) close(button.dataset.day, true);
    else if (button?.dataset.month) moveMonth(Number(button.dataset.month), false);
    else if (button?.dataset.calendarAction) close(button.dataset.calendarAction === 'today' ? today : '', true);
  });
  panel.querySelector('form').addEventListener('submit', event => {
    event.preventDefault();
    const value = entry.value.trim();
    if (isCalendarDate(value)) close(value, true);
    else {
      entry.setCustomValidity('请输入有效日期，格式为 YYYY-MM-DD');
      entry.reportValidity();
    }
  });
  entry.addEventListener('input', () => entry.setCustomValidity(''));

  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    keepFocusInside(event, panel);
    if (!event.target.matches('[data-day]')) return;
    const offset = {ArrowLeft:event.shiftKey ? -7 : -1, ArrowRight:event.shiftKey ? 7 : 1, ArrowUp:-7, ArrowDown:7,
      Home:-focused.getUTCDay(), End:6-focused.getUTCDay()}[event.key];
    if (offset !== undefined) {
      event.preventDefault();
      focused = new Date(focused.getTime() + offset * dayStep);
      paint(true);
      position();
    } else if (event.key === 'PageUp' || event.key === 'PageDown') {
      event.preventDefault();
      moveMonth((event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1), true);
    }
  });
  window.addEventListener('resize', position);
  window.addEventListener('scroll', position, true);

  return {
    open({trigger, value, other, range, commit}) {
      if (panel.matches(':popover-open') && active?.trigger === trigger) {
        close();
        return;
      }
      if (panel.matches(':popover-open')) panel.hidePopover();
      active = {trigger, range, commit};
      today = beijingDay.format(new Date());
      focused = calendarDay(value || other || today);
      entry.value = value;
      entry.setCustomValidity('');
      paint();
      panel.showPopover();
      trigger.setAttribute('aria-expanded', 'true');
      position();
      days.querySelector('[tabindex="0"]')?.focus({preventScroll:true});
    },
  };
}
