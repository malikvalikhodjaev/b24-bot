"""Request-contact consent, native links and recovery after interrupted CRM writes."""
from copy import deepcopy
import unittest

import test_miniapp_workflows as workflows
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.communications import InboxError
from datfo_crm_bot.support_contact import collect
from datfo_crm_bot.creation_attribution import bot_creation_ids
from datfo_crm_bot.work_inbox import SEND
from datfo_crm_bot.work_sync import WorkSync
from datfo_crm_bot.simulation import SimulationCrm
from test_intake_forms import payload


class SupportContactTests(unittest.TestCase):
    tearDown = workflows.SupportFormTests.tearDown
    send_form, token = workflows.SupportFormTests.send_form, workflows.SupportFormTests.token
    choose_recipient, say = workflows.SupportFormTests.choose_recipient, workflows.SupportFormTests.say
    setUpBase = workflows.SupportFormTests.setUpBase

    def setUp(self):
        workflows.SupportFormTests.setUp(self)
        self.contacts, self.bindings = {}, {}
        self.lose_contact = self.lose_binding = self.hide_origin = False
        self.fail_contact_metadata=False
        original_call, original_list = self.api.call, self.api.list_all

        def call(method, data=None):
            data = data or {}
            if method == 'crm.item.fields':
                result = original_call(method, data)
                result['result']['fields'].update(contactId={'isReadOnly':False},contactIds={'isReadOnly':False,'isMultiple':True})
                return result
            if not method.startswith('crm.contact.') and method != 'crm.duplicate.findbycomm':
                return original_call(method, data)
            self.api.calls.append((method,deepcopy(data)))
            if method == 'crm.duplicate.findbycomm':
                return {'result':{'CONTACT':[ident for ident,row in self.contacts.items() if self.sync.contacts.has_phone(row,data['values'][0])]}}
            if method == 'crm.contact.fields':
                if self.fail_contact_metadata:
                    raise RemoteError('CONNECTION_ERROR',uncertain=True)
                return {'result':{key:{'isReadOnly':False} for key in ['NAME','PHONE','ASSIGNED_BY_ID','ORIGINATOR_ID','ORIGIN_ID','OPENED']}}
            if method == 'crm.contact.add':
                ident = max(self.contacts,default=600)+1
                self.contacts[ident] = {'ID':ident,**deepcopy(data['fields'])}
                if self.lose_contact:
                    self.lose_contact = False
                    raise RemoteError('CONNECTION_ERROR',uncertain=True)
                return {'result':ident}
            if method == 'crm.contact.get':
                return {'result':deepcopy(self.contacts[data['id']])}
            if method == 'crm.contact.company.items.get':
                return {'result':deepcopy(self.bindings.get(data['id'],[]))}
            if method == 'crm.contact.company.add':
                self.assertEqual(data['fields']['IS_PRIMARY'],'N')
                self.bindings.setdefault(data['id'],[]).append(deepcopy(data['fields']))
                if self.lose_binding:
                    self.lose_binding = False
                    raise RemoteError('CONNECTION_ERROR',uncertain=True)
                return {'result':True}
            raise AssertionError(method)

        def listing(method, data, **kwargs):
            if method != 'crm.contact.list':
                return original_list(method,data,**kwargs)
            self.api.calls.append((method,deepcopy(data)))
            filters = data['filter']
            if '@ID' in filters:
                return [deepcopy(self.contacts[ident]) for ident in filters['@ID']]
            if self.hide_origin:
                return []
            return [deepcopy(row) for row in self.contacts.values() if all(str(row.get(key))==str(value) for key,value in filters.items())]
        self.api.call, self.api.list_all = call, listing

    def data(self, **changes):
        return {'token':self.token(),'mode':'support','pharmacy_kind':'existing','fom_id':'00081',
                'request_title':'Не открывается программа','request_description':'Нужна помощь с подключением',
                'support_contact':'person','support_contact_name':'Азиз','support_contact_phone':'90 123 45 67',**changes}

    def send(self, data):
        self.send_form(data)
        self.choose_recipient()
        self.say(SEND)
        return self.store.listing(200)[0]

    def calls(self, method):
        return [data for name,data in self.api.calls if name==method]

    def remote(self, row):
        return self.api.rows[self.sync.binding(row['id'])['crm_id']]

    def test_contact_is_created_only_after_confirmation_on_creator_and_linked_to_request_company(self):
        self.api.pharmacies[81]['contactIds'] = [55]
        original = deepcopy(self.api.pharmacies)
        self.send_form(self.data(support_contact_name='Азиз <б>'))
        self.assertEqual(self.contacts,{})
        self.choose_recipient()
        self.assertIn('Азиз &lt;б&gt;',self.telegram.messages[-1][1])
        self.say(SEND)
        self.say(SEND)
        row = self.store.listing(200)[0]
        self.assertEqual(row['contact_details'],{'name':'Азиз <б>','phone':'+998901234567'})
        contact = self.contacts[601]
        self.assertEqual(contact['ASSIGNED_BY_ID'],self.primary.registration(200)['bitrix_id'])
        self.assertEqual((contact['NAME'],contact['PHONE'][0]['VALUE']),('Азиз <б>','+998901234567'))
        self.assertEqual(self.remote(row)['contactIds'],[601,55])
        self.assertEqual(self.remote(row)['contactId'],601)
        self.assertIn('Телефон: +998901234567',self.remote(row)['comments'])
        self.assertEqual(self.bindings[601],[{'COMPANY_ID':7,'IS_PRIMARY':'N'}])
        self.assertEqual(self.api.pharmacies,original)
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.assertEqual(len(self.store.listing(200)),1)
        self.assertEqual(bot_creation_ids(self.primary,200)['contact'],[601])
        self.assertEqual(bot_creation_ids(self.primary,300)['contact'],[])

    def test_existing_phone_local_format_is_reused_without_renaming_or_reassigning(self):
        self.contacts[44]={'ID':44,'NAME':'Сохранённое имя','LAST_NAME':'Клиента',
            'PHONE':[{'VALUE':'901234567'}],'ASSIGNED_BY_ID':999}
        self.bindings[44]=[{'COMPANY_ID':9,'IS_PRIMARY':'Y'}]
        row = self.send(self.data())
        self.assertEqual(self.calls('crm.contact.add'),[])
        self.assertEqual(self.contacts[44]['NAME'],'Сохранённое имя')
        self.assertEqual(self.contacts[44]['ASSIGNED_BY_ID'],999)
        self.assertEqual(self.remote(row)['contactId'],44)
        self.assertIn('Сохранённое имя Клиента',self.remote(row)['comments'])
        self.assertEqual(self.bindings[44],[{'COMPANY_ID':9,'IS_PRIMARY':'Y'},{'COMPANY_ID':7,'IS_PRIMARY':'N'}])
        self.assertEqual(bot_creation_ids(self.primary,200)['contact'],[])

    def test_default_choice_ignores_hidden_person_data_and_preserves_source_contacts(self):
        self.api.pharmacies[81].update(contactId=55,contactIds=[55,56])
        row = self.send(self.data(support_contact='pharmacy'))
        self.assertIsNone(row['contact_details'])
        self.assertEqual(self.contacts,{})
        self.assertFalse(any(method.startswith('crm.contact.') for method,_ in self.api.calls))
        self.assertEqual(self.remote(row)['contactIds'],[55,56])
        self.assertEqual(self.remote(row)['contactId'],55)

    def test_bad_person_data_can_be_corrected_without_any_write(self):
        for change in [{'support_contact_name':''},{'support_contact_name':'123'},
                       {'support_contact_phone':''},{'support_contact_phone':123},{'support_contact':'forged'}]:
            data = self.data(**change)
            self.send_form(data)
            self.assertEqual(self.store.session(200)['step'],'web_form')
            self.assertEqual(self.store.listing(200),[])
            self.assertEqual(self.contacts,{})
            self.say('/cancel')
        self.send(self.data())
        self.assertEqual(len(self.calls('crm.contact.add')),1)

    def test_lost_contact_response_is_read_only_until_origin_found_across_restart(self):
        self.lose_contact = self.hide_origin = True
        row = self.send(self.data())
        self.assertEqual(self.sync.binding(row['id'])['state'],'failed')
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.assertEqual(self.calls('crm.item.add'),[])
        self.assertTrue(self.primary.other_inflight('separate-okb-form','phone','+998901234567',phase='contact'))
        self.sync = self.work.sync = WorkSync(self.store,self.live.crm,self.settings)
        self.sync.retry(row['id'],200)
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.assertEqual(self.calls('crm.item.add'),[])
        self.hide_origin = False
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.assertEqual(len(self.calls('crm.item.add')),1)

    def test_lost_company_link_response_recovers_without_duplicate_add(self):
        self.lose_binding = True
        row = self.send(self.data())
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(len(self.calls('crm.contact.company.add')),1)
        self.assertEqual(len(self.calls('crm.contact.add')),1)

    def test_lost_request_response_recovers_the_contact_binding_without_second_request(self):
        self.api.lost_create = True
        row = self.send(self.data())
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(len(self.calls('crm.item.add')),1)
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.assertEqual(self.remote(row)['contactId'],601)

    def test_ambiguous_phone_does_not_create_contact_or_request(self):
        for ident in [41,42]:
            self.contacts[ident]={'ID':ident,'NAME':'Клиент','PHONE':[{'VALUE':'+998901234567'}]}
        row = self.send(self.data())
        self.assertEqual(self.sync.binding(row['id'])['error'],'FOM_CONTACT_AMBIGUOUS')
        self.assertEqual(self.calls('crm.contact.add'),[])
        self.assertEqual(self.calls('crm.item.add'),[])
        self.assertIn('несколько контактов',self.sync.text(row))

    def test_other_uncertain_contact_with_same_phone_blocks_second_creation(self):
        self.lose_contact = self.hide_origin = True
        first = self.send(self.data())
        second = self.send(self.data(request_title='Другая проблема'))
        self.assertEqual(self.sync.binding(second['id'])['error'],'FOM_CONTACT_OTHER_PENDING')
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.hide_origin = False
        self.sync.retry(first['id'],200)
        self.sync.retry(second['id'],200)
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.assertEqual(self.remote(first)['contactId'],self.remote(second)['contactId'])

    def test_changed_contact_phone_after_partial_creation_stops_further_writes(self):
        self.lose_binding = True
        row = self.send(self.data())
        self.contacts[601]['PHONE']=[{'VALUE':'+998909999999'}]
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['error'],'FOM_CONTACT_CHANGED')
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.assertEqual(self.calls('crm.item.add'),[])

    def test_request_id_cannot_be_reused_with_different_contact(self):
        row = self.send(self.data())
        link = self.work.intake.link(200,self.work.intake.sales.lookup_fom('00081'))
        with self.assertRaises(InboxError):
            self.store.create(200,300,link,row['title'],row['description'],row['request_id'],
                              support_details=row['support_details'],contact_details={'name':'Другой','phone':'+998909999999'})

    def test_later_manual_contact_changes_are_preserved_on_stage_update(self):
        row = self.send(self.data())
        self.remote(row).update(contactId=999,contactIds=[999])
        self.store.change(300,row['id'],row['version'],'take')
        self.sync.flush()
        self.assertEqual(self.remote(row)['contactIds'],[999])
        self.assertEqual(len(self.calls('crm.contact.add')),1)

    def test_new_pharmacy_person_creates_one_contact_without_extra_sales_deal(self):
        sales=self.live.bot.sales
        crm=SimulationCrm(self.primary)
        contacts=deepcopy(crm.records['contacts'])
        choices=crm.choices
        crm.choices=lambda key:[{'id':'5919','title':'1С'}] if key=='current_program' else choices(key)
        crm.write_id=int
        sales.form.crm=sales.okb.crm=crm
        original=crm.create_okb_pharmacy
        def create(state,company,manager,contact):
            result=original(state,company,manager,contact)
            self.api.pharmacies[result['id']]={'id':result['id'],'title':state['title'],'companyId':company['id'],'categoryId':17}
            return result
        crm.create_okb_pharmacy=create
        data=payload(crm,token=self.token(),mode='support',pharmacy_kind='new',contact_policy='optional',
            create_contact=False,phone='',support_contact='person',support_contact_name='Азиз',support_contact_phone='901234567',
            request_title='Не открывается программа',request_description='Нужна помощь с подключением')
        row=self.send(data)
        self.assertEqual(crm.records['contacts'],contacts)
        self.assertEqual(len(crm.items),1)
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.assertEqual(len(self.calls('crm.item.add')),1)
        self.assertEqual(self.remote(row)['parentId1034'],row['pharmacy_id'])
        self.assertEqual(self.remote(row)['contactId'],601)
        self.assertEqual(self.bindings[601][0]['COMPANY_ID'],row['pharmacy_company_id'])

    def test_revoked_creator_stops_contact_and_company_writes(self):
        self.api.reject_create=True
        row=self.send(self.data())
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        attempts=len(self.calls('crm.item.add'))
        self.primary.db.execute("UPDATE registrations SET status='rejected' WHERE telegram_id=200")
        self.primary.db.commit()
        self.api.reject_create=False
        # Retry as an approved administrator, after the creator has lost access.
        self.sync.retry(row['id'],100)
        self.assertEqual(self.sync.binding(row['id'])['error'],'FOM_EMPLOYEE_REVOKED')
        self.assertEqual(len(self.calls('crm.contact.add')),1)
        self.assertEqual(len(self.calls('crm.item.add')),attempts)

    def test_metadata_read_failure_is_retryable_before_any_contact_mutation(self):
        self.fail_contact_metadata=True
        row=self.send(self.data())
        self.assertEqual(self.calls('crm.contact.add'),[])
        self.assertIsNone(self.sync.contacts.step(row['id'],'contact'))
        self.fail_contact_metadata=False
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(len(self.calls('crm.contact.add')),1)


if __name__ == '__main__':
    unittest.main()
