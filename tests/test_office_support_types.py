"""Latest office labels, programme-dependent native mapping and lossless fallback."""
import unittest

from datfo_crm_bot.support_types import catalogue, selected, revalidate, TYPE_FIELD, PROGRAM_FIELD, DESCRIPTION_FIELD
from datfo_crm_bot.support_office import snapshot
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.work_inbox import SEND
import test_miniapp_workflows as workflows

LOGIN = 'office:b96ef294-f632-484a-b08b-345cd915f00c'
NEW_AA = 'office:34c36e4e-61b6-4743-83e6-d1dbc6d39065'


class OfficeSupportTests(unittest.TestCase):
    setUpBase = workflows.SupportFormTests.setUpBase
    setUpOriginal = workflows.SupportFormTests.setUp
    tearDown = workflows.SupportFormTests.tearDown
    send_form, token = workflows.SupportFormTests.send_form, workflows.SupportFormTests.token
    choose_recipient, say = workflows.SupportFormTests.choose_recipient, workflows.SupportFormTests.say

    def setUp(self):
        self.setUpOriginal()
        original = self.api.call
        def fields(method,payload):
            result = original(method,payload)
            if method=='crm.item.fields':
                result['result']['fields'][TYPE_FIELD]['items'].append({'ID':'3449','VALUE':'[FA] Не могут войти в F-Apteka'})
                result['result']['fields'][PROGRAM_FIELD]['items'].append({'ID':'3120','VALUE':'ArzonApteka'})
            return result
        self.api.call = fields

    def draft(self, ident=LOGIN, program='3122'):
        self.send_form({'token':self.token(),'mode':'support','pharmacy_kind':'existing','fom_id':'00081',
                        'request_title':'Нужна помощь','request_description':'После входа появляется ошибка. Помогите проверить.',
                        'support_type':ident,'support_program':program})

    def test_same_office_type_resolves_to_correct_native_program(self):
        choices = catalogue(self.api)
        fk = selected({'support_type':LOGIN,'support_program':'3122'},choices)
        fa = selected({'support_type':LOGIN,'support_program':'3116'},choices)
        self.assertEqual((fk['id'],fa['id']),('3333','3449'))
        self.assertEqual(fk['office'],{'id':LOGIN[7:],'title':'Не могут войти в ПО'})
        self.assertFalse(fk['fallback'])
        revalidate(fk,self.api)
        with self.assertRaises(ValueError):
            selected({'support_type':LOGIN,'support_program':''},choices)
        with self.assertRaises(ValueError):
            selected({'support_type':LOGIN,'support_program':'3120'},choices)

    def test_native_creation_preview_and_repeat_keep_exact_office_choice(self):
        self.draft()
        self.choose_recipient()
        self.assertIn('Не могут войти в ПО',self.telegram.messages[-1][1])
        self.say(SEND)
        self.say(SEND)
        row = self.store.listing(200)[0]
        remote = self.api.rows[self.sync.binding(row['id'])['crm_id']]
        self.assertEqual((remote[TYPE_FIELD],remote[PROGRAM_FIELD]),(3333,3122))
        self.assertIn('Тип в новой платформе: Не могут войти в ПО',remote['comments'])
        self.assertNotIn(LOGIN[7:],remote['comments'])
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)

    def test_new_unmatched_type_is_preserved_and_fallback_is_disclosed(self):
        self.draft(NEW_AA,'3120')
        self.choose_recipient()
        self.assertIn('Точного типа в Б24 нет',self.telegram.messages[-1][1])
        self.say(SEND)
        row = self.store.listing(200)[0]
        remote = self.api.rows[self.sync.binding(row['id'])['crm_id']]
        title = 'Проблема с обновлением цен на препараты в платформах АА'
        self.assertEqual(remote[TYPE_FIELD],5890)
        self.assertIn(title,remote[DESCRIPTION_FIELD])
        self.assertIn(row['description'],remote[DESCRIPTION_FIELD])
        self.assertIn(title,remote['comments'])
        self.assertTrue(row['support_details']['fallback'])
        self.assertEqual(row['support_details']['office']['id'],NEW_AA[7:])

    def test_ambiguous_type_is_not_resolved_by_list_order(self):
        choices = catalogue(self.api)
        choices['types'].extend([{'id':'3712','title':'[VIPos] Ошибка стороннего сервиса'},
                                 {'id':'3716','title':'[Simurg] Ошибка стороннего сервиса'}])
        result = selected({'support_type':'office:5395e167-b619-4f50-992b-0ed6491085d9','support_program':'3122'},choices)
        self.assertEqual(result['id'],'5890')
        self.assertTrue(result['fallback'])

    def test_forged_office_id_and_deleted_native_mapping_are_rejected(self):
        with self.assertRaises(ValueError):
            selected({'support_type':'office:00000000-0000-0000-0000-000000000000'},catalogue(self.api))
        details = selected({'support_type':LOGIN,'support_program':'3122'},catalogue(self.api))
        original = self.api.call
        def deleted(method,payload):
            result = original(method,payload)
            if method=='crm.item.fields':
                field = result['result']['fields'][TYPE_FIELD]
                field['items'] = [row for row in field['items'] if row['ID']!='3333']
            return result
        self.api.call = deleted
        with self.assertRaises(ValueError):
            revalidate(details,self.api)

    def test_lost_response_recovers_same_native_mapping_without_duplicate(self):
        self.api.lost_create = True
        self.draft()
        self.choose_recipient()
        self.say(SEND)
        row = self.store.listing(200)[0]
        self.assertEqual(self.sync.binding(row['id'])['state'],'uncertain')
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)

    def test_snapshot_is_only_directory_metadata(self):
        data = snapshot()
        self.assertEqual(len(data['rows']),113)
        self.assertEqual(set(data),{'source','captured_on','scope','rows'})
        self.assertEqual(len({row[0] for row in data['rows']}),113)


if __name__=='__main__':
    unittest.main()
