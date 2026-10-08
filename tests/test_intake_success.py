from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from datfo_crm_bot.i18n import language_context
from datfo_crm_bot.intake_success import saved_deal
from datfo_crm_bot.okb_service import addition_notices


class IntakeSuccessTests(unittest.TestCase):
    def test_pharmacy_confirmation_is_short_and_has_one_thank_you(self):
        state={'kind':'deal','deadline':'2026-10-08T18:00:00+05:00','next_step':'Позвонить <клиенту>'}
        result={'id':140546,'url':'https://crm.fom.group/crm/deal/details/140546/','activity_id':351449}
        with language_context('ru'):
            text=saved_deal(state,result,pharmacy_added=True,contact_added=True,bulk=addition_notices({'contact_created':True}))
        self.assertEqual(text.count('Спасибо'),1)
        self.assertIn('№140546',text)
        self.assertIn('08.10.2026 · 18:00',text)
        self.assertIn('Позвонить &lt;клиенту&gt;',text)
        self.assertEqual(text.count('<blockquote>'),1)
        self.assertIn('>контакты</a> и <a',text)
        self.assertNotIn('/crm_deal',text)
        self.assertNotIn('Дальнейшие действия',text)

    def test_no_reminder_creates_no_deadline_or_notification_promise(self):
        with language_context('ru'):
            text=saved_deal({'kind':'deal'},{'id':1,'url':'https://crm.fom.group/crm/deal/details/1/'})
        self.assertNotIn('напоминание',text)
        self.assertNotIn('Telegram',text)

    def test_bitrix_utc_deadline_is_shown_in_tashkent_time(self):
        with language_context('ru'):
            text=saved_deal({'kind':'deal','deadline':'2026-10-08T13:00:00+00:00','next_step':'Позвонить'},
                {'id':1,'url':'https://crm.fom.group/crm/deal/details/1/','activity_id':3})
        self.assertIn('08.10.2026 · 18:00',text)
        self.assertNotIn('13:00',text)

    def test_uzbek_uses_deal_term_and_preserves_user_action(self):
        with language_context('uz'):
            text=saved_deal({'kind':'deal','deadline':'2026-10-08T18:00:00+05:00','next_step':'Позвонить Малику'},
                {'id':12,'url':'https://crm.fom.group/crm/deal/details/12/','activity_id':100})
        self.assertIn('Сделка рақами',text)
        self.assertIn('Позвонить Малику',text)
        self.assertNotIn('Дальнейшие',text)


if __name__=='__main__':
    unittest.main()
