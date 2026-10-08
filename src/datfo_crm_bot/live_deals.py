"""Real Bitrix deals, persistent links and explicit stage changes."""
from __future__ import annotations

from .i18n import tr

from dataclasses import replace
import json
import re
from uuid import uuid4

from .api import RemoteError
from .activity_identity import SOURCE
from .config import ConfigError, Target
from .crm import Crm
from .guidance import with_guide
from .service import Bot, CANCEL, CHECK, CONFIRM, DEAL, SUPPORT, DEAL_LABELS, SUPPORT_LABELS, STATS, esc, utc_text
from .sales_options import DISCUSSION_FIELD, discussion_choices, selected_programs

SALES_CATEGORY = 45
RESTART_DEAL = 'Начать новую форму сделки'
REAL_TEST = '🧪 Б24га ёзиб синаш'
END_TEST = '🏁 Б24 синовидан чиқиш'
MY_CRM = '📂 Мои сделки и заявки'
MY_CRM_LABELS = {MY_CRM, '📂 Мои заявки', '📂 Мои дела', '📂 Б24 ёзувларим', '📂 Б24 заявкаларим/сделкаларимни кўриш'}
TEST_PREFIX = '[ТЕСТ БОТА] '
MENU = with_guide([[DEAL, SUPPORT], [MY_CRM]])
FIELDS = {'title':'title', 'company_id':'companyId', 'responsible_id':'assignedById',
          'request_id':'originId', 'description':'comments'}


class LiveCrm(Crm):
    def sales_destination(self):
        category = next((row for row in self.categories() if row['id'] == SALES_CATEGORY), None)
        if not category:
            raise ConfigError(tr('Воронка продаж KG недоступна. Создание сделки не начато.'))
        stages = [row for row in self.stages(SALES_CATEGORY)
                  if row['title'].strip().casefold() == 'план' and row['semantics'] not in {'S', 'F'}]
        if len(stages) != 1:
            raise ConfigError(tr('В воронке продаж KG не найдена единственная стадия «План».'))
        return category, stages[0]

    def categories(self):
        rows = self.api.list_all('crm.category.list', {'entityTypeId':2}, key='categories')
        result = [{'id':int(row['id']), 'title':str(row['name']), 'sort':int(row.get('sort') or 0)} for row in rows]
        return sorted(result, key=lambda row:(not row['title'].upper().startswith('DATFO'),row['sort'],row['id']))

    def stages(self, category):
        entity = 'DEAL_STAGE' if category == 0 else 'DEAL_STAGE_' + str(category)
        rows = self.api.list_all('crm.status.list', {'filter':{'ENTITY_ID':entity}, 'order':{'SORT':'ASC'}})
        result = []
        for row in rows:
            if str(row.get('ENTITY_ID')) != entity:
                raise RemoteError('INVALID_STAGE_CATEGORY')
            result.append({'id':str(row['STATUS_ID']), 'title':str(row['NAME']),
                           'sort':int(row.get('SORT') or 0), 'semantics':row.get('SEMANTICS')})
        if not result or len({row['id'] for row in result}) != len(result):
            raise RemoteError('INVALID_STAGES')
        return sorted(result, key=lambda row:row['sort'])

    def item(self, ident):
        result = self.api.call('crm.item.get', {'entityTypeId':2,'id':ident,'useOriginalUfNames':'Y'})['result']
        row = result.get('item') if isinstance(result,dict) else None
        if not isinstance(row,dict) or str(row.get('id')) != str(ident):
            raise RemoteError('INVALID_DEAL_RESPONSE')
        return row

    def find_request(self, kind, request_id):
        # A human may move a created deal to another pipeline before recovery.
        try:
            rows = self.api.list_all('crm.item.list', {'entityTypeId':2,'useOriginalUfNames':'Y',
                'filter':{'originId':request_id,'originatorId':SOURCE}, 'select':['*']}, key='items')
        except RemoteError as exc:
            if exc.code!='INVALID_LIST':
                raise
            # Some portals return empty arrays for selected universal fields. Recover
            # by the exact same external origin through the classic deal list.
            native = self.api.list_all('crm.deal.list',{'filter':{'ORIGIN_ID':request_id,'ORIGINATOR_ID':SOURCE},
                'select':['ID'],'order':{'ID':'ASC'}})
            rows = [{'id':row['ID']} for row in native]
        if len(rows) > 1:
            raise RemoteError('DUPLICATE_REQUEST_ID')
        if not rows:
            return None
        row = self.item(int(rows[0].get('id',rows[0].get('ID'))))
        if row.get('originId') != request_id or row.get('originatorId') != SOURCE:
            raise RemoteError('DEAL_SOURCE_MISMATCH')
        return self.result(self.config.targets[kind],row)

    def create(self, state, bitrix_user_id, telegram_user_id, *, on_submit=None):
        if state['kind'] == 'deal' and not state.get('sales_intake'):
            raise ConfigError(tr('Форма сделки обновлена: теперь только продажи KG / «План», с аптекой и делом менеджеру. Старый черновик не отправлен; начните новую форму.'))
        if state.get('sales_intake'):
            if state.get('discussion_programs'):
                selected_programs([row['id'] for row in state['discussion_programs']], discussion_choices(self.api))
            category, stage = self.sales_destination()
            if (state['category_id'], state['initial_stage']) != (category['id'], stage['id']):
                raise ConfigError(tr('Воронка или стадия продаж изменилась. Проверьте форму заново.'))
        if not any(row['id'] == state['category_id'] for row in self.categories()):
            raise ConfigError(tr('Выбранная воронка больше не доступна'))
        if not any(row['id'] == state['initial_stage'] for row in self.stages(state['category_id'])):
            raise ConfigError(tr('Начальная стадия изменилась; выберите её заново'))
        return super().create(state,bitrix_user_id,telegram_user_id,on_submit=on_submit)

    def update_stage(self, ident, stage):
        fields = self.api.call('crm.item.fields',{'entityTypeId':2,'useOriginalUfNames':'Y'})['result'].get('fields',{})
        meta = fields.get('stageId',{})
        if not meta or meta.get('isReadOnly') or meta.get('isImmutable'):
            raise ConfigError(tr('Стадия недоступна для изменения'))
        return self.api.call('crm.item.update',{'entityTypeId':2,'id':ident,'useOriginalUfNames':'Y','fields':{'stageId':stage}})


def configure(config, state):
    fields = {**FIELDS, 'pharmacy_id': 'parentId1034'} if state.get('sales_intake') else FIELDS
    defaults = {'originatorId':SOURCE,'stageId':state['initial_stage']}
    if state.get('discussion_programs'):
        defaults[DISCUSSION_FIELD] = [row['id'] for row in state['discussion_programs']]
    target = Target(2,state['category_id'],None,fields,defaults)
    return replace(config,targets={**config.targets,state['kind']:target})


class LiveBot(Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .sales_intake import SalesIntake
        self.sales = SalesIntake(self)

    def _configure(self, state):
        self.config = configure(self.config,state)
        self.crm.config = self.config
        self.crm.validated.discard(state['kind'])

    def route_location(self, user_id, location, state):
        if state and state.get('step') == 'sales_okb':
            child = state['pharmacy_form']
            response, keyboard, child = self.sales.form.route_location(user_id, location, child)
            state['pharmacy_form'] = child
            if child.get('collected'):
                state.update(title='План: '+child['title'], company=child.get('company'), inn=child['inn'],
                             address=child['address'], phone=child['phone'])
                return self.sales.description_prompt(state)
            return response, keyboard, state
        return super().route_location(user_id, location, state)

    def route_payload(self, user_id, data, state):
        if self.store.unfinished(user_id):
            raise ValueError(tr('Сначала проверьте сохранение через /pending.'))
        if state and state.get('sales_intake'):
            return self.sales.route_payload(user_id, data, state)
        return super().route_payload(user_id, data, state)

    def test_mode(self, user):
        row = self.store.db.execute('SELECT value FROM settings WHERE key=?',('b24-test:'+str(user),)).fetchone()
        return bool(row and row[0] == '1')

    @staticmethod
    def choices(title, rows, state):
        text = title + '\n\n' + '\n'.join(f"{number}. {esc(row['title'][:150])}" for number,row in enumerate(rows,1))
        return text,[[str(number)] for number in range(1,len(rows)+1)]+[[CANCEL]],state

    def route(self, user_id, text, state):
        command = text.split(maxsplit=1)[0].split('@',1)[0].lower() if text.startswith('/') else text
        command = DEAL if command in DEAL_LABELS else SUPPORT if command in SUPPORT_LABELS else command
        command = MY_CRM if command in MY_CRM_LABELS else command
        command = MY_CRM if command in MY_CRM_LABELS else command
        if text == RESTART_DEAL:
            command = '/deal'
        pending = self.store.unfinished(user_id)
        if pending:
            operations = [self.store.operation(row['request_id']) for row in pending]
            operation = next((row for row in operations if row['value'].get('sales_intake')), operations[0])
            if operation['value'].get('workflow') == 'live_deal':
                self._configure(operation['value'])
                if operation['value'].get('sales_intake'):
                    return self.sales.pending(user_id, text, operation['value'])
                return super().route(user_id,text,state)
            return tr('Олдинги сақлашни /pending орқали текширинг.'),[[CHECK]],state
        legacy_deal = state and state.get('workflow') == 'live_deal' and state.get('kind') == 'deal' and not state.get('sales_intake') and state.get('step') != 'done'
        if legacy_deal and command not in {'/cancel', CANCEL} and text != RESTART_DEAL:
            return tr('Форма сделки обновлена: теперь только продажи KG / «План», с аптекой и делом менеджеру. Старый черновик не отправлен; начните новую форму.'), [[RESTART_DEAL, CANCEL]], state
        if command in {'/deal','/support',DEAL,SUPPORT}:
            if state and state.get('step') not in {'done','stats_scope','stats_period'} and not (legacy_deal and text == RESTART_DEAL):
                return tr('Аввал жорий қораламани тугатинг ёки «Отмена»ни босинг.'),[[CANCEL]],state
            kind = 'deal' if command in {'/deal',DEAL} else 'support'
            if kind == 'deal':
                category, stage = self.crm.sales_destination()
                state = {'workflow':'live_deal','kind':'deal','sales_intake':True,
                         'telegram_user':user_id,
                         'request_id':'datfo-live-'+uuid4().hex, 'live_test':self.test_mode(user_id),
                         'category_id':category['id'],'category_name':category['title'],
                         'initial_stage':stage['id'],'initial_stage_name':stage['title']}
                self._configure(state)
                return self.sales.begin(state)
            state = {'workflow':'live_deal','kind':kind,'request_id':'datfo-live-'+uuid4().hex,
                     'step':'live_category','live_test':self.test_mode(user_id),'category_candidates':self.crm.categories()}
            if not state['category_candidates']:
                raise ConfigError(tr('Нет доступных воронок'))
            notice = tr('🧪 Б24 СИНОВИ: ҳақиқий ёзув, номи «[ТЕСТ БОТА]» билан бошланади.\n') if state['live_test'] else tr('📌 Б24га ҳақиқий ёзув қўшилади.\n')
            return self.choices(notice+tr('Воронкани танланг:'),state['category_candidates'],state)
        if command in {'/cancel',CANCEL}:
            from .navigation import menu_text
            return menu_text(getattr(self, 'registration', None), user_id, cancelled=True),MENU,None
        if state and state.get('workflow') == 'live_deal':
            if state.get('sales_intake'):
                self._configure(state)
                return self.sales.route(user_id, text, state)
            if state['step'] == 'live_category':
                row = self.pick(text,state['category_candidates'])
                stages = self.crm.stages(row['id'])
                candidates = [item for item in stages if item['semantics'] not in {'S','F'}]
                if not candidates:
                    raise ConfigError(tr('Нет начальных рабочих стадий'))
                state.update(category_id=row['id'],category_name=row['title'],step='live_initial_stage',stage_candidates=candidates)
                return self.choices(tr('🧭 Бошланғич босқични танланг:'),candidates,state)
            if state['step'] == 'live_initial_stage':
                row = self.pick(text,state['stage_candidates'])
                state.update(initial_stage=row['id'],initial_stage_name=row['title'],step='title')
                return tr('📝 Сделка номини ёзинг.') if state['kind']=='deal' else tr('🛠 Сделка мавзусини ёзинг.'),[[CANCEL]],state
            self._configure(state)
            response,keyboard,after = super().route(user_id,text,state)
            if after and after.get('live_test') and after.get('title') and not after['title'].startswith(TEST_PREFIX):
                after['title'] = TEST_PREFIX + after['title']
            return response,keyboard,after
        return tr('Б24да сделка яратинг ёки ўз ёзувларингизни очинг.'),MENU,state

    @staticmethod
    def pick(text, rows):
        if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= len(rows):
            raise ValueError(tr('Рўйхатдан рақамни танланг.'))
        return rows[int(text)-1]

    def preview(self, user_id, state):
        text,keyboard,state = super().preview(user_id,state)
        text += tr('\nВоронка: ') + esc(state['category_name']) + tr('\nБосқич: ') + esc(state['initial_stage_name'])
        if state.get('live_test'):
            text += tr('\n🧪 Ҳақиқий синов ёзуви: Б24да кўринади.')
        return text,keyboard,state

    @staticmethod
    def success(state,result):
        from .intake_success import saved_deal
        state['step'] = 'done'
        return saved_deal(state, result), MENU, state


class StageStore:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS crm_stage_choices(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL,
                request_id TEXT NOT NULL,value TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS crm_stage_intents(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL,
                request_id TEXT NOT NULL,value TEXT NOT NULL,status TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,error TEXT);
        ''')
        self.db.commit()

    def operation_for(self, ident, user, administrator=False):
        rows = list(self.db.execute("SELECT request_id,user_id FROM operations WHERE kind IN ('deal','support') AND result IS NOT NULL AND json_extract(result,'$.id')=?",(ident,)))
        if len(rows) != 1 or (rows[0]['user_id'] != user and not administrator):
            raise ConfigError(tr('Ёзув ботда сизга боғланмаган'))
        operation = self.store.operation(rows[0]['request_id'])
        if operation['value'].get('workflow') != 'live_deal':
            raise ConfigError(tr('Бу ёзув учун Б24 бошқаруви уланмаган'))
        return operation

    def choice(self, user, operation, item, stages):
        token = uuid4().hex[:20]
        value = {'item':item,'stages':stages}
        with self.db:
            self.db.execute('INSERT INTO crm_stage_choices(token,user_id,request_id,value) VALUES(?,?,?,?)',
                            (token,user,operation['request_id'],json.dumps(value,ensure_ascii=False)))
        return token

    def row(self, table, token, user):
        if table not in {'crm_stage_choices','crm_stage_intents'}:
            raise ValueError('Unknown stage table')
        row = self.db.execute('SELECT * FROM '+table+' WHERE token=? AND user_id=?',(token,user)).fetchone()
        if not row:
            raise ConfigError(tr('Бу тугма сизга тегишли эмас. Ёзувни қайта очинг.'))
        return {**dict(row),'value':json.loads(row['value'])}

    def intent(self, choice, index):
        if not 0 <= index < len(choice['value']['stages']):
            raise ConfigError(tr('Нотўғри босқич рақами'))
        token = choice['token']+'-'+str(index)
        value = {'item':choice['value']['item'],'target':choice['value']['stages'][index]}
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO crm_stage_intents(token,user_id,request_id,value,status) VALUES(?,?,?,?,'prepared')",
                            (token,choice['user_id'],choice['request_id'],json.dumps(value,ensure_ascii=False)))
        return self.row('crm_stage_intents',token,choice['user_id'])

    def status(self, token, status, error=None):
        with self.db:
            self.db.execute('UPDATE crm_stage_intents SET status=?,error=? WHERE token=?',(status,error,token))

    def claim(self, token):
        with self.db:
            return self.db.execute("UPDATE crm_stage_intents SET status='sending' WHERE token=? AND status='prepared'",(token,)).rowcount == 1


class LiveDeals:
    def __init__(self, registration, config, api, telegram, simulation=None, inbox=None):
        self.registration, self.telegram, self.simulation, self.inbox = registration,telegram,simulation,inbox
        self.crm = LiveCrm(config,api)
        self.bot = LiveBot(config,registration.store,self.crm,telegram)
        self.bot.registration = registration
        self.stages = StageStore(registration.store)
        registration.live_deals_enabled = True
        self.work_inbox = None

    def item_for(self, operation, member, user):
        if operation['user_id'] != user and not member.admin:
            raise ConfigError(tr('Сизга тегишли ёзувни танланг'))
        row = self.crm.item(operation['result']['id'])
        if row.get('originId') != operation['request_id'] or row.get('originatorId') != SOURCE:
            raise ConfigError(tr('Б24 ёзувининг бот билан боғланиши ўзгарган'))
        if not member.admin and str(row.get('assignedById')) != str(member.bitrix_id):
            raise ConfigError(tr('Б24да масъул ўзгарган. Фақат ҳозирги масъул бошқара олади.'))
        return row

    def send_inline(self, user, text, buttons):
        self.telegram.call('sendMessage',{'chat_id':user,'text':text,'parse_mode':'HTML',
            'reply_markup':{'inline_keyboard':buttons},'link_preview_options':{'is_disabled':True}})

    def card(self, user, operation, member, heading=''):
        row = self.item_for(operation,member,user)
        stages = self.crm.stages(int(row['categoryId']))
        stage = next((item['title'] for item in stages if item['id'] == row['stageId']),row['stageId'])
        categories = self.crm.categories()
        category = next((item['title'] for item in categories if item['id'] == int(row['categoryId'])),str(row['categoryId']))
        ident = int(row['id'])
        text = (esc(heading)+'\n' if heading else '') + ''.join([tr('<b>📌 Б24 #'), format(ident, ''), ' · ', format(esc(row['title']), ''), tr('</b>\nВоронка: '), format(esc(category), ''), tr('\nБосқич: '), format(esc(stage), ''), tr('\nМасъул: Б24 #'), format(esc(row['assignedById']), ''), '\n'])
        text += ''.join(['<a href="', format(esc(operation['result']['url']), ''), tr('">Б24да очиш</a>\n\n▶️ Ҳолатни янгиланг ёки босқични танланг.')])
        buttons = [[{'text':tr('🔄 Янгилаш'),'callback_data':f'ld:card:{ident}'},
                    {'text':tr('🧭 Босқични ўзгартириш'),'callback_data':f'ld:stages:{ident}'}]]
        if operation['result'].get('activity_id') and getattr(self, 'reminders', None):
            from .reminders import phrase
            buttons.append([{'text':phrase('heading'),'callback_data':'rm:card:'+str(operation['result']['activity_id'])}])
        if self.work_inbox and self.work_inbox.store.role(user)=='fom_sales':
            from .work_inbox import NEW
            buttons.append([{'text':NEW,'callback_data':f'wr:from:{ident}'}])
        self.send_inline(user,text,buttons)

    def stage_menu(self,user,operation,member):
        row = self.item_for(operation,member,user)
        stages = self.crm.stages(int(row['categoryId']))
        token = self.stages.choice(user,operation,row,stages)
        self.stage_page(user,token,0)

    def stage_page(self,user,token,page):
        choice = self.stages.row('crm_stage_choices',token,user)
        rows = choice['value']['stages']
        if page < 0 or page*10 >= len(rows):
            raise ConfigError(tr('Нотўғри саҳифа'))
        buttons = [[{'text':row['title'][:100],'callback_data':f'ld:pick:{token}:{index}'}]
                   for index,row in enumerate(rows) if page*10 <= index < (page+1)*10]
        nav = []
        if page:
            nav.append({'text':'⬅️','callback_data':f'ld:page:{token}:{page-1}'})
        if (page+1)*10 < len(rows):
            nav.append({'text':'➡️','callback_data':f'ld:page:{token}:{page+1}'})
        if nav:
            buttons.append(nav)
        self.send_inline(user,tr('🧭 <b>Янги босқични танланг</b>\nБ24даги ҳозирги воронка босқичлари. Танлагандан кейин тасдиқлаш сўралади.'),buttons)

    def preview_stage(self,user,token,index,member):
        choice = self.stages.row('crm_stage_choices',token,user)
        operation = self.registration.store.operation(choice['request_id'])
        current = self.item_for(operation,member,user)
        observed = choice['value']['item']
        if any(current.get(key) != observed.get(key) for key in ('categoryId','stageId','updatedTime','assignedById')):
            raise ConfigError(tr('Б24да ёзув ўзгарган. Ёзувни янгилаб, босқични қайта танланг.'))
        intent = self.stages.intent(choice,index)
        target = intent['value']['target']
        self.send_inline(user,''.join([tr('👀 <b>Б24 #'), format(current['id'], ''), '</b>\n', format(esc(current['title']), ''), tr('\n\nЯнги босқич: <b>'), format(esc(target['title']), ''), tr('</b>\nБ24да шу босқичга ўтказилсинми?')]),
                         [[{'text':tr('✅ Б24да тасдиқлаш'),'callback_data':'ld:apply:'+intent['token']}],
                          [{'text':tr('↩️ Ёзувга қайтиш'),'callback_data':f"ld:card:{current['id']}"}]])

    def apply_stage(self,user,token,member):
        intent = self.stages.row('crm_stage_intents',token,user)
        operation = self.registration.store.operation(intent['request_id'])
        current = self.item_for(operation,member,user)
        observed, target = intent['value']['item'],intent['value']['target']
        if current.get('stageId') == target['id'] and current.get('categoryId') == observed.get('categoryId'):
            self.stages.status(token,'succeeded')
            return operation,tr('✅ Б24да босқич тасдиқланди: ') + target['title']
        if intent['status'] != 'prepared':
            return operation,tr('⏳ Аввалги ўтиш қайта юборилмади. Б24даги ҳозирги ҳолат қуйида.')
        if any(current.get(key) != observed.get(key) for key in ('categoryId','stageId','updatedTime','assignedById')):
            self.stages.status(token,'conflict')
            raise ConfigError(tr('Б24да ёзув ўзгарган. Ёзувни янгилаб, босқични қайта танланг.'))
        if not any(row['id'] == target['id'] for row in self.crm.stages(int(current['categoryId']))):
            self.stages.status(token,'conflict')
            raise ConfigError(tr('Босқич воронкада қолмаган. Рўйхатни янгиланг.'))
        if not self.stages.claim(token):
            return operation,tr('⏳ Бу ўтиш аввал қабул қилинган. Ҳолатни янгиланг.')
        try:
            self.crm.update_stage(int(current['id']),target['id'])
        except ConfigError:
            self.stages.status(token,'rejected')
            raise
        except RemoteError as exc:
            self.stages.status(token,'uncertain' if exc.uncertain else 'rejected',exc.code)
            return operation,tr('⏳ Б24 ўтишни тасдиқламади (')+exc.code+tr('). Қайта босиш яна ўтиш юбормайди; ҳолатни янгиланг.')
        try:
            after = self.item_for(operation,member,user)
        except RemoteError as exc:
            self.stages.status(token,'uncertain',exc.code)
            raise
        if after.get('stageId') == target['id']:
            self.stages.status(token,'succeeded')
            return operation,tr('✅ Босқич ўзгарди: ')+target['title']
        self.stages.status(token,'uncertain','STAGE_NOT_CONFIRMED')
        return operation,tr('⏳ Б24да танланган босқич тасдиқланмади. Ҳозирги ҳолат қуйида.')

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message',{}) if isinstance(callback,dict) else update.get('message',{})
        sender = callback.get('from',{}) if isinstance(callback,dict) else message.get('from',{})
        chat = message.get('chat',{})
        if chat.get('type') != 'private' or sender.get('is_bot') or not sender.get('id') or sender['id'] != chat.get('id'):
            return False
        user = int(sender['id'])
        text = message.get('text','') if not callback else ''
        text = text.strip() if isinstance(text,str) else ''
        command = text.split(maxsplit=1)[0].split('@',1)[0].lower() if text.startswith('/') else text
        command = DEAL if command in DEAL_LABELS else SUPPORT if command in SUPPORT_LABELS else command
        if not callback and command not in {'/b24_test',REAL_TEST,'/end_b24_test',END_TEST}:
            if (self.simulation and self.simulation.active(user)) or (self.inbox and self.inbox.store.mode(user)):
                return False
        state = self.registration.store.session(user)
        if callback and not str(callback.get('data','')).startswith('ld:'):
            return False
        commands = {'/b24_test',REAL_TEST,'/end_b24_test',END_TEST,'/my_crm',MY_CRM,'/crm_deal','/deal','/support',DEAL,SUPPORT,RESTART_DEAL}
        pending = self.registration.store.unfinished(user)
        if command in {'/pending',CHECK} and pending and self.registration.store.operation(pending[0]['request_id'])['value'].get('workflow')=='live_deal':
            commands.add(command)
        web_data = message.get('web_app_data') if not callback else None
        web_deal = False
        if isinstance(web_data,dict) and isinstance(web_data.get('data'),str) and len(web_data['data'].encode('utf-8'))<=4096:
            try:
                parsed = json.loads(web_data['data'])
                if isinstance(parsed, dict) and parsed.get('mode') == 'pharmacy':
                    return False
                web_deal = parsed.get('mode')=='deal'
            except (ValueError,AttributeError):
                pass
        if not callback and not web_deal and command not in commands and (not state or state.get('workflow') != 'live_deal'):
            return False
        from .registration import PROFILE_LABELS
        if not callback and command in {'/register','/profile',*PROFILE_LABELS,'/members','/registrations','/approve','/reject','/revoke','/test','/inbox_test','/okb','/pharmacy','/guide','/help','/next','/start'}:
            return False
        member = self.registration.user(user)
        if not member:
            self.telegram.send(user,tr('Аввал рўйхатдан ўтинг ва тасдиқ олинг.'),self.registration.keyboard(user))
            return True
        self.bot.config.users[user] = member
        try:
            if self.work_inbox and (web_deal or command in {'/deal',DEAL,RESTART_DEAL,'/crm_deal'}) and not self.registration.sales_allowed(user):
                raise ConfigError(tr('Сделку создаёт менеджер продаж ФОМ.'))
            if web_deal:
                from .input_forms import launch_matches, consume_launch
                from .intake_ui import phrase as intake
                data=json.loads(web_data['data'])
                fresh = not state or state.get('request_id')!=data.get('token')
                if fresh:
                    if (self.registration.store.unfinished(user) or (state and state.get('step') not in {'done','stats_scope','stats_period'})
                            or (self.work_inbox and self.work_inbox.store.session(user))):
                        raise ConfigError(tr('Сначала завершите текущую форму или отмените её.'))
                    if not launch_matches(self.registration.store,user,'deal',data.get('token')):
                        raise ConfigError(tr('Эта форма уже закрыта. Откройте новую форму через меню.'))
                if not self.registration.store.delivery(update.get('update_id')) and (
                        fresh or state.get('step') in {'sales_pharmacy','sales_bulk_input'}):
                    self.telegram.send(user,intake('processing_deal'))
                if fresh:
                    _,_,state=self.bot.route(user,'/deal',state)
                    state['request_id']=data['token']
                    self.registration.store.set_session(user,state)
                    consume_launch(self.registration.store,user,'deal',data['token'])
            if callback:
                try:
                    self.telegram.call('answerCallbackQuery',{'callback_query_id':callback.get('id','')})
                except RemoteError as exc:
                    if exc.code == 'TELEGRAM_401':
                        raise
                data = str(callback['data'])
                if data=='ld:hub':
                    from .crm_list import hub
                    hub(self,user,message.get('message_id'))
                    return True
                if match := re.fullmatch(r'ld:list:(deal|support):(\d{1,6})',data):
                    from .crm_list import show
                    show(self,user,member,int(match[2]),message.get('message_id'),scope=match[1])
                    return True
                if match := re.fullmatch(r'ld:list:(\d{1,6})',data):
                    from .crm_list import show
                    show(self,user,member,int(match[1]),message.get('message_id'),scope='deal' if self.registration.sales_allowed(user) else 'support')
                    return True
                if not self.registration.sales_allowed(user):
                    from .role_content import TEXTS
                    raise ConfigError(tr(TEXTS['sales_only']['ru']))
                match = re.fullmatch(r'ld:(card|stages):(\d+)',data)
                if match:
                    action,ident = match.groups()
                    operation = self.stages.operation_for(int(ident),user,member.admin)
                    (self.card if action=='card' else self.stage_menu)(user,operation,member)
                elif match := re.fullmatch(r'ld:(pick|page):([a-f0-9]{20}):(\d{1,4})',data):
                    action,token,index = match.groups()
                    if action == 'pick':
                        self.preview_stage(user,token,int(index),member)
                    else:
                        self.stage_page(user,token,int(index))
                elif match := re.fullmatch(r'ld:apply:([a-f0-9]{20}-\d{1,4})',data):
                    operation,heading = self.apply_stage(user,match[1],member)
                    self.card(user,operation,member,heading)
                else:
                    raise ConfigError(tr('Нотўғри тугма'))
                return True
            if command in {'/b24_test',REAL_TEST,'/end_b24_test',END_TEST}:
                if not member.admin:
                    raise ConfigError(tr('Б24 синови администратор учун'))
                active = command in {'/b24_test',REAL_TEST}
                if self.simulation:
                    self.simulation.set_active(user,False)
                if self.inbox:
                    self.inbox.store.exit(user)
                with self.registration.store.db:
                    self.registration.store.db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('b24-test:'+str(user),'1' if active else '0'))
                text = (tr('🧪 <b>Б24га ёзиб синаш</b>\n\nСделкалар ҳақиқий Б24да яратилади. Номида «[ТЕСТ БОТА]» бўлади. Масъул — сиз.\n'
                        '1️⃣ Оддий ёки техник ёрдам сделкасини танланг.\n2️⃣ Воронка ва бошланғич босқични танланг.\n3️⃣ Формани тўлдириб, тасдиқланг.\n'
                        '📂 Ёзув «Б24 ёзувларим»да сақланади. Карточкадан босқични ўзгартириш мумкин.') if active else tr('🏁 Б24 синови тугади. Яратилган Б24 ёзувлари ва уларнинг боғланишлари сақланди.'))
                self.telegram.send(user,text,MENU if active else self.registration.keyboard(user))
                return True
            if command in {'/my_crm',MY_CRM,'/crm_deal'}:
                if command != '/crm_deal':
                    from .crm_list import hub
                    hub(self,user)
                    return True
                if command == '/crm_deal':
                    parts = text.split()
                    if len(parts)!=2 or not parts[1].isascii() or not parts[1].isdigit():
                        raise ConfigError(tr('Масалан: /crm_deal 123'))
                    operations = [self.stages.operation_for(int(parts[1]),user,member.admin)]
                else:
                    rows = self.registration.store.db.execute("SELECT request_id FROM operations WHERE user_id=? AND kind IN ('deal','support') AND status IN ('succeeded','created') ORDER BY created_at DESC LIMIT 20",(user,))
                    operations = [self.registration.store.operation(row['request_id']) for row in rows]
                    operations = [row for row in operations if row['value'].get('workflow')=='live_deal' and row.get('result') and row['result'].get('id')]
                tickets = self.work_inbox.store.listing(user) if self.work_inbox and command != '/crm_deal' else []
                self.telegram.send(user,
                    tr('📂 <b>Б24даги сделкаларим ва заявкаларим</b>\n\n'
                    'Бу ерда бот орқали яратган сделкаларингиз ва сизга очиқ техник ёрдам заявкалари кўринади. '
                    'Карточкаларнинг ҳозирги босқичи Б24дан ўқилади. Бу Б24нинг тўлиқ рўйхати эмас.\n\n')
                    +''.join([tr('Сделкалар: '), format(len(operations), ''), tr(' · Заявкалар: '), format(len(tickets), ''), tr('.\nКарточкани очиб, ҳолатини текширинг ёки рухсат этилган кейинги амални танланг.')]),
                    self.registration.keyboard(user))
                for operation in operations:
                    self.card(user,operation,member)
                for ticket in tickets:
                    self.work_inbox.card(user,ticket)
                return True
            self.bot.handle(update)
            # The confirmation already contains the link; detailed controls remain
            # available in My CRM instead of producing a second automatic card.
        except ConfigError as exc:
            self.telegram.send(user,'⚠️ '+esc(exc),with_guide([[MY_CRM],['/pending'],[CANCEL]]))
        except RemoteError as exc:
            if exc.code in {'TELEGRAM_401','TELEGRAM_403','TELEGRAM_409'}:
                raise
            self.telegram.send(user,tr('⏳ Б24/Telegram жавоби тасдиқланмади (')+esc(exc.code)+tr('). Ёзув сақланган бўлса, «Б24 ёзувларим»дан текширинг. Сақлашни /pending орқали текшириш мумкин.'),MENU)
        return True
