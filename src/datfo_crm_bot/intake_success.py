"""Short confirmation with one thank-you and an optional reminder deadline."""
from datetime import datetime
from zoneinfo import ZoneInfo
from .intake_ui import phrase
from .service import esc


def saved_deal(state, result, *, pharmacy_added=False, contact_added=False, bulk='', zone=None):
    request = state.get('kind') == 'support'
    thanks = 'thanks_pharmacy' if pharmacy_added else 'thanks_request' if request else 'thanks_deal'
    text = phrase(thanks) + '\n\n'
    text += phrase('request_number' if request else 'deal_number') + '<b>№' + str(result['id']) + '</b>\n'
    text += '<a href="' + esc(result['url']) + '">' + phrase('open_request' if request else 'open_deal') + '</a>'
    if contact_added:
        text += '\n' + phrase('contact_saved_short')
    if result.get('activity_id'):
        due = datetime.fromisoformat(state['deadline']).astimezone(zone or ZoneInfo('Asia/Tashkent')).strftime('%d.%m.%Y · %H:%M')
        text += '\n\n🔔 ' + phrase('reminder_added_on') + '<b>' + due + '</b>\n'
        title = state.get('title') or '№' + str(result['id'])
        deal = '<a href="' + esc(result['url']) + '">' + esc(title) + '</a>'
        text += phrase('reminder_request_subject' if request else 'reminder_subject').format(
            action=esc(state['next_step']), deal=deal)
        text += '\n' + phrase('reminder_here')
    if bulk:
        text += '\n\n' + bulk
    return text
