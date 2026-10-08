from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.config import ConfigError, User
from datfo_crm_bot.demo import demo_config
from datfo_crm_bot.live_deals import LiveDeals, SOURCE, TEST_PREFIX, MY_CRM, RESTART_DEAL
from datfo_crm_bot.sales_intake import USE_PHARMACY, USE_ADDRESS, NEXT_DAY, NEW_PHARMACY, CONTINUE_DEAL, SEARCH_AGAIN
from datfo_crm_bot.registration import Registration, RegistrationSettings
from datfo_crm_bot.simulation import Simulation
from datfo_crm_bot.communications import InboxStore, InboxTest
from datfo_crm_bot.storage import Store
from test_communications import FakeTelegram, NoDirectory


class DealApi:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.lost_create = False
        self.lost_update = False
        self.reject_update = False
        self.hide_created = False
        self.reject_create = False
        self.lost_activity = False
        self.reject_activity = False
        self.reject_metadata = False
        self.lost_metadata = False
        self.hide_activities = False
        self.activities = {}
        self.pharmacies = {81: {'id':81, 'title':'Проверка связи', 'companyId':7, 'categoryId':17,
            'UF_CRM_8_1719398102513':'00081','UF_CRM_8_1719398563749':'+998909876543','UF_CRM_8_1719398485569':'Ташкент, улица 1'}}
        self.clock = 0
        self.catalogue = {category:[{'id':f'C{category}:BASE','title':'База','sort':10,'semantics':None},
                                   {'id':f'C{category}:QUAL','title':'Квалификация','sort':20,'semantics':None},
                                   {'id':f'C{category}:WON','title':'Успех','sort':30,'semantics':'S'}] for category in (43,45,47)}
        self.catalogue[45][0].update(id='C45:PLAN',title='План')

    def call(self,method,payload):
        self.calls.append((method,deepcopy(payload)))
        if method=='crm.item.fields':
            from datfo_crm_bot.sales_options import DISCUSSION_FIELD
            fields = {name:{'isReadOnly':False} for name in ('title','companyId','assignedById','originId','comments','originatorId','categoryId','stageId','parentId1034')}
            fields[DISCUSSION_FIELD] = {'type':'enumeration','isMultiple':True,'items':[{'ID':'5246','VALUE':'◉ F-Apteka: Business'},{'ID':'5247','VALUE':'◉ F-Kassa'}]}
            fields['UF_CRM_8_1772692252313'] = {'type':'enumeration','isMultiple':False,'items':[{'ID':'4793','VALUE':'Работает'},{'ID':'4794','VALUE':'Не работаем'},{'ID':'4795','VALUE':'Закрыта'}]}
            return {'result':{'fields':fields}}
        if method=='crm.category.list':
            return {'result':{'categories':[{'id':43,'name':'DATFO Новые продажи','sort':100},{'id':47,'name':'Техобслуживание','sort':200},{'id':45,'name':'Продажи и онбординг [KG]','sort':300}]}}
        if method=='crm.company.get':
            return {'result':{'ID':payload['id'],'TITLE':'Аптека Компания'}}
        if method=='crm.item.list':
            if payload['select'] != ['*']:
                return {'result':[]}
            name = payload['filter']['%title'].casefold()
            return {'result':{'items':[deepcopy(row) for row in self.pharmacies.values() if name in row['title'].casefold()]}}
        if method=='crm.item.get':
            if payload['entityTypeId']==1034:
                return {'result':{'item':deepcopy(self.pharmacies[int(payload['id'])])}}
            return {'result':{'item':deepcopy(self.rows[int(payload['id'])])}}
        if method=='crm.item.add':
            if self.reject_create:
                raise RemoteError('ACCESS_DENIED')
            ident = len(self.rows)+501
            self.clock += 1
            row = {'id':ident,**deepcopy(payload['fields']),'updatedTime':str(self.clock)}
            self.rows[ident] = row
            if self.lost_create:
                self.lost_create = False
                raise RemoteError('CONNECTION_ERROR',uncertain=True)
            return {'result':{'item':deepcopy(row)}}
        if method=='crm.activity.todo.add':
            if self.reject_activity:
                raise RemoteError('ACCESS_DENIED')
            ident = len(self.activities)+901
            self.activities[ident] = {'ID':ident,'DESCRIPTION':payload['description'],'ownerId':payload['ownerId'],**deepcopy(payload)}
            if self.lost_activity:
                self.lost_activity = False
                raise RemoteError('CONNECTION_ERROR',uncertain=True)
            return {'result':{'id':ident}}
        if method=='crm.activity.get':
            activity = self.activities[payload['id']]
            return {'result': {'ID':payload['id'], 'OWNER_TYPE_ID':2, 'OWNER_ID':activity['ownerId'],
                'PROVIDER_ID':'CRM_TODO', 'SUBJECT':activity['title'], 'DESCRIPTION':activity['description'],
                'RESPONSIBLE_ID':activity['responsibleId'], 'DEADLINE':activity['deadline'],
                'COMPLETED':activity.get('completed','N'), 'ORIGINATOR_ID':activity.get('ORIGINATOR_ID'),
                'ORIGIN_ID':activity.get('ORIGIN_ID')}}
        if method=='crm.activity.update':
            if self.reject_metadata:
                raise RemoteError('ACCESS_DENIED')
            activity = self.activities[payload['id']]
            activity.update(deepcopy(payload['fields']))
            if 'DESCRIPTION' in payload['fields']:
                activity['description'] = payload['fields']['DESCRIPTION']
            if self.lost_metadata:
                self.lost_metadata = False
                raise RemoteError('CONNECTION_ERROR',uncertain=True)
            return {'result':True}
        if method=='crm.item.update':
            if self.reject_update:
                raise RemoteError('ACCESS_DENIED')
            row = self.rows[int(payload['id'])]
            row.update(payload['fields'])
            self.clock += 1
            row['updatedTime'] = str(self.clock)
            if self.lost_update:
                self.lost_update = False
                raise RemoteError('CONNECTION_ERROR',uncertain=True)
            return {'result':{'item':deepcopy(row)}}
        raise AssertionError(method)

    def list_all(self,method,payload,*,key=None):
        if method=='crm.category.list':
            return self.call(method,payload)['result']['categories']
        if method=='crm.status.list':
            entity = payload['filter']['ENTITY_ID']
            category = int(entity.rsplit('_',1)[1])
            return [{'ENTITY_ID':entity,'STATUS_ID':row['id'],'NAME':row['title'],'SORT':row['sort'],'SEMANTICS':row['semantics']} for row in self.catalogue[category]]
        if method=='crm.item.list':
            if payload.get('entityTypeId')==1034:
                return [deepcopy(row) for row in self.pharmacies.values() if all(str(row.get(k))==str(v) for k,v in payload['filter'].items())]
            if self.hide_created:
                return []
            return [deepcopy(row) for row in self.rows.values() if all(str(row.get(k))==str(v) for k,v in payload['filter'].items())]
        if method=='crm.requisite.list':
            return [{'ENTITY_TYPE_ID':4,'ENTITY_ID':7,'RQ_INN':'123456789'}]
        if method=='crm.company.list':
            return [{'ID':7,'TITLE':'Аптека Компания'}]
        if method=='crm.activity.list':
            if self.hide_activities:
                return []
            return [deepcopy(row) for row in self.activities.values() if row['ownerId']==payload['filter']['OWNER_ID']]
        raise AssertionError(method)


class LiveDealTests(unittest.TestCase):
    # Keep coverage of the old persisted intake and all its write/recovery phases.
    legacy_entries = True
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        from miniapp_fixture import published_form
        published_form(self, root)
        self.path = root/'primary.sqlite3'
        self.store = Store(self.path)
        for tg,b24 in ((100,132),(200,133)):
            self.store.request_registration(tg,tg,{'id':b24,'name':f'Менеджер {b24}'})
            self.store.approve_registration(tg,100,{'id':b24,'name':f'Менеджер {b24}'})
        self.telegram = FakeTelegram()
        self.registration = Registration(RegistrationSettings({100:User(132,True,'Менеджер 132')}),self.store,NoDirectory(),self.telegram)
        self.config = replace(demo_config(self.path),targets={})
        self.api = DealApi()
        self.sim_store = Store(root/'sim.sqlite3')
        self.inbox_store = InboxStore(root/'inbox.sqlite3')
        self.sim = Simulation(self.registration,self.sim_store,self.telegram)
        self.inbox = InboxTest(self.registration,self.inbox_store,self.telegram,self.sim)
        self.live = LiveDeals(self.registration,self.config,self.api,self.telegram,self.sim,self.inbox)
        self.sequence = 0

    def tearDown(self):
        self.store.close()
        self.sim_store.close()
        self.inbox_store.close()
        self.folder.cleanup()

    def message(self,text,user=100):
        self.sequence += 1
        return {'update_id':self.sequence,'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}}

    def say(self,text,user=100):
        update = self.message(text,user)
        self.live.handle(update)
        if text == '/deal' and getattr(self, 'legacy_entries', True):
            state = self.store.session(user)
            if state and state.get('step') == 'sales_bulk_input':
                self.store.set_session(user, {**state, 'step': 'sales_pharmacy', 'discussion_programs':[{'id':'5246','title':'◉ F-Apteka: Business'}]})
        return update

    def form(self,kind='deal',user=100,company=False):
        if kind=='deal':
            for text in ('/deal','Проверка связи',USE_PHARMACY,USE_ADDRESS,'🔔 Добавить напоминание',
                         'Позвонить и согласовать презентацию',NEXT_DAY,'Без телефона'):
                self.say(text,user)
            return self.store.session(user)
        for text in ('/'+kind,'1','1','Проверка связи','123456789' if company else 'Без ИНН'):
            self.say(text,user)
        if company:
            self.say('1',user)
        self.say('Описание для проверки',user)
        self.say('Без телефона',user)
        return self.store.session(user)

    def create(self,kind='deal',user=100):
        state = self.form(kind,user)
        self.say('Подтвердить',user)
        return self.store.operation(state['request_id'])

    def callback(self,data,user=100):
        self.sequence += 1
        return {'update_id':self.sequence,'callback_query':{'id':'cb'+str(self.sequence),'from':{'id':user},'message':{'chat':{'id':user,'type':'private'}},'data':data}}

    def intent(self,operation,index=1,user=100):
        member = self.registration.user(user)
        self.live.stage_menu(user,operation,member)
        token = self.store.db.execute('select token from crm_stage_choices order by rowid desc limit 1').fetchone()[0]
        self.live.preview_stage(user,token,index,member)
        return token+'-'+str(index)

    def test_real_test_requires_confirmation_and_saves_user_category_stage_and_link(self):
        self.say('/b24_test')
        state = self.form(company=True)
        self.assertEqual(len(self.api.rows),0)
        self.assertTrue(state['title'].startswith(TEST_PREFIX))
        self.say('Подтвердить')
        row = self.api.rows[501]
        self.assertEqual((row['assignedById'],row['companyId'],row['categoryId'],row['stageId']),(132,7,45,'C45:PLAN'))
        self.assertEqual(row['parentId1034'],81)
        self.assertIn('Адрес: Ташкент, улица 1',row['comments'])
        self.assertEqual(self.api.activities[901]['responsibleId'],132)
        self.assertEqual(row['originatorId'],SOURCE)
        self.assertEqual(row['originId'],state['request_id'])
        operation = self.store.operation(state['request_id'])
        self.assertEqual(operation['result']['id'],501)
        self.say('/my_crm')
        self.live.handle(self.callback('ld:list:deal:0'))
        self.assertIn('Квалификация',str(self.api.catalogue))
        self.assertIn('#501',self.telegram.calls[-1][1]['text'])

    def test_support_creates_deal_in_explicitly_selected_technical_pipeline(self):
        for text in ('/support','2','1','Сбой подключения','Без ИНН','Проверить сеть','Без телефона','Подтвердить'):
            self.say(text)
        row = self.api.rows[501]
        self.assertEqual((row['categoryId'],row['stageId'],row['assignedById']),(47,'C47:BASE',132))

    def test_new_registered_manager_uses_own_bitrix_account_without_test_prefix(self):
        self.create(user=200)
        self.assertEqual(self.api.rows[501]['assignedById'],133)
        self.assertFalse(self.api.rows[501]['title'].startswith(TEST_PREFIX))

    def test_lost_create_response_and_pending_after_lost_session_recover_same_id(self):
        state = self.form()
        self.api.lost_create = True
        self.say('Подтвердить')
        self.assertEqual(self.store.operation(state['request_id'])['status'],'uncertain')
        self.store.set_session(100,None)
        self.say('/pending')
        self.assertEqual(self.store.operation(state['request_id'])['result']['id'],501)
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)

    def test_unknown_create_result_never_resends_even_if_search_temporarily_empty(self):
        self.form()
        self.api.lost_create = True
        self.say('Подтвердить')
        self.api.hide_created = True
        self.say('/pending')
        self.say('Подтвердить')
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)

    def test_lost_telegram_result_replayed_update_does_not_create_again(self):
        self.form()
        self.telegram.fail_reply = True
        update = self.say('Подтвердить')
        self.live.handle(update)
        self.assertEqual(len(self.api.rows),1)
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)

    def test_stage_requires_explicit_confirm_and_duplicate_click_never_updates_twice(self):
        operation = self.create()
        token = self.intent(operation)
        self.assertEqual(self.api.rows[501]['stageId'],'C45:PLAN')
        self.live.handle(self.callback('ld:apply:'+token))
        self.live.handle(self.callback('ld:apply:'+token))
        self.assertEqual(self.api.rows[501]['stageId'],'C45:QUAL')
        mutations = [payload for method,payload in self.api.calls if method=='crm.item.update']
        self.assertEqual(len(mutations),1)
        self.assertEqual(mutations[0]['fields'],{'stageId':'C45:QUAL'})

    def test_stale_stage_confirmation_does_not_replace_external_edit(self):
        operation = self.create()
        token = self.intent(operation)
        self.api.rows[501]['stageId']='C45:WON'
        self.api.rows[501]['updatedTime']='external'
        self.live.handle(self.callback('ld:apply:'+token))
        self.assertEqual(self.api.rows[501]['stageId'],'C45:WON')
        self.assertEqual(sum(method=='crm.item.update' for method,_ in self.api.calls),0)

    def test_removed_stage_is_rejected_before_write(self):
        operation = self.create()
        token = self.intent(operation)
        self.api.catalogue[45] = [self.api.catalogue[45][0]]
        self.live.handle(self.callback('ld:apply:'+token))
        self.assertEqual(sum(method=='crm.item.update' for method,_ in self.api.calls),0)

    def test_lost_stage_response_is_verified_by_read_without_repeating_update(self):
        operation = self.create()
        token = self.intent(operation)
        self.api.lost_update = True
        self.live.handle(self.callback('ld:apply:'+token))
        self.live.handle(self.callback('ld:apply:'+token))
        self.assertEqual(sum(method=='crm.item.update' for method,_ in self.api.calls),1)
        self.assertEqual(self.store.db.execute('select status from crm_stage_intents where token=?',(token,)).fetchone()[0],'succeeded')

    def test_explicit_stage_rejection_is_not_automatically_retried(self):
        operation = self.create()
        token = self.intent(operation)
        self.api.reject_update = True
        self.live.handle(self.callback('ld:apply:'+token))
        self.api.reject_update = False
        self.live.handle(self.callback('ld:apply:'+token))
        self.assertEqual(self.api.rows[501]['stageId'],'C45:PLAN')
        self.assertEqual(sum(method=='crm.item.update' for method,_ in self.api.calls),1)

    def test_link_survives_restart_and_cards_follow_actual_pipeline_from_bitrix(self):
        operation = self.create()
        self.api.rows[501].update(categoryId=47,stageId='C47:QUAL')
        self.store.close()
        self.store = Store(self.path)
        self.registration.store = self.store
        self.live = LiveDeals(self.registration,self.config,self.api,self.telegram,self.sim,self.inbox)
        self.say('/crm_deal 501')
        self.assertIn('Техобслуживание',self.telegram.calls[-1][1]['text'])
        self.assertIn('Квалификация',self.telegram.calls[-1][1]['text'])
        self.assertEqual(self.store.operation(operation['request_id'])['result']['id'],501)

    def test_callback_identity_source_and_current_responsible_are_rechecked(self):
        operation = self.create(user=200)
        token = self.intent(operation,user=200)
        self.live.handle(self.callback('ld:apply:'+token,user=100))
        self.api.rows[501]['assignedById']=132
        self.live.handle(self.callback('ld:apply:'+token,user=200))
        self.api.rows[501]['assignedById']=133
        self.api.rows[501]['originatorId']='another-app'
        self.live.handle(self.callback('ld:apply:'+token,user=200))
        self.assertEqual(sum(method=='crm.item.update' for method,_ in self.api.calls),0)

    def test_unrelated_record_ids_and_unregistered_users_are_denied(self):
        self.create()
        self.say('/crm_deal 777')
        self.say('/deal',user=300)
        self.assertEqual(len(self.api.rows),1)
        self.assertIn('рўйхатдан',self.telegram.messages[-1][1])

    def test_offline_modes_remain_offline_until_real_test_selected(self):
        self.sim.set_active(100,True)
        self.assertFalse(self.live.handle(self.message('/deal')))
        self.say('/b24_test')
        self.assertFalse(self.sim.active(100))
        self.inbox_store.bootstrap(100)
        self.inbox_store.set_mode(100,'tech')
        self.assertFalse(self.live.handle(self.message('/support')))
        self.say('/b24_test')
        self.assertIsNone(self.inbox_store.mode(100))
        self.assertEqual(len(self.api.rows),0)

    def test_sending_intent_after_restart_only_reads_and_never_reapplies(self):
        operation = self.create()
        token = self.intent(operation)
        self.live.stages.status(token,'sending')
        self.live.handle(self.callback('ld:apply:'+token))
        self.assertEqual(sum(method=='crm.item.update' for method,_ in self.api.calls),0)
        self.assertEqual(self.api.rows[501]['stageId'],'C45:PLAN')

    def test_cancelled_preview_and_mode_exit_keep_already_created_link(self):
        self.say('/b24_test')
        self.form()
        self.say('Отмена')
        self.assertEqual(len(self.api.rows),0)
        operation = self.create()
        self.say('/end_b24_test')
        self.assertFalse(self.live.bot.test_mode(100))
        self.assertEqual(self.store.operation(operation['request_id'])['result']['id'],501)

    def test_sales_starts_in_kg_plan_without_category_or_stage_choices(self):
        self.legacy_entries = False
        self.say('/deal')
        state = self.store.session(100)
        self.assertEqual((state['step'],state['category_id'],state['initial_stage']),('sales_bulk_input',45,'C45:PLAN'))
        self.assertIn('форму',self.telegram.messages[-1][1])
        self.assertFalse(any(method=='crm.item.add' for method,_ in self.api.calls))

    def test_adapter_also_rejects_direct_creation_from_legacy_pipeline(self):
        with self.assertRaises(ConfigError):
            self.live.crm.create({'kind':'deal','category_id':43,'initial_stage':'C43:BASE'},132,100)
        self.assertEqual(self.api.rows,{})

    def test_legacy_preview_is_not_submitted_to_old_pipeline(self):
        state = {'workflow':'live_deal','kind':'deal','step':'confirm','request_id':'old-draft',
                 'category_id':43,'category_name':'DATFO Новые продажи','initial_stage':'C43:BASE','title':'Старый черновик'}
        self.store.set_session(100,state)
        self.store.prepare(100,state,'2026-10-06T10:00:00+00:00')
        self.say('Подтвердить')
        self.assertEqual(self.api.rows,{})
        self.assertIn('Старый черновик не отправлен',self.telegram.messages[-1][1])
        self.say(RESTART_DEAL)
        self.assertEqual(self.store.session(100)['category_id'],45)
        self.assertEqual(self.store.operation('old-draft')['value']['title'],'Старый черновик')

    def test_existing_pharmacy_requires_confirmation_and_can_be_changed(self):
        for text in ('/deal','Проверка связи'):
            self.say(text)
        self.assertEqual(self.store.session(100)['step'],'sales_pharmacy_confirm')
        self.say('Подтвердить')
        self.assertEqual(self.store.session(100)['step'],'sales_pharmacy_confirm')
        self.say(SEARCH_AGAIN)
        self.assertEqual(self.store.session(100)['step'],'sales_bulk_input')
        self.assertEqual(self.api.rows,{})

    def test_old_search_button_cannot_replace_frozen_final_preview(self):
        state = self.form()
        frozen = self.store.operation(state['request_id'])['value']
        self.say(SEARCH_AGAIN)
        self.assertEqual(self.store.session(100)['step'],'confirm')
        self.assertEqual(self.store.session(100)['pharmacy'],frozen['pharmacy'])
        self.say('Подтвердить')
        self.assertEqual(self.api.rows[501]['parentId1034'],frozen['pharmacy']['id'])

    def test_new_pharmacy_from_similar_match_clears_old_selection(self):
        self.use_offline_pharmacy_components()
        for text in ('/deal','Проверка',NEW_PHARMACY):
            self.say(text)
        state = self.store.session(100)
        self.assertNotIn('pharmacy',state)
        self.assertTrue(state['new_pharmacy'])
        self.assertEqual(state['step'],'sales_okb')

    def test_multiple_pharmacies_are_never_chosen_automatically(self):
        self.api.pharmacies[82] = {**self.api.pharmacies[81],'id':82,'title':'Проверка связи филиал','UF_CRM_8_1719398485569':'Самарканд, улица 2'}
        for text in ('/deal','Проверка связи','2',USE_PHARMACY):
            self.say(text)
        state = self.store.session(100)
        self.assertEqual(state['pharmacy']['id'],82)
        self.assertEqual(state['step'],'sales_address')
        self.assertEqual(self.api.rows,{})

    def test_too_many_matches_ask_for_narrower_search(self):
        self.api.pharmacies = {n:{**self.api.pharmacies[81],'id':n} for n in range(1,12)}
        self.say('/deal')
        self.say('Проверка')
        self.assertEqual(self.store.session(100)['step'],'sales_pharmacy')
        self.assertIn('больше 10',self.telegram.messages[-1][1])

    def test_missing_pharmacy_asks_before_starting_okb(self):
        self.say('/deal')
        self.say('Новая аптека')
        self.assertEqual(self.store.session(100)['step'],'sales_match')
        self.assertIn('добавить её в базу',self.telegram.messages[-1][1])
        self.assertEqual(self.api.rows,{})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)

    def test_address_and_local_phone_are_preserved_without_editing_existing_pharmacy(self):
        original = deepcopy(self.api.pharmacies[81])
        for text in ('/deal','Проверка связи',USE_PHARMACY,'Ташкент, новый адрес, 4',
                     '🔔 Добавить напоминание','Назначить презентацию',NEXT_DAY,'90 123-45-67','Подтвердить'):
            self.say(text)
        row = self.api.rows[501]
        self.assertIn('Адрес: Ташкент, новый адрес, 4',row['comments'])
        self.assertIn('Телефон: +998901234567',row['comments'])
        self.assertEqual(self.api.pharmacies[81],original)
        self.assertEqual(self.api.activities[901]['ownerId'],501)
        self.assertEqual(self.api.activities[901]['title'],'Назначить презентацию')

    def test_access_denied_keeps_draft_and_uses_friendly_error_then_retry(self):
        state = self.form()
        self.api.reject_create = True
        self.say('Подтвердить')
        response = self.telegram.messages[-1][1]
        self.assertNotIn('ACCESS_DENIED',response)
        self.assertIn('нет нужного права',response)
        self.assertNotEqual(self.store.operation(state['request_id'])['status'],'succeeded')
        self.assertEqual(self.api.rows,{})
        self.api.reject_create = False
        self.say(CONTINUE_DEAL)
        self.assertEqual(self.store.operation(state['request_id'])['status'],'succeeded')
        self.assertEqual(len(self.api.rows),1)

    def test_failed_activity_returns_deal_link_and_retry_only_adds_activity(self):
        state = self.form()
        self.api.reject_activity = True
        self.say('Подтвердить')
        operation = self.store.operation(state['request_id'])
        self.assertEqual(operation['status'],'created')
        self.assertIn('/crm/deal/details/501/',self.telegram.messages[-1][1])
        self.api.reject_activity = False
        self.say(CONTINUE_DEAL)
        self.assertEqual(self.store.operation(state['request_id'])['status'],'succeeded')
        self.assertEqual(sum(m=='crm.item.add' for m,_ in self.api.calls),1)
        self.assertEqual(len(self.api.activities),1)

    def test_lost_activity_response_recovers_without_second_deal_or_activity(self):
        state = self.form()
        self.api.lost_activity = True
        self.say('Подтвердить')
        self.assertEqual(self.store.operation(state['request_id'])['status'],'created')
        self.store.set_session(100,None)
        self.say('/pending')
        self.assertEqual(self.store.operation(state['request_id'])['status'],'created')
        self.say(CONTINUE_DEAL)
        self.assertEqual(self.store.operation(state['request_id'])['status'],'succeeded')
        self.assertEqual(sum(m=='crm.item.add' for m,_ in self.api.calls),1)
        self.assertEqual(sum(m=='crm.activity.todo.add' for m,_ in self.api.calls),1)

    def test_reminder_marker_is_moved_out_of_description_before_success(self):
        operation = self.create()
        activity = self.api.activities[901]
        self.assertEqual(activity['description'],operation['value']['next_step'])
        self.assertEqual(activity['ORIGINATOR_ID'],SOURCE)
        self.assertEqual(activity['ORIGIN_ID'],operation['request_id']+'-next-step')
        self.assertEqual(activity['pingOffsets'],[15])
        self.assertEqual(self.live.bot.sales.find_activity(operation['value'],operation['result']),{'id':901})
        writes=[payload for method,payload in self.api.calls if method=='crm.activity.update']
        self.assertEqual(set(writes[0]['fields']),{'DESCRIPTION','ORIGINATOR_ID','ORIGIN_ID'})

    def test_metadata_lost_response_recovers_without_another_activity_or_update(self):
        state=self.form()
        self.api.lost_metadata=True
        self.say('Подтвердить')
        self.assertEqual(self.store.operation(state['request_id'])['status'],'created')
        self.say('/pending')
        self.assertEqual(self.store.operation(state['request_id'])['status'],'succeeded')
        self.assertEqual(sum(method=='crm.activity.todo.add' for method,_ in self.api.calls),1)
        self.assertEqual(sum(method=='crm.activity.update' for method,_ in self.api.calls),1)
        self.assertNotIn('Запрос бота:',self.api.activities[901]['description'])

    def test_denied_metadata_retry_preserves_saved_activity_and_user_text(self):
        state=self.form()
        self.api.reject_metadata=True
        self.say('Подтвердить')
        self.assertEqual(self.store.operation_step(state['request_id'],'sales_next_step')['result'],{'id':901})
        activity=self.api.activities[901]
        activity['description']='Позвонить\nКомментарий менеджера\nЗапрос бота: '+state['request_id']+'-next-step'
        activity['DESCRIPTION']=activity['description']
        activity['deadline']='2026-10-10T12:00:00+05:00'
        self.api.reject_metadata=False
        self.say(CONTINUE_DEAL)
        self.assertEqual(self.store.operation(state['request_id'])['status'],'succeeded')
        self.assertEqual(activity['description'],'Позвонить\nКомментарий менеджера')
        self.assertEqual(activity['deadline'],'2026-10-10T12:00:00+05:00')
        self.assertEqual(sum(method=='crm.activity.todo.add' for method,_ in self.api.calls),1)

    def test_unknown_activity_result_never_reposts_if_search_lags(self):
        self.form()
        self.api.lost_activity = True
        self.say('Подтвердить')
        self.api.hide_activities = True
        self.say(CONTINUE_DEAL)
        self.say('/pending')
        self.assertEqual(sum(m=='crm.item.add' for m,_ in self.api.calls),1)
        self.assertEqual(sum(m=='crm.activity.todo.add' for m,_ in self.api.calls),1)

    def test_new_buttons_are_translated_and_normalized_back_to_working_actions(self):
        from datfo_crm_bot.language_ui import LanguageUI
        from datfo_crm_bot.i18n import tr
        ui = LanguageUI(self.registration,self.telegram)
        for action in (USE_PHARMACY,NEW_PHARMACY,SEARCH_AGAIN,USE_ADDRESS,NEXT_DAY,CONTINUE_DEAL,RESTART_DEAL):
            translated = tr(action,'uz')
            self.assertNotEqual(translated,action)
            self.assertEqual(ui.normalize(self.message(translated))['message']['text'],action)

    def test_activity_targets_current_responsible_after_native_automation(self):
        state = self.form()
        self.api.reject_activity = True
        self.say('Подтвердить')
        self.api.rows[501]['assignedById']=133
        self.api.reject_activity = False
        self.say(CONTINUE_DEAL)
        self.assertEqual(self.api.activities[901]['responsibleId'],133)
        self.assertEqual(self.store.operation(state['request_id'])['status'],'succeeded')

    def test_moved_company_blocks_creation_instead_of_wrong_link(self):
        self.form()
        self.api.pharmacies[81]['companyId']=9
        self.say('Подтвердить')
        self.assertEqual(self.api.rows,{})
        self.assertIn('Компания выбранной аптеки изменилась',self.telegram.messages[-1][1])

    def test_plan_stage_removed_after_preview_does_not_create_in_another_stage(self):
        self.form()
        self.api.catalogue[45][0]['title']='Новая стадия'
        self.say('Подтвердить')
        self.assertEqual(self.api.rows,{})

    def test_success_thanks_and_links_to_deal_and_reports_activity(self):
        state = self.form()
        self.say('Подтвердить')
        saved = self.store.delivery(self.sequence)
        self.assertIn('Спасибо!',saved['text'])
        self.assertIn('/crm/deal/details/501/',saved['text'])
        self.assertIn('Вам добавлено напоминание на',saved['text'])
        self.assertIn('18:00', saved['text'])
        self.assertNotIn('/crm_deal', saved['text'])
        self.assertNotIn('Кейинги амаллар', saved['text'])
        self.say('Подтвердить')
        self.assertEqual(sum(m=='crm.item.add' for m,_ in self.api.calls),1)
        self.assertEqual(sum(m=='crm.activity.todo.add' for m,_ in self.api.calls),1)

    def use_offline_pharmacy_components(self):
        from datfo_crm_bot.simulation import SimulationCrm
        sales = self.live.bot.sales
        crm = SimulationCrm(self.sim_store)
        choices = crm.choices
        crm.choices = lambda key: [{'id':'5919','title':'1С'},{'id':'5920','title':'Excel'}] if key=='current_program' else choices(key)
        crm.write_id = lambda value: int(value)
        sales.pharmacy_crm = sales.form.crm = sales.okb.crm = crm
        sales.company = lambda ident: {'id':ident,'title':'Учебная фирма'}
        return crm

    def new_pharmacy_form(self):
        for text in ('/deal','Новая аптека',NEW_PHARMACY,'111222333','Учебная фирма','Без телефона',
                     '3','МИРАБАДСКИЙ','1','Учебная улица, 1','Пропустить локацию','1',
                     '🔔 Добавить напоминание','Презентация',NEXT_DAY):
            self.say(text)
        return self.store.session(100)

    def test_new_pharmacy_company_and_deal_wait_for_final_confirmation(self):
        crm = self.use_offline_pharmacy_components()
        state = self.new_pharmacy_form()
        self.assertEqual(state['step'],'confirm')
        self.assertEqual(crm.records['companies'],{})
        self.assertEqual(crm.items,{})
        self.assertEqual(self.api.rows,{})
        self.say('Подтвердить')
        operation = self.store.operation(state['request_id'])
        self.assertEqual(operation['status'],'succeeded')
        self.assertEqual(len(crm.records['companies']),1)
        self.assertEqual(len(crm.items),1)
        self.assertEqual(self.api.rows[501]['companyId'],200)
        self.assertEqual(self.api.rows[501]['parentId1034'],operation['result']['pharmacy_id'])
        self.say('Подтвердить')
        self.assertEqual(len(crm.items),1)
        self.assertEqual(len(self.api.rows),1)

    def test_partial_new_pharmacy_recovery_does_not_repeat_company_or_pharmacy(self):
        crm = self.use_offline_pharmacy_components()
        state = self.new_pharmacy_form()
        self.api.reject_create = True
        self.say('Подтвердить')
        self.assertEqual(len(crm.items),1)
        self.say('/my_crm')
        self.api.reject_create = False
        self.say(CONTINUE_DEAL)
        self.assertEqual(self.store.operation(state['request_id'])['status'],'succeeded')
        self.assertEqual(len(crm.records['companies']),1)
        self.assertEqual(len(crm.items),1)
        self.assertEqual(len(self.api.rows),1)

    def test_sales_embedded_contact_form_and_final_notices_survive_deal_rejection(self):
        from datfo_crm_bot.okb_service import CREATE_CONTACT, USE_CONTACT_PHONE
        crm = self.use_offline_pharmacy_components()
        for text in ('/deal','Новая аптека',NEW_PHARMACY,'111222333','Учебная фирма','90 987 65 43',
                     CREATE_CONTACT,'Малика','3','3','МИРАБАДСКИЙ','1',
                     'Учебная улица, 1','Пропустить локацию','1','🔔 Добавить напоминание','Презентация',NEXT_DAY):
            self.say(text)
        state=self.store.session(100)
        self.assertEqual(state['step'],'confirm')
        self.assertIn('Фармацевт',self.telegram.messages[-1][1])
        self.assertEqual(len(crm.records['contacts']),3)
        self.api.reject_create=True
        self.say('Подтвердить')
        self.assertEqual(len(crm.records['contacts']),4)
        self.assertNotIn('массово загрузить',self.telegram.messages[-1][1])
        self.api.reject_create=False
        self.say(CONTINUE_DEAL)
        self.assertEqual(self.store.operation(state['request_id'])['status'],'succeeded')
        self.assertEqual(len(crm.records['contacts']),4)
        self.assertEqual(len(crm.items),1)
        self.assertIn('Контакт тоже добавлен.',self.telegram.messages[-1][1])
        self.assertEqual(self.telegram.messages[-1][1].count('Спасибо'), 1)
        self.assertIn('<blockquote>А также, если хотите,',self.telegram.messages[-1][1])
        self.assertIn('>контакты</a>',self.telegram.messages[-1][1])
        self.assertIn('>аптеки</a>',self.telegram.messages[-1][1])


if __name__=='__main__':
    unittest.main()
