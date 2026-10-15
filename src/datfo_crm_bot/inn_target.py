"""Revalidate submitted INN bindings and journal company-only creation."""
from .config import ConfigError, positive_id
from .i18n import tr
from .inn_lookup import inn_value, FOM_ID_FIELD


def selection(sales, data):
    inn = inn_value(data.get('inn'))
    companies = sales.pharmacy_crm.companies(inn)
    company_id = data.get('company_id')
    company = None
    if company_id not in (None, '', 0):
        ident = positive_id(company_id, 'Компания')
        company = next((row for row in companies if row['id'] == ident), None)
        if not company:
            raise ConfigError(tr('Фирма не относится к этому ИНН. Повторите поиск.'))
    elif companies:
        raise ConfigError(tr('По ИНН есть компания. Выберите её из списка.'))
    elif data.get('create_company') is not True:
        raise ConfigError(tr('Подтвердите создание новой фирмы.'))
    name = company['title'] if company else data.get('company_name')
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 120:
        raise ConfigError(tr('Укажите наименование фирмы (2–120 символов).'))
    result = {'inn':inn,'company':company,'company_name':name.strip(),
              'new_company':not bool(company),'company_consent':data.get('create_company') is True,'inn_intake':True}
    if data.get('pharmacy_kind') == 'existing':
        if not company:
            raise ConfigError(tr('Выберите аптеку существующей фирмы.'))
        row = sales.pharmacy(positive_id(data.get('pharmacy_id'), 'Аптека'))
        if int(row.get('companyId') or 0) != company['id']:
            raise ConfigError(tr('Аптека не относится к выбранной фирме. Повторите поиск.'))
        fields = sales.pharmacy_crm.settings.pharmacy.fields
        result['pharmacy'] = {'id':int(row['id']),'title':str(row['title']),
            'company_id':company['id'],'fom_id':str(row.get(FOM_ID_FIELD) or ''),
            'address':str(row.get(fields['address']) or ''),'phone':str(row.get(fields['phone']) or ''),
            'status':sales.pharmacy_status(row)}
        for key in ('business_region','city','current_program'):
            value = row.get(fields.get(key))
            if isinstance(value,list):
                value = value[0] if value else None
            if value not in (None,'',0):
                choice = next((item for item in sales.pharmacy_crm.choices(key) if item['id']==str(value)),None)
                if choice:
                    result['pharmacy'][key] = choice
    return result


def check_company(sales, state):
    company = state.get('company')
    if not company or not any(row['id'] == company['id'] for row in sales.pharmacy_crm.companies(state['inn'])):
        raise ConfigError(tr('Связь ИНН с компанией изменилась. Повторите поиск.'))
    return sales.company(company['id'])


def save_company(sales, user, state, *, verify_only=False):
    """No pharmacy validation/add: only company and requisite, with existing recovery phases."""
    if state.get('company'):
        return check_company(sales, state)
    if not state.get('new_company') or state.get('company_consent') is not True:
        raise ConfigError(tr('Подтвердите создание новой фирмы.'))
    crm, store = sales.pharmacy_crm, sales.store
    request = state['request_id']
    if store.other_inflight(request, 'inn', state['inn']):
        raise ConfigError(tr('Создание фирмы с этим ИНН ещё не подтверждено в другом запросе. Сначала проверьте его.'))
    if not store.operation_step(request, 'company') and not crm.find_company_request(request) and crm.companies(state['inn']):
        raise ConfigError(tr('По ИНН появилась компания. Выберите её заново.'))
    # Recheck the write schema; do not require pharmacy permissions for a company-only target.
    for method, required in (('crm.company.fields', {'TITLE','ASSIGNED_BY_ID','ORIGINATOR_ID','ORIGIN_ID','OPENED'}),
                             ('crm.requisite.fields', {'ENTITY_TYPE_ID','ENTITY_ID','PRESET_ID','NAME','RQ_INN','RQ_COMPANY_NAME','XML_ID','ACTIVE'})):
        fields = crm.api.call(method)['result']
        if not isinstance(fields, dict) or any(not isinstance(fields.get(key),dict) or fields[key].get('isReadOnly') for key in required):
            raise ConfigError(tr('Недоступны поля компании или реквизитов в Б24.'))
    presets = crm.api.list_all('crm.requisite.preset.list', {'filter':{'ID':crm.settings.preset_id}})
    if not any(str(row.get('ID')) == str(crm.settings.preset_id) and str(row.get('ENTITY_TYPE_ID')) == '8' for row in presets):
        raise ConfigError(tr('Шаблон реквизитов фирмы недоступен.'))
    fields = crm.api.call('crm.requisite.preset.field.list', {'preset':{'ID':crm.settings.preset_id}})['result']
    if not isinstance(fields,list) or not {'RQ_INN','RQ_COMPANY_NAME'}.issubset({row.get('FIELD_NAME') for row in fields}):
        raise ConfigError(tr('В шаблоне реквизитов нет ИНН или наименования фирмы.'))
    company = sales.okb.phase(state, 'company', lambda:crm.find_company_request(request),
        lambda:crm.create_company(state, sales.bot.config.users[user].bitrix_id), verify_only=verify_only)
    sales.okb.phase(state, 'requisite', lambda:crm.find_requisite(company['id'],state['inn']),
        lambda:crm.create_requisite(state,company), verify_only=verify_only)
    return company
