"""Persistent registration notifications and administrator-only inline decisions."""
import html
import json
import re
import time

from .api import RemoteError
from .i18n import language_context, stored_language, tr
from .role_content import TEXTS


def phrase(key):
    return tr(TEXTS[key]['ru'])


class RegistrationNotices:
    def __init__(self, registration):
        self.registration = registration
        self.db = registration.store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS registration_reviews(
                id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
                bitrix_id INTEGER NOT NULL, name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS registration_review_user ON registration_reviews(telegram_id,id);
            CREATE TABLE IF NOT EXISTS registration_notices(
                review_id INTEGER NOT NULL, recipient INTEGER NOT NULL, event TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                retry_at INTEGER NOT NULL DEFAULT 0, last_error TEXT, message_id INTEGER,
                PRIMARY KEY(review_id,recipient,event));
        ''')

    def latest(self, target):
        row = self.db.execute('SELECT * FROM registration_reviews WHERE telegram_id=? ORDER BY id DESC LIMIT 1',
                              (target,)).fetchone()
        return dict(row) if row else None

    def review(self, target, *, fresh=False):
        current = self.latest(target)
        row = self.registration.store.registration(target)
        if not row:
            return None
        if fresh or not current:
            with self.db:
                ident = self.db.execute('INSERT INTO registration_reviews(telegram_id,bitrix_id,name) VALUES(?,?,?)',
                                        (target,row['bitrix_id'],row['name'])).lastrowid
            current = dict(self.db.execute('SELECT * FROM registration_reviews WHERE id=?',(ident,)).fetchone())
        return current

    def queue(self, review, recipient, event):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO registration_notices(review_id,recipient,event) VALUES(?,?,?)',
                            (review['id'],recipient,event))

    def submitted(self, target):
        review = self.review(target, fresh=True)
        for admin in self.registration.settings.admins:
            self.queue(review, admin, 'request')

    def decision(self, target, event):
        self.queue(self.review(target), target, event)

    def recover(self):
        """Existing pending requests and decisions survive restarts without re-sending delivered cards."""
        for row in self.registration.store.registrations('pending'):
            review = self.review(row['telegram_id'])
            for admin in self.registration.settings.admins:
                self.queue(review, admin, 'request')
        rows = self.db.execute('''SELECT r.* FROM registrations r JOIN registration_reviews v
            ON v.telegram_id=r.telegram_id AND v.id=(SELECT MAX(id) FROM registration_reviews WHERE telegram_id=r.telegram_id)
            WHERE r.status IN ('approved','rejected','revoked') AND r.bitrix_id=v.bitrix_id''').fetchall()
        for row in rows:
            self.decision(row['telegram_id'],row['status'])

    def card(self, review, *, choose_role=False):
        from .work_inbox import ROLES
        target = review['telegram_id']
        row = self.registration.store.registration(target)
        identity = self.db.execute('SELECT value FROM settings WHERE key=?',('identity:'+str(target),)).fetchone()
        identity = json.loads(identity[0]) if identity else {}
        telegram_name = ' '.join(str(identity.get(key) or '') for key in ('first_name','last_name')).strip()
        text = phrase('registration_heading') + '\n\n' + html.escape(review['name'][:120])
        text += '\n' + phrase('registration_employee') + str(review['bitrix_id'])
        if telegram_name:
            text += '\nTelegram: ' + html.escape(telegram_name[:128])
        text += '\nTelegram ID: <code>' + str(target) + '</code>'
        if row['status'] != 'pending':
            text += '\n\n' + phrase('registration_status_'+row['status'])
            return text, []
        ident = str(review['id'])
        if choose_role and self.registration.work_inbox:
            text += '\n\n' + phrase('choose_role')
            buttons = [[{'text':phrase('sales' if role=='fom_sales' else role),
                         'callback_data':'reg:approve:'+ident+':'+role}] for role in ROLES]
            buttons += [[{'text':phrase('registration_reject'),'callback_data':'reg:reject:'+ident},
                         {'text':phrase('registration_back'),'callback_data':'reg:card:'+ident}]]
        else:
            text += '\n\n' + phrase('registration_admin_hint')
            buttons = [[{'text':phrase('registration_accept'),'callback_data':'reg:choose:'+ident},
                        {'text':phrase('registration_reject'),'callback_data':'reg:reject:'+ident}]]
        return text, buttons

    def flush(self, *, now=None, limit=5):
        now = int(time.time()) if now is None else now
        rows = self.db.execute('''SELECT n.*,r.telegram_id,r.bitrix_id,r.name FROM registration_notices n
            JOIN registration_reviews r ON r.id=n.review_id WHERE n.state='pending' AND n.retry_at<=?
            ORDER BY n.review_id,n.recipient,n.event LIMIT ?''',(now,limit)).fetchall()
        for notice in rows:
            review = {'id':notice['review_id'],'telegram_id':notice['telegram_id'],
                      'bitrix_id':notice['bitrix_id'],'name':notice['name']}
            recipient, event = notice['recipient'],notice['event']
            row = self.registration.store.registration(review['telegram_id'])
            expected = 'pending' if event=='request' else event
            valid = row and row['status']==expected and self.latest(review['telegram_id'])['id']==review['id']
            if event=='request':
                valid = valid and recipient in self.registration.settings.admins and self.registration.user(recipient)
            key = (review['id'],recipient,event)
            if not valid:
                with self.db:
                    self.db.execute("UPDATE registration_notices SET state='cancelled' WHERE review_id=? AND recipient=? AND event=?",key)
                continue
            with language_context(stored_language(self.registration.store,recipient)):
                if event=='request':
                    text, buttons = self.card(review)
                else:
                    text = phrase('registration_result_'+event)
                    if event=='approved' and self.registration.work_inbox:
                        role = self.registration.work_inbox.store.role(recipient)
                        if role:
                            text += '\n' + phrase('role') + phrase('sales' if role=='fom_sales' else role)
                    buttons = None
                params = {'chat_id':recipient,'text':text,'parse_mode':'HTML',
                          'link_preview_options':{'is_disabled':True}}
                if buttons is not None:
                    params['reply_markup'] = {'inline_keyboard':buttons}
                elif event=='approved':
                    params['reply_markup'] = {'keyboard':self.registration.keyboard(recipient),
                                              'resize_keyboard':True,'is_persistent':True}
                try:
                    result = self.registration.telegram.call('sendMessage',params)
                except RemoteError as exc:
                    if exc.code=='TELEGRAM_401':
                        raise
                    attempts = notice['attempts']+1
                    with self.db:
                        self.db.execute('''UPDATE registration_notices SET attempts=?,last_error=?,retry_at=?,state=?
                            WHERE review_id=? AND recipient=? AND event=?''',
                            (attempts,exc.code,now+min(300,30*2**min(attempts-1,3)),
                             'blocked' if exc.code in {'TELEGRAM_400','TELEGRAM_403'} else 'pending',*key))
                    continue
                ident = result.get('message_id') if isinstance(result,dict) else None
                with self.db:
                    self.db.execute('''UPDATE registration_notices SET state='delivered',attempts=attempts+1,
                        last_error=NULL,message_id=? WHERE review_id=? AND recipient=? AND event=?''',(ident,*key))

    def handle_callback(self, update):
        callback = update.get('callback_query')
        data = callback.get('data') if isinstance(callback,dict) else None
        if not isinstance(data,str) or not data.startswith('reg:'):
            return False
        sender, message = callback.get('from',{}),callback.get('message',{})
        user, chat = sender.get('id'),message.get('chat',{})
        allowed = (isinstance(user,int) and not sender.get('is_bot') and chat.get('type')=='private'
                   and chat.get('id')==user and isinstance(message.get('message_id'),int)
                   and message['message_id']>0 and user in self.registration.settings.admins and self.registration.user(user))
        match = re.fullmatch(r'reg:(card|choose|approve|reject):([1-9]\d{0,17})(?::(fom_sales|tech|trainer|dispatcher))?',data)
        error = 'registration_admin_only' if not allowed else 'registration_invalid'
        review = None
        if allowed and match:
            review = self.db.execute('SELECT * FROM registration_reviews WHERE id=?',(int(match[2]),)).fetchone()
            if review:
                review = dict(review)
                row = self.registration.store.registration(review['telegram_id'])
                if (row and row['status']=='pending' and row['bitrix_id']==review['bitrix_id'] and
                        self.latest(review['telegram_id'])['id']==review['id'] and review['telegram_id'] not in self.registration.settings.admins):
                    error = None
                else:
                    error = 'registration_processed'
        self.registration.telegram.call('answerCallbackQuery',{'callback_query_id':callback['id'],
            'text':phrase(error or 'registration_processing')[:180], 'show_alert':bool(error)})
        if error:
            return True
        action, role = match[1],match[3]
        if action=='choose' and not self.registration.work_inbox:
            action = 'approve'
        if action in {'card','choose'} and role is None:
            text, buttons = self.card(review,choose_role=action=='choose')
        elif action=='approve' and (role or not self.registration.work_inbox):
            command = '/approve '+str(review['telegram_id'])+(' '+role if role else '')
            try:
                text, _ = self.registration.admin_route(user,'/approve',command)
            except RemoteError:
                text, buttons = phrase('registration_crm_unavailable'),self.card(review,choose_role=True)[1]
            else:
                _, buttons = self.card(review,choose_role=True)
        elif action=='reject' and role is None:
            text, _ = self.registration.admin_route(user,'/reject','/reject '+str(review['telegram_id']))
            _, buttons = self.card(review)
        else:
            text, buttons = phrase('registration_invalid'),self.card(review,choose_role=True)[1]
        if not buttons:
            text = self.card(review)[0]
        params = {'chat_id':user,'message_id':message['message_id'],'text':text,'parse_mode':'HTML',
                  'reply_markup':{'inline_keyboard':buttons},'link_preview_options':{'is_disabled':True}}
        try:
            self.registration.telegram.call('editMessageText',params)
        except RemoteError as exc:
            if exc.code=='TELEGRAM_400':
                # Repeated clicks can edit an already identical card.
                return True
            raise
        return True
