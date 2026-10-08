"""Read only support enum metadata; preserve the other published Mini App directories."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datfo_crm_bot.api import Bitrix
from datfo_crm_bot.config import ROOT, connection_from_env, load_env
from datfo_crm_bot.support_types import picker_reference


def main():
    load_env(ROOT / '.env')
    _, hook = connection_from_env(live=True)
    path = ROOT / 'webapp/reference.json'
    original = json.loads(path.read_text(encoding='utf-8'))
    support = picker_reference(Bitrix(hook))
    result = {**original, 'support': support}
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'types': len(support['types']), 'programs': len(support['programs']),
                      'unknown_first': support['types'][0]['id'] == support['unknown_id'],
                      'other_directories_preserved': all(result[key] == value for key, value in original.items() if key != 'support')}))


if __name__ == '__main__':
    main()
