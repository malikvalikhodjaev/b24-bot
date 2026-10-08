"""A compact calendar in the Telegram keyboard; free date input still works."""
import calendar
from datetime import date, timedelta
from .i18n import tr
from .intake_ui import phrase

PREVIOUS_MONTH = '⬅️ Предыдущий месяц'
NEXT_MONTH = 'Следующий месяц ➡️'


def calendar_prompt(state, now, *, shift=0):
    from .service import CANCEL
    from .sales_intake import NEXT_DAY
    first = date.fromisoformat(state.get('calendar_month', now.date().replace(day=1).isoformat()))
    index = first.year * 12 + first.month - 1 + shift
    first = date(index // 12, index % 12 + 1, 1)
    minimum = now.date().replace(day=1)
    if first < minimum or (first.year - minimum.year) * 12 + first.month - minimum.month > 12:
        raise ValueError(tr('Выберите дату в ближайшие 12 месяцев.'))
    state['calendar_month'] = first.isoformat()
    state['calendar_options'] = {}
    rows = []
    for week in calendar.monthcalendar(first.year, first.month):
        row = []
        for day in week:
            chosen = date(first.year, first.month, day) if day else None
            valid = chosen is not None and chosen >= now.date()
            label = str(day) if valid else '·'
            row.append(label)
            if valid:
                state['calendar_options'][label] = chosen.isoformat()
        rows.append(row)
    text = phrase('reminder_date') + '\n\n<b>' + first.strftime('%m.%Y') + '</b>\n' + tr('Пн · Вт · Ср · Чт · Пт · Сб · Вс')
    rows += [[PREVIOUS_MONTH, NEXT_MONTH], [NEXT_DAY], [CANCEL]]
    state['step'] = 'sales_deadline'
    return text, rows, state


def time_prompt(state, now):
    from .service import CANCEL
    chosen = date.fromisoformat(state['reminder_date'])
    times = [f'{hour:02}:{minute:02}' for hour in range(8, 21) for minute in (0, 30)
             if chosen > now.date() or (hour, minute) > (now.hour, now.minute)]
    if not times:
        state.pop('reminder_date', None)
        return calendar_prompt(state, now)
    state['step'] = 'sales_time'
    state['reminder_times'] = times
    return phrase('reminder_time') + '\n📅 ' + chosen.strftime('%d.%m.%Y'), [times[i:i+4] for i in range(0, len(times), 4)] + [[CANCEL]], state
