"""Personal executor statistics, scoped by request creation date in Tashkent."""
from datetime import datetime
import html
import re
from .api import RemoteError
from .i18n import tr
from .personal_statistics import PERIODS, phrase, statistics_interval
from .role_content import TEXTS


def show(work, user, period='month', message_id=None, *, now=None, message=None):
    from .work_inbox import duration, InboxError
    if not work.registration.support_statistics_allowed(user):
        raise InboxError(tr(TEXTS['support_only']['ru']))
    start, now = statistics_interval(period, now or datetime.now(work.live.crm.config.timezone))
    stats = work.store.stats(user, start.isoformat() if start else None, now.isoformat())
    title = phrase(period) + (f' · {start:%d.%m}–{now:%d.%m.%Y}' if start else '')
    text = tr(TEXTS['support_heading']['ru']) + '\n' + title + '\n\n' + tr(TEXTS['support_scope']['ru']) + '\n\n'
    for key in ('assigned','working','waiting','closed'):
        text += tr(TEXTS[key]['ru']) + str(stats[key]) + '\n'
    if stats['resolution_seconds'] is not None:
        text += '\n' + tr(TEXTS['resolution']['ru']) + duration(round(stats['resolution_seconds'])) + '\n'
    text += '\n' + phrase('updated').format(period=phrase('period_'+period),time=now.strftime('%d.%m · %H:%M'))
    buttons = [{'text':('✓ ' if key==period else '')+phrase(key),'callback_data':'wr:stats:'+key} for key in PERIODS]
    payload={'chat_id':user,'text':text,'parse_mode':'HTML','reply_markup':{'inline_keyboard':[
        buttons[:2],buttons[2:],[{'text':phrase('refresh'),'callback_data':'wr:stats:'+period}]]}}
    plain=html.unescape(re.sub(r'</?[^>]+>','',text))
    if message and message.get('text') in {text,plain} and message.get('reply_markup')==payload['reply_markup']:
        return
    if isinstance(message_id,int) and message_id>0:
        payload['message_id']=message_id
        try:
            return work.telegram.call('editMessageText',payload)
        except RemoteError as exc:
            if exc.code!='TELEGRAM_400':
                raise
            payload.pop('message_id')
    return work.telegram.call('sendMessage',payload)
