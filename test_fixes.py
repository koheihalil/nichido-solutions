import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# リポジトリルートを「seal_types.json がある場所」で特定する。
# tests/ サブディレクトリ配置でも、リポジトリ直下への平置きでも動く
_here = Path(__file__).resolve()
_cands = [_here.parent.parent, _here.parent, Path.cwd()]
ROOT = next((c for c in _cands if (c / "seal_types.json").exists()), _here.parent)
sys.path.insert(0, str(ROOT))

import observe_daily as od
import audit_price_observations as apo
import bargain_finder as bf
import seal_eval as se
import seal_pilot as sp


class NoDomPage:
    def locator(self, *_args, **_kwargs):
        raise RuntimeError('no DOM in unit test')


class TitleHygieneTests(unittest.TestCase):
    def test_search_spam_not_used_for_artist(self):
        # 「渡辺省亭/紅葉鳥(検索＝…川瀬巴水…)」を巴水と誤判定していた事故
        t = "即決！真作 渡辺省亭/紅葉鳥の木版画(検索＝新版画 川瀬巴水 吉田博)"
        self.assertIsNone(bf.artist_from_title(t))

    def test_real_artist_before_spam_is_kept(self):
        t = "笠松紫浪 犬吠岬 検索:川瀬巴水"
        self.assertEqual(bf.artist_from_title(t), "笠松紫浪")

    def test_imitation_words_are_replica(self):
        # 模作/摸作/模写はコピー品。掘り出し物にしてはいけない
        for t in ("即決！真作 川瀬巴水の模作の版画①", "巴水 摸作", "巴水の模写"):
            self.assertTrue(bf.has_any(t, bf.REPLICA_WORDS), t)
        self.assertFalse(bf.has_any("【真作】川瀬巴水 増上寺の雪", bf.REPLICA_WORDS))


class PriceExtractionTests(unittest.TestCase):
    def test_current_price_beats_buyout_and_other_amounts(self):
        body = '''送料無料 6,900円\n即決価格 39,800円\n現在価格 9,000円\nヤフーフリマ 9,800円'''
        got = od._extract_prices(NoDomPage(), body, ended=False)
        self.assertEqual(got['price'], 9000.0)
        # ★即決はJSON-LD由来のみ(2026-07-27)。本文の即決39800は関連商品の
        #   可能性があるので取らない。現在価格9000がノイズに勝つ性質は不変
        self.assertIsNone(got['buyout'])
        self.assertEqual(got['role'], 'current')

    def test_buyout_below_current_is_rejected(self):
        # 即決<=現在は関連商品の誤爆。増上寺(現在20000)に関連商品即決2980が
        # 紛れても弾く。ヤフオクの仕組み上、即決は現在価格より高い
        body = ("関連 川瀬巴水 即決\n2,980円\n……\n"
                "現在\n20,000円\n（税0円）")
        got = od._extract_prices(NoDomPage(), body, ended=False)
        self.assertIsNone(got['buyout'])  # 2980は現在20000以下なので却下

    def test_tax_marked_buyout_ignores_related_item(self):
        # 実測(2026-07-30, 12件): メイン商品の価格には「(税N円)」が付き、
        # 関連商品には付かない。x1220347645: 現在17,700+即決22,000が正、
        # 先頭の関連商品48,000は誤り
        body = ("関連 即決\n48,000円\n……\n"
                "現在\n17,700円\n（税0円）\n即決\n22,000円\n（税0円）")
        self.assertEqual(od._tax_marked(body, ("即決",)), 22000.0)
        self.assertEqual(od._tax_marked(body, ("現在",)), 17700.0)

    def test_tax_marked_none_when_only_related_has_buyout(self):
        # 関連商品にしか即決が無いページ(f1238042235)は即決なしと判定する
        body = "関連 即決\n4,500円\n……\n現在\n15,500円\n（税0円）"
        self.assertIsNone(od._tax_marked(body, ("即決",)))

    def test_ended_page_keeps_current_label(self):
        # 終了ページは「現在」ラベルのまま最終価格を出す(SOLDのf1238042235)
        body = "7月29日（水）21時27分 終了\n現在\n15,500円\n（税0円）"
        self.assertEqual(od._tax_marked(body, ("落札", "現在")), 15500.0)

    def test_ended_detection_distinguishes_scheduled_end(self):
        # 「終了予定」は出品中、単独の「終了」は終了済み
        self.assertTrue(od._is_ended(NoDomPage(),
                                     "7月29日（水）21時27分 終了"))
        self.assertFalse(od._is_ended(NoDomPage(),
                                      "7月30日（木）22時44分 終了予定"))

    def test_buyout_only_from_jsonld_not_body(self):
        # 富士の冬: 本文にメイン即決57000があってもJSON-LD由来でないので取らない。
        # (JSON-LDに即決が載る将来ケースは別テストで担保)
        body = ("川瀬巴水「五月雨 荒川」\n即決\n48,000円\n"
                "現在\n45,000円\n（税0円）\n即決\n57,000円")
        got = od._extract_prices(NoDomPage(), body, ended=False)
        self.assertIsNone(got['buyout'])  # 本文の即決は関連・メイン問わず不採用


    def test_buyout_not_taken_from_body(self):
        # 本文に即決があってもJSON-LD由来でなければ取らない(関連商品の疑い)
        body = '即決 39,800円\n現在価格 9,000円\n関連商品 6,900円'
        got = od._extract_prices(NoDomPage(), body, ended=False)
        self.assertEqual(got['price'], 9000.0)
        self.assertIsNone(got['buyout'])  # 本文即決は不採用


    def test_arbitrary_amount_is_never_price(self):
        body = '送料無料 6,900円\nヤフーフリマ 9,800円\nクーポン 1,000円'
        got = od._extract_prices(NoDomPage(), body, ended=False)
        self.assertIsNone(got['price'])
        self.assertEqual(got['source'], 'not_found')

    def test_ended_uses_labeled_sale_price(self):
        body = 'このオークションは終了しています\n落札価格 24,500円\n即決価格 39,800円'
        got = od._extract_prices(NoDomPage(), body, ended=True)
        self.assertEqual(got['price'], 24500.0)
        self.assertEqual(got['role'], 'sale')

    def test_price_drop_is_quarantined(self):
        con = sqlite3.connect(':memory:')
        con.execute('''CREATE TABLE listing_observation(
            txn_id TEXT, price_jpy REAL, bid_count INTEGER,
            status TEXT, observed_at TEXT, price_confidence REAL)''')
        con.execute("INSERT INTO listing_observation VALUES('x',39800,1,'出品中','2026-07-16',0.99)")
        info = {'price': 9000.0, 'confidence': 0.99, 'evidence': {}}
        got = od._apply_price_sanity(con, 'x', info, bids=1, ended=False)
        self.assertLessEqual(got['confidence'], 0.35)
        self.assertIn('価格下落異常', got['warning'])

    def test_rejected_observation_without_trusted_buyout_is_excluded(self):
        # ★2026-07-29: 収集時即決へのフォールバックは廃止した(汚染源だった)。
        # 信頼できる観測も信頼できる即決も無い競りは、候補から外すのが正しい。
        got = bf.select_actionable_price(
            'auction', 39800, 9000, 0.35, 'text_only_unverified',
            buyout_latest=41000, buyout_at_collect=39800,
            buyout_latest_confidence=0.35)
        self.assertIsNone(got['price'])
        self.assertIsNone(got['buyout'])
        self.assertEqual(got['basis'], 'no_trusted_active_price')
        self.assertTrue(got['observation_rejected'])


    def test_low_confidence_observation_buyout_is_rejected(self):
        # 観測由来の即決しかなく信頼度不足なら、候補から外す(國輝¥52,800の事故)
        got = bf.select_actionable_price(
            'auction', None, 3000, 0.0, 'legacy_unverified',
            buyout_latest=52800, buyout_at_collect=None,
            buyout_latest_confidence=0.0)
        self.assertIsNone(got['price'])
        self.assertEqual(got['basis'], 'no_trusted_active_price')

    def test_stale_auction_without_buyout_is_not_scored(self):
        got = bf.select_actionable_price(
            'auction', 9000, None, None, None,
            buyout_latest=None, buyout_at_collect=None)
        self.assertIsNone(got['price'])
        self.assertEqual(got['basis'], 'no_trusted_active_price')

    def test_fixed_ask_can_fallback_to_collection(self):
        got = bf.select_actionable_price(
            'fixed', 12000, None, None, None,
            buyout_latest=None, buyout_at_collect=None)
        self.assertEqual(got['price'], 12000)
        self.assertEqual(got['basis'], 'collection_fixed_ask')


class SealLogicTests(unittest.TestCase):
    def test_consensus_requires_strict_majority(self):
        self.assertEqual(se._consensus(['A1', 'D'])[0], 'unreadable')
        self.assertEqual(se._consensus(['A1', 'A1', 'D'])[0], 'A1')

    def test_modern_physical_mark_vetoes_prewar_facsimile(self):
        got = sp._classification_decision([
            {'type': 'D', 'mark_medium': 'printed_in_image'},
            {'type': 'J', 'mark_medium': 'physical_stamp'},
        ])
        self.assertEqual(got['decision'], 'veto_modern')
        self.assertEqual(got['best_type'], 'J')

    def test_facsimile_only_never_promotes(self):
        got = sp._classification_decision([
            {'type': 'D', 'mark_medium': 'printed_in_image'},
        ])
        self.assertEqual(got['decision'], 'manual_facsimile_only')

    def test_copyright_plus_artist_seal_resolves_copyright(self):
        # 版権印C2 + 作家落款(not_watanabe)は矛盾ではなく整合。C2で確定
        got = sp._classification_decision([
            {'type': 'C2', 'mark_medium': 'physical_stamp'},
            {'type': 'not_watanabe', 'mark_medium': 'physical_stamp'},
        ])
        self.assertEqual(got['best_type'], 'C2')
        self.assertEqual(got['decision'], 'manual_prewar')

    def test_copyright_plus_personal_seal_prefers_copyright(self):
        # C2 + M3(個人印) + 落款 = 桜美人の実例。C2が代表
        got = sp._classification_decision([
            {'type': 'C2', 'mark_medium': 'physical_stamp'},
            {'type': 'M3', 'mark_medium': 'physical_stamp'},
            {'type': 'not_watanabe', 'mark_medium': 'physical_stamp'},
        ])
        self.assertEqual(got['best_type'], 'C2')
        self.assertEqual(got['decision'], 'manual_prewar')

    def test_conflicting_copyright_seals_flagged(self):
        # 版権印同士の食い違い(C2+I) = 印貼り替え詐欺の型。強警戒
        got = sp._classification_decision([
            {'type': 'C2', 'mark_medium': 'printed_in_image'},
            {'type': 'I', 'mark_medium': 'printed_in_image'},
        ])
        self.assertEqual(got['decision'], 'manual_conflicting_seals')

    def test_round_with_artist_seal_is_round(self):
        got = sp._classification_decision([
            {'type': 'round', 'mark_medium': 'physical_stamp'},
            {'type': 'not_watanabe', 'mark_medium': 'physical_stamp'},
        ])
        self.assertEqual(got['best_type'], 'round')

    def test_taxonomy_gap_routes_to_manual(self):
        got = sp._classification_decision([
            {'type': 'unknown_watanabe', 'mark_medium': 'physical_stamp'},
        ])
        self.assertEqual(got['decision'], 'manual_taxonomy_gap')

    def test_era_map_is_derived_from_json(self):
        types = se.load_types(str(ROOT / 'seal_types.json'))
        era = se.build_era_map(types)
        self.assertEqual(era['I'], 'atozuri')
        self.assertEqual(era['J'], 'repro')
        self.assertEqual(era['M1'], 'personal')


class SqlAndMigrationTests(unittest.TestCase):
    def test_pick_listings_parameter_order(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, 'x.db')
            con = sqlite3.connect(db)
            con.executescript('''
                CREATE TABLE sale_record(
                    txn_id TEXT, listing_title_raw TEXT, jpy_converted REAL,
                    source_url TEXT, auction_result TEXT, platform TEXT);
                CREATE TABLE title_classification(
                    raw_title TEXT, item_class TEXT, impression_claim TEXT, artist TEXT);
            ''')
            con.execute("INSERT INTO sale_record VALUES(?,?,?,?,?,?)", (
                't1', '川瀬巴水 芝増上寺', 5000,
                'https://page.auctions.yahoo.co.jp/jp/auction/t1', '出品中', 'ヤフオク'))
            con.execute("INSERT INTO title_classification VALUES(?,?,?,?)", (
                '川瀬巴水 芝増上寺', 'original_print', None, '川瀬巴水'))
            con.commit(); con.close()
            rows = sp.pick_listings(db, 10, 80000)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['txn_id'], 't1')

    def test_observation_columns_are_migrated(self):
        con = sqlite3.connect(':memory:')
        con.execute('''CREATE TABLE listing_observation(
            txn_id TEXT, price_jpy REAL, bid_count INTEGER, time_left TEXT,
            status TEXT, observed_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        od._ensure_observation_columns(con)
        cols = {r[1] for r in con.execute('PRAGMA table_info(listing_observation)')}
        for c in ('buyout_price_jpy', 'price_source', 'price_confidence', 'price_raw', 'note'):
            self.assertIn(c, cols)

    def test_legacy_price_audit_quarantines_old_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, 'obs.db')
            con = sqlite3.connect(db)
            con.execute('''CREATE TABLE listing_observation(
                txn_id TEXT, price_jpy REAL, bid_count INTEGER, time_left TEXT,
                status TEXT, observed_at TEXT, note TEXT)''')
            con.execute("INSERT INTO listing_observation VALUES('x',39800,1,NULL,'出品中','2026-07-16',NULL)")
            con.execute("INSERT INTO listing_observation VALUES('x',9000,1,NULL,'出品中','2026-07-25',NULL)")
            con.commit(); con.close()
            result = apo.audit(db, apply=True)
            self.assertEqual(result['legacy'], 2)
            self.assertEqual(len(result['anomalies']), 1)
            con = sqlite3.connect(db)
            rows = con.execute('SELECT price_source, price_confidence FROM listing_observation').fetchall()
            con.close()
            self.assertTrue(all(r == ('legacy_unverified', 0.0) for r in rows))


if __name__ == '__main__':
    unittest.main(verbosity=2)
