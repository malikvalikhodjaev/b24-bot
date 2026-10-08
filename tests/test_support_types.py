"""Support type integrity, native fields, recovery and human-edited CRM cards."""
from copy import deepcopy
import unittest

from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.communications import InboxError
from datfo_crm_bot.support_types import catalogue, selected, picker_reference, TYPE_FIELD, PROGRAM_FIELD, DESCRIPTION_FIELD
from datfo_crm_bot.work_inbox import SEND
import test_miniapp_workflows as workflows


class SupportTypesTests(unittest.TestCase):
    setUpBase = workflows.SupportFormTests.setUpBase
    setUp, tearDown = workflows.SupportFormTests.setUp, workflows.SupportFormTests.tearDown
    send_form, token = workflows.SupportFormTests.send_form, workflows.SupportFormTests.token
    choose_recipient, say = workflows.SupportFormTests.choose_recipient, workflows.SupportFormTests.say

    def draft(self, **changes):
        data = {'token':self.token(), 'mode':'support', 'pharmacy_kind':'existing', 'fom_id':'00081',
                'support_type':'3333', 'support_program':'3122', 'request_title':'Не входит в кассу',
                'request_description':'После ввода пароля вход не происходит. Помогите подключить кассу.', **changes}
        self.send_form(data)
        return data

    def request(self, **changes):
        self.draft(**changes)
        self.choose_recipient()
        self.say(SEND)
        return self.store.listing(200)[0]

    def test_native_type_program_and_description_saved_and_visible_after_handoff(self):
        row = self.request()
        remote = self.api.rows[self.sync.binding(row['id'])['crm_id']]
        self.assertEqual(remote[TYPE_FIELD],3333)
        self.assertEqual(remote[PROGRAM_FIELD],3122)
        self.assertEqual(remote[DESCRIPTION_FIELD],row['description'])
        self.assertIn(row['description'],remote['comments'])
        self.assertIn('Тип обращения: [FK] Не могут войти в F-Kassa',remote['comments'])
        self.assertEqual(row['support_details']['program'], {'id':'3122','title':'F-Kassa'})
        self.assertIn('[FK] Не могут войти в F-Kassa',self.telegram.calls[-1][1]['text'])
        # Native employees may refine the type later. Stage/owner sync must preserve their edit.
        remote[TYPE_FIELD] = 5890
        working = self.store.change(300,row['id'],0,'take')['ticket']
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(remote[TYPE_FIELD],5890)
        updates = [payload['fields'] for method,payload in self.api.calls if method=='crm.item.update']
        self.assertTrue(updates)
        self.assertTrue(all(TYPE_FIELD not in fields and PROGRAM_FIELD not in fields for fields in updates))
        self.assertEqual(working['support_details'],row['support_details'])

    def test_unknown_type_does_not_require_program_and_keeps_free_comment(self):
        row = self.request(support_type='5890',support_program='',request_description='Неизвестная ошибка. Экран зависает после открытия программы.')
        remote = self.api.rows[self.sync.binding(row['id'])['crm_id']]
        self.assertEqual(remote[TYPE_FIELD],5890)
        self.assertNotIn(PROGRAM_FIELD,remote)
        self.assertIn('Экран зависает',remote['comments'])
        self.assertIsNone(row['support_details']['program'])

    def test_missing_forged_or_deleted_choices_never_create_request(self):
        for changes in [{'support_type':''}, {'support_type':'999999'}, {'support_type':True},
                        {'support_type':['5890']}, {'support_program':'999999'}, {'support_program':True}]:
            with self.subTest(changes=changes):
                self.draft(**changes)
                self.assertEqual(self.store.session(200)['step'],'web_form')
                self.assertEqual(self.store.listing(200),[])
        self.assertFalse(any(method=='crm.item.add' for method,_ in self.api.calls))

    def test_metadata_is_checked_again_before_saving_and_draft_survives(self):
        self.draft()
        self.choose_recipient()
        original = self.api.call
        def changed(method,payload):
            result = original(method,payload)
            if method=='crm.item.fields':
                result['result']['fields'][TYPE_FIELD]['items'] = [{'ID':'5890','VALUE':'Неопознанная ошибка: нужно уточнить'}]
            return result
        self.api.call = changed
        self.say(SEND)
        self.assertEqual(self.store.listing(200),[])
        self.assertEqual(self.store.session(200)['step'],'confirm')
        self.assertIn('Список типов обращения изменился',self.telegram.messages[-1][1])
        self.assertFalse(any(method=='crm.item.add' for method,_ in self.api.calls))

    def test_lost_create_response_recovers_same_type_without_duplicate(self):
        self.api.lost_create = True
        row = self.request()
        self.assertEqual(self.sync.binding(row['id'])['state'],'uncertain')
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)
        self.assertEqual(self.api.rows[self.sync.binding(row['id'])['crm_id']][TYPE_FIELD],3333)

    def test_lost_response_with_wrong_native_type_is_not_falsely_confirmed_or_recreated(self):
        self.api.lost_create = True
        row = self.request()
        created = next(record for record in self.api.rows.values() if record.get('originId')==row['request_id'])
        created[TYPE_FIELD] = 5890
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['state'],'uncertain')
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)

    def test_same_request_id_cannot_change_support_type(self):
        row = self.request()
        pharmacy = self.work.intake.sales.lookup_fom('00081')
        link = self.work.intake.link(200,pharmacy)
        self.assertEqual(self.store.create(200,300,link,row['title'],row['description'],row['request_id'],
                         support_details=row['support_details'])['id'],row['id'])
        with self.assertRaises(InboxError):
            self.store.create(200,300,link,row['title'],row['description'],row['request_id'],
                              support_details={**row['support_details'],'id':'5890'})

    def test_catalogue_uses_native_ids_ignores_client_labels_and_groups_only_explicit_products(self):
        choices = catalogue(self.api)
        self.assertEqual(choices['types'][0]['id'],'5890')
        details = selected({'support_type':'3333','support_program':'3122','support_type_title':'Forged'}, choices)
        self.assertEqual(details['title'],'[FK] Не могут войти в F-Kassa')
        reference = picker_reference(self.api)
        rows = {row['id']:row for row in reference['types']}
        self.assertEqual(rows['5890']['program_ids'],[])
        self.assertEqual(rows['office:b96ef294-f632-484a-b08b-345cd915f00c']['program_ids'],['3122'])
        self.assertEqual(rows['office:9ddd84d7-b5f2-45f8-86d2-da4c1552d6ec']['program_ids'],['3116'])

    def test_changed_field_schema_and_duplicate_ids_are_rejected(self):
        original = self.api.call
        for mutation in ['read_only','multi','duplicates']:
            def changed(method,payload):
                result = original(method,payload)
                if method=='crm.item.fields':
                    field = result['result']['fields'][TYPE_FIELD]
                    if mutation=='read_only':field['isReadOnly']=True
                    elif mutation=='multi':field['isMultiple']=True
                    else:field['items'].append(deepcopy(field['items'][0]))
                return result
            self.api.call = changed
            with self.subTest(mutation=mutation),self.assertRaises(RemoteError):
                catalogue(self.api)


if __name__=='__main__':
    unittest.main()
