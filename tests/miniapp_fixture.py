"""Hermetic publication fixture, independent of the operator's local data."""
import json
from pathlib import Path
from unittest.mock import patch


def published_form(case, folder):
    root = Path(folder)
    data = root / 'data'
    data.mkdir(exist_ok=True)
    (data / 'miniapp-publication.json').write_text(json.dumps({
        'verified': True, 'url': 'https://fom-analytics.uz/bot-form/'}), encoding='utf-8')
    fixture = patch('datfo_crm_bot.input_forms.ROOT', root)
    fixture.start()
    case.addCleanup(fixture.stop)
