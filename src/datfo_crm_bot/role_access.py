"""Reject old sales actions after a role change, before any draft replay or CRM call."""
import json
from .i18n import tr
from .role_content import TEXTS


class RoleAccess:
    def __init__(self, registration, telegram):
        self.registration, self.telegram = registration, telegram

    def handle(self, update):
        registration = self.registration
        if not registration.work_inbox:
            return False
        callback = update.get('callback_query')
        message = callback.get('message', {}) if isinstance(callback, dict) else update.get('message', {})
        sender = callback.get('from', {}) if isinstance(callback, dict) else message.get('from', {})
        user = sender.get('id')
        if not user or sender.get('is_bot') or message.get('chat', {}).get('type') != 'private' or message['chat'].get('id') != user or not registration.user(user):
            return False
        from .service import DEAL_LABELS, SUPPORT_LABELS, PHARMACY, STATS
        from .okb_service import OKB_LABELS, CONTINUE
        from .work_inbox import NEW, STATS as SUPPORT_STATS
        from .personal_statistics import MY_STATS
        from .live_deals import MY_CRM
        from .navigation import MORE
        text = str(message.get('text') or '').strip() if not callback else ''
        command = text.split(maxsplit=1)[0].split('@',1)[0].lower() if text.startswith('/') else text
        sales_actions = {'/deal','/support','/request','/okb','/pharmacy','/stats','/my_stats','/crm_deal',
                         *DEAL_LABELS,*SUPPORT_LABELS,*OKB_LABELS,PHARMACY,STATS,MY_STATS,NEW,CONTINUE}
        data = str(callback.get('data') or '') if callback else ''
        sales = registration.sales_allowed(user)
        denied_sales = not sales and (command in sales_actions or data.startswith('personal_stats:') or
            (data.startswith('ld:') and not data.startswith(('ld:list:', 'ld:hub'))))
        web = message.get('web_app_data')
        if not sales and isinstance(web,dict) and isinstance(web.get('data'),str):
            try:
                parsed = json.loads(web['data'])
                denied_sales |= isinstance(parsed,dict) and parsed.get('mode') in {'deal','support','pharmacy'}
            except ValueError:
                pass
        state = registration.store.session(user)
        if not sales and command in {'/pending','Проверить сохранение'}:
            denied_sales |= bool(registration.store.unfinished(user))
        if not sales and state and state.get('step') not in {'done','stats_scope','stats_period'} and not callback and command not in {
                '/start','/menu','/profile','/settings','/language','/help','/guide','/next','/cancel','/request_cancel',
                '/my_crm','/requests','/requests_mine','/requests_sent','/request_ticket','/requests_stats','/reminders',
                '/approve','/reject','/revoke','/registrations','/members','/inbox_role','/inbox_team'}:
            # Viewing other role sections never resumes an old sales form.
            denied_sales |= bool(text and not command.startswith('/') and command not in {
                '👤 Мой профиль','📂 Мои дела',MY_CRM,MORE,'🏠 Главное меню',SUPPORT_STATS,
                '📥 Техник ёрдам заявкалари','📌 Менга тайинланган','📤 Юборган заявкаларим','🔔 Мои напоминания'})
        denied_support = (command in {'/requests_stats', SUPPORT_STATS} or data.startswith('wr:stats:')) and not registration.support_statistics_allowed(user)
        if not denied_sales and not denied_support:
            return False
        reason = tr(TEXTS['sales_only' if denied_sales else 'support_only']['ru'])
        if callback:
            self.telegram.call('answerCallbackQuery', {'callback_query_id':callback['id'],'text':reason[:180], 'show_alert':True})
        else:
            self.telegram.send(user, reason, registration.keyboard(user))
        return True
