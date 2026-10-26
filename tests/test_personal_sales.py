"""Synthetic sale mails: no game interaction and no production DB writes."""
import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from market import Store

ROOT = Path(__file__).resolve().parents[1]


class PersonalSalesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy2(ROOT/'config.json', self.root/'config.json')
        self.now = datetime(2026,10,25,15,tzinfo=timezone.utc)
        self.store = Store(self.root, clock=lambda:self.now)
        self.sale = {'CharacterId':'character-a','CharacterName':'private-name',
            'NotificationType':'SalesNotification','Notification':{'Id':81,'ItemTypeId':'T7_RELIC',
                'LocationId':'3008','UnitPriceSilver':16999,'Amount':120,
                'Expires':'2026-11-24T12:00:00Z','TotalAfterTaxes':1978683.6}}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_silver_not_divided_and_duplicates_do_not_inflate(self):
        self.assertEqual(self.store.ingest('marketnotifications',self.sale)['accepted'],1)
        first=self.store.personal_sales('T7_RELIC','3008')['selected'][0]['first_seen']
        self.now += timedelta(days=1)
        self.assertTrue(self.store.ingest('marketnotifications',self.sale)['duplicate'])
        s=self.store.personal_sales('T7_RELIC','3008')
        self.assertEqual(s['by_market']['T7_RELIC/3008']['total_amount'],120)
        self.assertEqual(s['selected'][0]['price'],16999)
        self.assertEqual(s['selected'][0]['first_seen'],first)
        self.assertNotEqual(s['selected'][0]['last_seen'],first)

    def test_mail_does_not_change_order_history_or_date_statistics(self):
        before=self.store.state('T7_RELIC','3008')
        self.store.ingest('marketnotifications',self.sale)
        after=self.store.state('T7_RELIC','3008')
        for key in ('history','patterns','books','city_extremes','candidates','packets','last_packet'):
            self.assertEqual(before[key],after[key],key)

    def test_conflicting_reopened_mail_is_excluded(self):
        self.store.ingest('marketnotifications',self.sale)
        changed=copy.deepcopy(self.sale);changed['Notification']['Amount']=999
        self.assertTrue(self.store.ingest('marketnotifications',changed)['conflict'])
        s=self.store.personal_sales('T7_RELIC','3008')
        self.assertEqual(s['conflicts'],1)
        self.assertEqual(s['mail_count'],0)
        self.assertFalse(s['by_market'])

    def test_character_identity_separates_mail_ids_without_storing_names(self):
        self.store.ingest('marketnotifications',self.sale)
        other=copy.deepcopy(self.sale);other['CharacterId']='character-b'
        self.store.ingest('marketnotifications',other)
        self.assertEqual(self.store.personal_sales('T7_RELIC','3008')['mail_count'],2)
        rows=[dict(r) for r in self.store.db.execute('SELECT * FROM personal_sales')]
        self.assertNotIn('private-name',json.dumps(rows))
        self.assertNotIn('character-a',json.dumps(rows))
        self.assertNotIn('TotalAfterTaxes',json.dumps(rows))

    def test_highest_price_quantity_and_total(self):
        for mid,price,amount in [(1,16999,120),(2,16999,30),(3,16000,200)]:
            sale=copy.deepcopy(self.sale);sale['Notification'].update(Id=mid,UnitPriceSilver=price,Amount=amount)
            self.store.ingest('marketnotifications',sale)
        market=self.store.personal_sales('T7_RELIC','3008')['by_market']['T7_RELIC/3008']
        self.assertEqual((market['highest_price'],market['highest_price_amount'],market['total_amount']),(16999,150,350))

    def test_expired_or_untracked_mail_not_counted(self):
        for change in [{'NotificationType':'ExpiryNotification'},{'NotificationType':'BuyNotification'}]:
            mail=copy.deepcopy(self.sale);mail.update(change)
            self.assertTrue(self.store.ingest('marketnotifications',mail)['ignored'])
        mail=copy.deepcopy(self.sale);mail['Notification']['ItemTypeId']='T1_UNKNOWN'
        self.assertTrue(self.store.ingest('marketnotifications',mail)['ignored'])
        self.assertEqual(self.store.personal_sales('T7_RELIC','3008')['mail_count'],0)

    def test_invalid_quantity_or_price_is_rejected(self):
        for field,value in [('Amount',0),('Amount',1.5),('UnitPriceSilver',-1),('UnitPriceSilver',True),('Id','')]:
            mail=copy.deepcopy(self.sale);mail['Notification'][field]=value
            with self.assertRaises(ValueError):self.store.ingest('marketnotifications',mail)

    def test_every_mail_outcome_is_logged_for_diagnosis(self):
        self.store.ingest('marketnotifications',self.sale)
        self.store.ingest('marketnotifications',self.sale)
        other=copy.deepcopy(self.sale);other['Notification']['LocationId']='9999';other['Notification']['Id']=2
        self.store.ingest('marketnotifications',other)
        broken=copy.deepcopy(self.sale);broken['Notification']['Amount']=0
        with self.assertRaises(ValueError):self.store.ingest('marketnotifications',broken)
        d=self.store.mail_diagnostics()
        self.assertEqual(d['counts'],{'saved':1,'duplicate':1,'ignored':1,'rejected':1})
        self.assertEqual(d['events'][1]['detail'],'알 수 없는 도시 코드 9999')
        self.assertEqual(d['client_log'],{'file':None,'lines':[]})

    def test_mail_list_covers_every_market_not_only_the_selected_one(self):
        self.store.ingest('marketnotifications',self.sale)
        s=self.store.personal_sales('T8_RUNE','1002')
        self.assertEqual(s['selected_count'],0)
        self.assertEqual(s['recent'][0]['city'],'3008')

    def test_client_log_mail_lines_are_shown_without_colour_codes(self):
        (self.root/'vendor').mkdir()
        (self.root/'vendor/albiondata-client.log').write_text('noise\n\x1b[36mINFO\x1b[0m Mail Info is not valid. Please transition zones\n','utf-8')
        self.assertEqual(self.store.mail_diagnostics()['client_log']['lines'],['INFO Mail Info is not valid. Please transition zones'])

    def test_unknown_identity_and_persistence(self):
        mail=copy.deepcopy(self.sale);mail.pop('CharacterId');mail.pop('CharacterName')
        self.store.ingest('marketnotifications',mail)
        self.store.db.close();self.store=Store(self.root,clock=lambda:self.now)
        self.assertEqual(self.store.personal_sales('T7_RELIC','3008')['unknown_identity'],1)


if __name__=='__main__':unittest.main()
