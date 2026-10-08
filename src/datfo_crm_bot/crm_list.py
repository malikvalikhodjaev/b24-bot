"""One compact, paged list; actions open for the chosen record."""
import html
from .api import RemoteError
from .config import ConfigError
from .i18n import tr
from .role_content import TEXTS

PAGE_SIZE=8


def deliver(live, user, text, buttons, message_id=None):
    params={'chat_id':user,'text':text,'parse_mode':'HTML','reply_markup':{'inline_keyboard':buttons},
            'link_preview_options':{'is_disabled':True}}
    if isinstance(message_id,int) and message_id>0:
        params['message_id']=message_id
        return live.telegram.call('editMessageText',params)
    return live.telegram.call('sendMessage',params)


def hub(live, user, message_id=None):
    buttons=[]
    if live.registration.sales_allowed(user):
        buttons.append([{'text':tr(TEXTS['deals']['ru']), 'callback_data':'ld:list:deal:0'}])
    buttons.append([{'text':tr(TEXTS['requests']['ru']), 'callback_data':'ld:list:support:0'}])
    return deliver(live,user,tr(TEXTS['tasks_heading']['ru'])+'\n\n'+tr(TEXTS['tasks_hint']['ru']),buttons,message_id)


def show(live,user,member,page=0,message_id=None,scope='deal'):
    if scope not in {'deal','support'}:
        raise ConfigError(tr('Нотўғри тугма'))
    if scope=='deal' and not live.registration.sales_allowed(user):
        raise ConfigError(tr(TEXTS['sales_only']['ru']))
    rows=live.registration.store.db.execute("""SELECT request_id FROM operations WHERE user_id=?
        AND kind IN ('deal','support') AND status IN ('succeeded','created') AND result IS NOT NULL
        ORDER BY created_at DESC""",(user,))
    entries=[]
    seen=set()
    for raw in rows:
        operation=live.registration.store.operation(raw['request_id'])
        if operation['kind']!=scope or operation['value'].get('workflow')!='live_deal' or not operation['result'].get('id'):
            continue
        ident=operation['result']['id']
        if ident in seen:
            continue
        seen.add(ident)
        entries.append({'kind':'deal','operation':operation,'at':operation['created_at']})
    tickets=live.work_inbox.store.listing(user,limit=None) if live.work_inbox and scope=='support' else []
    tickets=[row for row in tickets if row['creator']==user or row['owner']==user]
    entries += [{'kind':'ticket','ticket':row,'at':row['created_at']} for row in tickets]
    entries.sort(key=lambda row:row['at'],reverse=True)
    pages=max(1,(len(entries)+PAGE_SIZE-1)//PAGE_SIZE)
    page=max(0,min(page,pages-1))
    text='<b>'+tr(TEXTS['deals' if scope=='deal' else 'requests']['ru'])+'</b>\n'
    text+=tr('Через бот: ')+str(len(entries))+tr(' · Страница ')+str(page+1)+'/'+str(pages)+'\n\n'
    buttons=[]
    stage_cache={}
    for index,entry in enumerate(entries[page*PAGE_SIZE:(page+1)*PAGE_SIZE],page*PAGE_SIZE+1):
        try:
            if entry['kind']=='deal':
                operation=entry['operation']
                row=live.item_for(operation,member,user)
                category=int(row.get('categoryId') or 0)
                if category not in stage_cache:
                    stage_cache[category]={stage['id']:stage['title'] for stage in live.crm.stages(category)}
                stages=stage_cache[category]
                title=str(row.get('title') or '')
                ident=int(row['id'])
                stage=stages.get(row.get('stageId'),tr('Стадия недоступна'))
                url=operation['result']['url']
                callback='ld:card:'+str(ident)
                label='📋 #'+str(ident)
            else:
                row=entry['ticket']
                from .work_inbox import STATUSES
                title=row['title']
                label=tr('🛠 Заявка #')+str(row['id'])
                stage=tr(STATUSES[row['status']])
                binding=live.work_inbox.sync.binding(row['id']) if live.work_inbox.sync else None
                url=(live.crm.portal+'/crm/deal/details/'+str(binding['crm_id'])+'/' if binding and binding.get('crm_id') else row['deal_url'])
                callback=f"wr:card:{row['id']}:{row['version']}"
            title=title[:46]+('…' if len(title)>46 else '')
            text+=str(index)+'. '+label+' · <a href="'+html.escape(url,quote=True)+'">'+html.escape(title)+'</a>\n'
            text+='   '+html.escape(stage)+'\n\n'
            buttons.append([{'text':str(index)+'. '+title[:38],'callback_data':callback}])
        except (RemoteError,ConfigError):
            text+=str(index)+'. '+tr('Карточка сейчас недоступна')+'\n\n'
    if not entries:
        text+=tr(TEXTS['empty_deals' if scope=='deal' else 'empty_requests']['ru'])+'\n'
    navigation=[]
    if page:
        navigation.append({'text':'⬅️','callback_data':'ld:list:'+scope+':'+str(page-1)})
    if page+1<pages:
        navigation.append({'text':'➡️','callback_data':'ld:list:'+scope+':'+str(page+1)})
    if navigation:
        buttons.append(navigation)
    buttons.append([{'text':tr(TEXTS['back_tasks']['ru']),'callback_data':'ld:hub'}])
    text+=tr('Выберите строку ниже, чтобы посмотреть карточку и действия.')
    return deliver(live,user,text,buttons,message_id)
