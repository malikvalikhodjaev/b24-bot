"""Export only choice labels/IDs; never contacts, companies or credentials."""
from dataclasses import replace
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from datfo_crm_bot.config import ROOT,load_env,connection_from_env
from datfo_crm_bot.api import Bitrix
from datfo_crm_bot.demo import demo_config
from datfo_crm_bot.okb_crm import OkbCrm,OkbSettings
from datfo_crm_bot.sales_options import discussion_choices
from datfo_crm_bot.support_types import picker_reference

load_env(ROOT/'.env'); token,hook=connection_from_env(live=True)
settings=OkbSettings.load()
config=replace(demo_config(ROOT/'data/unused-reference.sqlite3'),token=token,webhook=hook,targets={'pharmacy':settings.pharmacy})
crm=OkbCrm(config,Bitrix(hook),settings)
regions=crm.choices('business_region')
data={'regions':regions,'cities':{row['id']:crm.cities_for_region(row['id']) for row in regions},
      'programs':crm.choices('current_program'), 'discussion_programs':discussion_choices(crm.api),
      'positions':[{key:row[key] for key in ('id','title')} for row in crm.contact_positions()],
      'support':picker_reference(crm.api)}
(ROOT/'webapp/reference.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'regions':len(regions),'cities':sum(len(rows) for rows in data['cities'].values()),'programs':len(data['programs']),'positions':len(data['positions'])}))
