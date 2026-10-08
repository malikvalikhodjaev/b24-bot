"""Mini App pharmacy intake for FOM requests, without an extra sales deal."""
from uuid import uuid4
from html import escape

from .communications import InboxError
from .i18n import tr
from .input_forms import form_keyboard, launch_matches, consume_launch
from .intake_ui import phrase as intake
from .intake_preview import pharmacy_blocks
from .service import utc_text
from .api import RemoteError
from .support_types import catalogue, selected, revalidate
from .support_contact import collect as collect_contact, preview as contact_preview


class SupportIntake:
    def __init__(self, inbox):
        self.inbox = inbox
        self.store = inbox.store
        self.primary = inbox.registration.store
        self.sales = inbox.live.bot.sales

    def start(self, user, token=None):
        if self.store.role(user) != 'fom_sales':
            raise InboxError(tr('Заявку отправляет менеджер продаж ФОМ.'))
        self.sales.bot.config.users[user]=self.inbox.registration.user(user)
        state = self.primary.session(user)
        if self.primary.unfinished(user) or (state and state.get('step') not in {'done','stats_scope','stats_period'}):
            raise InboxError(tr('Сначала завершите текущую форму или проверьте сохранение через /pending.'))
        if self.store.session(user):
            self.inbox.prompt(user,self.store.session(user))
            return
        if token is not None and not launch_matches(self.primary,user,'support',token):
            raise InboxError(tr('Эта форма уже закрыта. Откройте новую форму через меню.'))
        draft = {'step':'web_form','request_id':token or 'fom-request-'+uuid4().hex,'form_mode':'support','web_intake':True}
        self.store.set_session(user,draft)
        if token is not None:
            consume_launch(self.primary,user,'support',token)
        else:
            self.prompt(user,draft)

    def receive(self, user, data):
        draft = self.store.session(user)
        if not draft:
            self.start(user,data.get('token'))
            draft = self.store.session(user)
        if not draft or draft['step'] != 'web_form' or draft['request_id'] != data.get('token'):
            raise InboxError(tr('Эта форма уже закрыта. Откройте новую форму через меню.'))
        for key,minimum,maximum in [('request_title',3,100),('request_description',5,1200)]:
            value=data.get(key)
            if not isinstance(value,str) or not minimum<=len(value.strip())<=maximum:
                raise InboxError(tr('Заполните тему (3–100 символов) и описание (5–1200 символов).'))
        if data.get('pharmacy_kind') not in {'new','existing'}:
            raise InboxError(tr('Выберите новую или существующую аптеку.'))
        contact_details = collect_contact(data)
        self.inbox.say(user,intake('processing_support'),cache=False)
        try:
            details = selected(data, catalogue(self.inbox.live.crm.api))
        except RemoteError as exc:
            raise InboxError(tr('Не удалось проверить список типов обращения в Б24. Попробуйте ещё раз немного позже.')) from exc
        draft.update(title=data['request_title'].strip(),description=data['request_description'].strip())
        draft['support_details'] = details
        draft['contact_details'] = contact_details
        if data.get('pharmacy_kind')=='existing':
            draft.update(pharmacy=self.sales.lookup_fom(data.get('fom_id')),step='recipient')
            if draft['pharmacy']['company_id']:
                draft['company']=self.sales.company(draft['pharmacy']['company_id'])
        elif data.get('pharmacy_kind')=='new':
            child={'kind':'pharmacy','workflow':'okb','request_id':draft['request_id']+'-pharmacy','step':'okb_inn'}
            response,keyboard,child=self.sales.form.route_payload(user,data,child)
            draft.update(pharmacy_form=child,step='pharmacy_form',child_prompt=response,child_keyboard=keyboard)
            self.store.set_session(user,draft)
            if not child.get('collected'):
                self.inbox.say(user,response,keyboard)
                return
            draft['step']='recipient'
        else:
            raise InboxError(tr('Выберите новую или существующую аптеку.'))
        self.store.set_session(user,draft)
        self.inbox.prompt(user,draft)

    def prompt(self, user, draft, error=None):
        if draft['step']=='web_form':
            self.inbox.say(user,((escape(str(error))+'\n\n') if error else '')+intake('form_only'),
                form_keyboard(draft,self.primary,user))
        else:
            response,keyboard=draft.get('child_prompt',''),draft.get('child_keyboard',[])
            self.inbox.say(user,((escape(str(error))+'\n\n') if error else '')+response,keyboard)

    def route(self, user, text, draft):
        if draft['step']=='web_form':
            self.prompt(user,draft)
            return
        response,keyboard,child=self.sales.form.route(user,text,draft['pharmacy_form'])
        draft['pharmacy_form']=child
        if child.get('collected'):
            draft['step']='recipient'
            self.store.set_session(user,draft)
            self.inbox.prompt(user,draft)
        else:
            draft.update(child_prompt=response,child_keyboard=keyboard)
            self.store.set_session(user,draft)
            self.inbox.say(user,response,keyboard)

    def preview(self, user, draft):
        contact = contact_preview(draft.get('contact_details'))
        if draft.get('pharmacy_form'):
            text = pharmacy_blocks(draft['pharmacy_form'],self.inbox.registration.user(user).name)
            return text + ('\n\n'+contact if contact else '')
        pharmacy=draft['pharmacy']
        return ('🏪 <b>'+escape(pharmacy['title'])+'</b>\nFOM ID: '+escape(pharmacy['fom_id'])
            +'\n\n'+intake('company_section')+'\n'+escape((draft.get('company') or {}).get('title') or intake('not_set'))
            + ('\n\n'+contact if contact else ''))

    def link(self, user, pharmacy):
        row=self.sales.pharmacy(pharmacy['id'])
        if int(row.get('companyId') or 0)!=pharmacy['company_id']:
            raise InboxError(tr('Компания аптеки изменилась. Проверьте аптеку заново.'))
        if pharmacy.get('fom_id') and self.sales.lookup_fom(pharmacy['fom_id'])['id']!=pharmacy['id']:
            raise InboxError(intake('fom_not_found'))
        url=self.inbox.live.crm.portal+f"/crm/type/1034/details/{pharmacy['id']}/"
        with self.store.db:
            self.store.db.execute('''INSERT INTO fom_pharmacy_references(user_id,pharmacy_id,company_id,url,title)
                VALUES(?,?,?,?,?) ON CONFLICT(user_id,pharmacy_id) DO UPDATE SET company_id=excluded.company_id,
                url=excluded.url,title=excluded.title,checked_at=CURRENT_TIMESTAMP''',
                (user,pharmacy['id'],pharmacy['company_id'],url,str(row['title'])))
        return {'id':0,'pharmacy_id':pharmacy['id'],'company_id':pharmacy['company_id'],'link_kind':'pharmacy',
                'url':url,'title':str(row['title']),'request_id':f"pharmacy:{user}:{pharmacy['id']}"}

    def submit(self, user, draft):
        try:
            revalidate(draft.get('support_details'), self.inbox.live.crm.api)
        except RemoteError as exc:
            raise InboxError(tr('Не удалось проверить список типов обращения в Б24. Попробуйте ещё раз немного позже.')) from exc
        if draft.get('pharmacy_form'):
            child=draft['pharmacy_form']
            self.primary.prepare(user,child,utc_text(self.sales.bot.clock()))
            response,keyboard,_=self.sales.okb.submit(user,child)
            operation=self.primary.operation(child['request_id'])
            if operation['status'] not in {'succeeded','existing'}:
                from .work_inbox import SEND,CANCEL
                self.inbox.say(user,response,[[SEND],[CANCEL]])
                return None
            result=operation['result']
            pharmacy={'id':result['id'],'company_id':result['company_id']}
        else:
            pharmacy=draft['pharmacy']
        return self.link(user,pharmacy)
