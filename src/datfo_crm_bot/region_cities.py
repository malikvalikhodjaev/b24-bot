"""Explicit local relationships between existing Bitrix enumeration IDs."""
from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT, ConfigError


class RegionCities:
    def __init__(self, path: Path | None = None):
        try:
            data = json.loads((path or ROOT / 'okb-region-cities.json').read_text(encoding='utf-8'))
            self.regions = {str(row['id']): row for row in data['regions']}
            self.city_ids = {key: [str(city['id']) for city in row['cities']] for key, row in self.regions.items()}
            assigned = [city for cities in self.city_ids.values() for city in cities]
            if not self.regions or len(assigned) != len(set(assigned)) or any(not cities for cities in self.city_ids.values()):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise ConfigError('ОКБ: не удалось прочитать связи городов с регионами') from None

    def choices(self, region_id, cities):
        allowed = self.city_ids.get(str(region_id))
        if allowed is None:
            raise ConfigError('ОКБ: для выбранного региона ещё не настроены города')
        current = {str(city['id']): city for city in cities}
        if any(city not in current for city in allowed):
            raise ConfigError('ОКБ: справочник городов изменился; обновите связи регионов')
        return [current[city] for city in allowed]

    def validate(self, state):
        if state and state.get('city'):
            allowed = self.city_ids.get(str(state.get('business_region', {}).get('id')), [])
            if str(state['city']['id']) not in allowed:
                raise ConfigError('ОКБ: выбранный город не относится к бизнес-региону')
