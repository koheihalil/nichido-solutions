"""
bargain_finder.py
掘り出し物判定の中核。DBを読んで「今買える割安候補」を出す。

設計原則(外部AI 2社の助言を反映):
  1. 相場は1つの数字にしない。4層を分けて持つ:
       仕入れ確定相場   = ヤフオク等の auction/final (中央値・25%点)
       出口確定相場     = Artelino/eBay の auction/final
       出口希望価格     = ディーラーの fixed/ask (上限の目安。直接の利益計算に使わない)
       売れ残り情報     = auction/ask, 長期 fixed/ask (その額では売れない証拠)
  2. 判定は「安く見えるか」ではなく:
       仕入れ確定相場の中央値と比べて安いか
       保守的出口(出口確定の25%点)との差があるか
  3. 中央値を使う(平均は外れ値に弱い)。比較データ3件未満は「参考値」扱い
  4. 減点: 複製キーワード / サイズ不一致 / 後摺 (加点: 版元名 / 真作保証 / 初摺)

図柄同定(ルールベース第1段階):
  - 作品名からノイズ語(真作保証/木版画/浮世絵/版画/額装...)を除去
  - 作家ごとの代表作キーワード辞書で正規化(増上寺/芝増上寺→shiba-zojo 等)
  - 繋がらないものは作家単位の相場で比較(粗いが無いよりマシ)
  ※日英照合はAI正規化(normalize_titles.py)で後から上書き可能な設計

使い方:
  python3 bargain_finder.py shinhanga.db            # 候補をターミナルに表示
  python3 bargain_finder.py shinhanga.db --json out.json   # ダッシュボード用JSON
"""
import sqlite3
import re
import json
import sys
import statistics

# ------------------------------------------------------------
# 図柄同定: ノイズ除去と正規化
# ------------------------------------------------------------
NOISE_WORDS = [
    "真作保証", "真作", "保証", "木版画", "木版", "版画", "浮世絵", "新版画",
    "額装", "額入り", "額付", "美品", "希少", "レア", "本物", "肉筆",
    "アンティーク", "ビンテージ", "ヴィンテージ", "古い", "時代物",
    "送料無料", "1円", "スタート", "彫師", "摺師", "監修",
    "original", "woodblock", "print", "japanese", "antique", "vintage",
    "authentic", "guaranteed", "framed",
]

# 代表図柄の正規化辞書: canonical_id -> 検出キーワード(日英)
# ★AI正規化(normalize_titles.py)が入るまでの手動辞書。
#   ここに無い図柄は「同定不能」として候補から外れる(strictモード)ので、
#   実データを見ながら追記していくのが効く。
DESIGN_ALIASES = {
    # --- 川瀬巴水 ---
    "shiba-zojo":      ["芝増上寺", "増上寺", "zojoji", "zojo temple", "shiba zojo"],
    "toyamagahara":    ["冬の月", "戸山ヶ原", "戸山ガ原", "toyamagahara", "winter moon"],
    "ueno-toshogu":    ["東照宮", "toshogu"],
    "shiba-benten":    ["芝弁天池", "弁天池", "benten pond", "shiba benten"],
    "magome-moon":     ["馬込の月", "magome"],
    "kiyomizu":        ["清水堂", "清水寺", "kiyomizu"],
    "nakajima":        ["中島の雨", "nakajima"],
    "sarusawa":        ["猿沢池", "sarusawa"],
    "mizuki":          ["三重塔", "水木", "pagoda"],
    "kaminoyama":      ["上山", "kaminoyama"],
    "arakawa":         ["荒川", "赤羽", "arakawa", "akabane"],
    "kisaragi":        ["如月", "kisaragi"],
    "hirakawa":        ["平川口", "hirakawa"],
    "sengakuji":       ["泉岳寺", "sengakuji"],
    "inokashira":      ["井の頭", "inokashira"],
    "nezu":            ["根津", "nezu"],
    "omori":           ["大森海岸", "大森", "omori"],
    "sakurada":        ["桜田門", "sakurada"],
    "harunoyu":        ["春の夕", "spring evening"],
    # --- 吉田博 ---
    "hansen-asa":      ["帆船 朝", "帆船・朝", "帆船朝", "sailboat morning",
                        "sailing boat, morning"],
    "hansen-gogo":     ["帆船 午後", "帆船午後", "sailboat afternoon"],
    "kameido":         ["亀井戸", "亀戸", "kameido"],
    "fuji-kawaguchi":  ["河口湖", "kawaguchi lake", "lake kawaguchi"],
    "matterhorn":      ["マッターホルン", "matterhorn"],
    "kumoi-zakura":    ["雲井桜", "kumoi"],
    "hutu-mine":       ["穂高", "剱岳", "hodaka", "tsurugi"],
    # --- 小原古邨 ---
    "shirasagi":       ["白鷺", "柳に白鷺", "egret", "heron"],
    "suzume-nanten":   ["雀に南天", "南天に雀", "sparrow nandina"],
    "kingyo":          ["金魚", "goldfish"],
    "kamo":            ["鴨", "duck"],
    "tsuru":           ["鶴", "crane"],
    "fukurou":         ["梟", "ふくろう", "owl"],
    "kujaku":          ["孔雀", "peacock"],
    # --- 笠松紫浪 ---
    "akamon-yuki":     ["赤門の雪", "赤門", "akamon"],
    "yushima":         ["湯島天神", "湯島", "yushima"],
    "nikko":           ["日光", "nikko"],
    "matsushima":      ["松島", "matsushima"],
    # --- 土屋光逸 ---
    "kihan-yabase":    ["帰帆", "矢橋", "yabase"],
    "ishiyama":        ["石山寺", "ishiyama"],
    # --- 伊東深水 ---
    "taikyo":          ["対鏡", "鏡", "mirror"],
    "yukake":          ["湯かけ", "湯上り", "after bath"],
    # --- 共通の名所 ---
    "kinkaku":         ["金閣", "golden pavilion", "kinkaku"],
    "senjo-towada":    ["千丈", "十和田", "senjo", "towada"],
    "fuji":            ["富士", "mount fuji", "mt fuji"],
}

REPLICA_WORDS = ["複製", "ポスター", "印刷", "レプリカ", "コピー", "CD版",
                 "reproduction", "reprint poster", "replica", "copy",
                 # ★2026-07-30 追加。「川瀬巴水の模作の版画」が候補処理に
                 #   回っていた。模作/摸作/写し/倣い は他人が真似て作った品で、
                 #   これを掘り出し物として買うと丸損になる
                 "模作", "摸作", "模写", "写し", "倣い", "imitation"]

# ★版画本体ではないもの。相場も汚すし、安いので候補上位に紛れ込む。
#   実データ(1666件)を見て判明: 切手シート・カタログ・書籍が大量に混入していた。
NON_PRINT_WORDS = [
    # 切手類
    "切手", "シート", "初日カバー", "記念切手", "小型シート", "郵趣", "stamp",
    # 印刷物・書籍
    "カタログ", "図録", "目録", "画集", "作品集", "書籍", "古書", "豆本",
    "写真集", "解説書", "冊子", "パンフレット", "リーフレット",
    "catalog", "catalogue", "book", "album",
    # 紙もの・派生品
    "絵葉書", "絵はがき", "ハガキ", "はがき", "ポストカード", "postcard",
    "カレンダー", "calendar", "クリアファイル", "マグネット", "ステッカー",
    "テレカ", "テレホンカード", "しおり", "栞",
    # 付属品のみ
    "額のみ", "額縁のみ", "マットのみ", "パネルのみ", "空箱", "箱のみ",
    "タトウのみ", "たとうのみ",
    # まとめ売り(個別の相場比較ができない)
    "まとめて", "まとめ売り", "セット販売", "大量", "ジャンク",
]
LATER_WORDS = ["後摺", "後刷", "後版", "later printing", "later edition", "posthumous"]
EARLY_WORDS = ["初摺", "初刷", "初版", "生前摺", "first edition", "early printing",
               "lifetime", "first state"]
PUBLISHER_WORDS = ["渡邊", "渡辺", "watanabe", "土井", "doi", "酒井", "sakai",
                   "川口", "kawaguchi", "芸艸堂", "unsodo"]
GUARANTEE_WORDS = ["真作保証", "保証", "guaranteed authentic"]


def clean_title(title):
    """ノイズ語・記号・年号を除去して、図柄名の芯を残す。"""
    if not title:
        return ""
    t = title
    for w in NOISE_WORDS:
        t = re.sub(re.escape(w), " ", t, flags=re.I)
    t = re.sub(r"昭和\s*\d+\s*年?", " ", t)
    t = re.sub(r"大正\s*\d+\s*年?", " ", t)
    t = re.sub(r"(19|20)\d{2}", " ", t)
    t = re.sub(r"[【】\[\]()（）「」『』〈〉<>#★☆■□◆◇!!??・、。,\.]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# AIが振った細かいIDを、相場計算用に粗くまとめるための題材語。
# 例: willow-and-egret / egret-in-rain / white-egret → すべて "egret" 群として集計。
# 理由: AIは作品名ごとに固有IDを振るので、そのままだと母数1のIDだらけになり
#       相場が作れない(実データで1142ID中1028個が母数1だった)。
COARSE_TOPICS = [
    # ★並び順が重要: 先に見つかった語が採用される。
    #   主題になりやすい固有名詞・被写体を前に、修飾語(snow/rain/moon等)を後ろに置く。
    # 固有の場所・建物(最も主題性が高い)
    "zojoji", "zojo", "toshogu", "kiyomizu", "kinkaku", "sengakuji", "yushima",
    "asakusa", "sensoji", "nikko", "matsushima", "inokashira", "sarusawa",
    "ishiyama", "akamon", "kameido", "magome", "sakurada", "arakawa", "akabane",
    "ueno", "shiba", "nezu", "omori", "fuji",
    # 被写体(花鳥・人物・船)
    "egret", "heron", "crane", "sparrow", "owl", "peacock", "duck", "goose",
    "goldfish", "carp", "crow", "hawk", "swallow", "kingfisher",
    "lotus", "iris", "peony", "chrysanthemum", "bamboo", "willow", "pine",
    "cherry", "sakura", "maple",
    "bijin", "beauty", "geisha", "mirror", "bath", "kimono",
    "sailboat", "sailing", "boat", "ship",
    # 一般名詞(場所の種類)
    "pagoda", "temple", "shrine", "castle", "bridge", "gate",
    "lake", "river", "sea", "coast", "mountain",
    # 修飾語(最後。これしか無い場合のみ使う)
    "snow", "rain", "moon", "evening", "morning", "night",
    "spring", "summer", "autumn", "winter",
]


def coarse_key(ai_id):
    """
    AIの細かいID(willow-and-egret 等)から、粗い題材キーを作る。
    題材語は1つだけ使う。2語使うと egret-willow と egret-rain が
    別キーになってしまい、細分化が解消しないため。
    COARSE_TOPICS の並び順で先に見つかったものを優先する
    (主題になりやすい語を前に置いてある)。
    題材語が無ければ None。
    """
    if not ai_id:
        return None
    low = str(ai_id).lower().replace("_", "-")
    for t in COARSE_TOPICS:
        if t in low:
            return "topic:" + t
    return None


def design_key(title, canonical_map=None):
    """
    タイトルから図柄の正規IDを返す。
    優先順:
      (1) 手動辞書 — 意図的に粗くまとめてあるので相場の母数を確保しやすい
      (2) AI正規化ID を粗い題材キーに変換したもの
      (3) AI正規化IDそのもの(母数は薄いが、無いよりマシ)
      (4) None(同定不能)
    ※以前はAIを最優先していたが、AIが作品名ごとに固有IDを振るため
      母数1のIDだらけになり、かえって相場が作れなくなった。順序を逆にした。
    """
    if not title:
        return None
    low = title.lower()

    # (1) 手動辞書が最優先
    for cid, aliases in DESIGN_ALIASES.items():
        for a in aliases:
            if a.lower() in low:
                return cid

    # (2)(3) AIの正規化結果
    if canonical_map:
        ai_id = canonical_map.get(title)
        if ai_id:
            ck = coarse_key(ai_id)
            return ck or ai_id
    return None


ARTIST_NAMES = ["川瀬巴水", "吉田博", "小原古邨", "笠松紫浪", "土屋光逸", "伊東深水",
                "橋口五葉", "高橋松亭", "川瀬 巴水", "小原 古邨"]


SEARCH_SPAM_MARKS = ["検索＝", "検索=", "検索:", "検索：", "検＝", "検=",
                     "検:", "検：", "(検", "（検", "検索ワード", "他多数"]


def strip_search_spam(title):
    """検索避け・集客用の作家名羅列を切り落とす。

    ★2026-07-30: 「渡辺省亭/紅葉鳥の木版画(検索＝新版画 川瀬巴水…)」が
      作家=川瀬巴水と誤判定されていた。出品者は集客のため無関係な有名作家名を
      末尾に並べるので、そこを作家判定に使うと別人の相場を引いてしまう。
      AI分類のプロンプトは既にこれを除外しているが、分類が無い品の
      タイトル照合フォールバックが素通ししていた。
    """
    if not title:
        return title
    cut = len(title)
    for mark in SEARCH_SPAM_MARKS:
        i = title.find(mark)
        if i != -1:
            cut = min(cut, i)
    return title[:cut]


def artist_from_title(title, fallback=None):
    """
    タイトルに作家名が書いてあればそれを優先する。
    (design_master 経由の artist_id がズレている場合の保険)
    ★検索スパム部分は作家判定に使わない。
    """
    if not title:
        return fallback
    head = strip_search_spam(title)
    for name in ARTIST_NAMES:
        if name in head:
            return name.replace(" ", "")
    return fallback


def has_any(text, words):
    if not text:
        return False
    low = str(text).lower()
    return any(w.lower() in low for w in words)


# ------------------------------------------------------------
# 相場の4層を計算
# ------------------------------------------------------------
def build_market(con, canonical_map=None):
    """
    図柄ごと・作家ごとに、4層の相場を組み立てる。
    返り値:
      by_design[design_key] = {"source_final": [...], "exit_final": [...], "exit_ask": [...]}
      by_artist[artist]     = 同上(図柄同定できない品のフォールバック)
    """
    SOURCE_PLATFORMS = ("ヤフオク",)                      # 仕入れ市場
    EXIT_PLATFORMS = ("Artelino", "eBay", "Christie's")  # 出口市場(成約)

    rows = con.execute("""
        SELECT sr.txn_id, sr.platform, sr.listing_type, sr.price_kind,
               sr.jpy_converted, sr.listing_title_raw,
               dm.title AS dm_title, am.name AS artist
        FROM sale_record sr
        LEFT JOIN design_master dm ON dm.design_id = sr.design_id
        LEFT JOIN artist_master am ON am.artist_id = dm.artist_id
        WHERE sr.jpy_converted IS NOT NULL
          AND sr.price_kind IN ('final', 'ask')
    """).fetchall()

    # ★相場の分母にもAI分類を効かせる。
    #   実測では 本物¥127,286 / 後摺¥47,002 / 複製¥4,893 と26倍の開きがある。
    #   これを混ぜた中央値を「相場」と呼ぶと、候補判定が根本から狂う。
    class_map = {}
    artist_map = {}
    if con.execute("""SELECT 1 FROM sqlite_master
                      WHERE type='table' AND name='title_classification'
                   """).fetchone():
        for r in con.execute("""SELECT raw_title, item_class, artist
                                FROM title_classification"""):
            class_map[r[0]] = r[1]
            if r[2]:
                artist_map[r[0]] = r[2]
    OK_CLASSES = ("original_print", "later_impression")

    def resolve_artist(raw_title, fallback_title, db_artist):
        """
        作家の解決。AIの判定(SEOスパム耐性あり)を最優先。
        「検索用 吉田博」の羅列に反応して光逸の品が吉田博の相場に
        混ざる事故(¥172,250 → ¥37,094)を防ぐ。
        「その他」= 28作家リスト外 → 文字列一致に戻さずそのまま使う。
        """
        ai = artist_map.get(raw_title)
        if ai:
            return ai
        return artist_from_title(fallback_title, db_artist or "不明")

    by_design, by_artist = {}, {}

    def bucket(dic, key):
        return dic.setdefault(key, {"source_final": [], "exit_final": [],
                                    "exit_ask": [], "areas": []})

    for r in rows:
        title = r["dm_title"] or r["listing_title_raw"] or ""
        raw = r["listing_title_raw"] or ""
        cls = class_map.get(raw) or class_map.get(title)
        if cls:
            # 分類があるなら、それに従う(本物と後摺だけを相場に使う)
            if cls not in OK_CLASSES:
                continue
        elif has_any(title, REPLICA_WORDS) or has_any(title, NON_PRINT_WORDS):
            # 分類が無いものだけ従来のキーワード判定
            continue
        dkey = design_key(title, canonical_map)
        akey = resolve_artist(raw, title, r["artist"])
        price = r["jpy_converted"]
        plat = r["platform"] or ""

        # 相場側の面積(仕入層のみ。比較の母集団になる)
        _area = usable_area(raw or title)

        layer = None
        if r["price_kind"] == "final":
            if any(p in plat for p in SOURCE_PLATFORMS):
                layer = "source_final"
            elif any(p in plat for p in EXIT_PLATFORMS):
                layer = "exit_final"
            else:
                layer = "exit_final"   # ディーラー成約等も出口側に
        elif r["price_kind"] == "ask" and r["listing_type"] == "fixed":
            layer = "exit_ask"
        if layer is None:
            continue

        if dkey:
            bucket(by_design, dkey)[layer].append(price)
            if _area and layer == "source_final":
                bucket(by_design, dkey)["areas"].append(_area)
        bucket(by_artist, akey)[layer].append(price)
        if _area and layer == "source_final":
            bucket(by_artist, akey)["areas"].append(_area)

    return by_design, by_artist


def pct(values, p):
    if not values:
        return None
    vs = sorted(values)
    k = (len(vs) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(vs) - 1)
    return vs[f] + (vs[c] - vs[f]) * (k - f)



# ------------------------------------------------------------
# サイズ互換チェック(SIZE_HANDLING.md v1.1)
#   豆判¥5,000を小判中心の相場¥36,400と比べて「13%激安」と誤判定した
#   問題への対策。異常検出用途なので、外部レビューの基準に従い
#   conf>=0.5 かつ target!=frame の寸法を使う(unknownは使ってよい)。
#   セット物(三枚続等)は面積比較の適用外。
# ------------------------------------------------------------
try:
    from size_extractor import extract as _size_extract
    _HAS_SIZE = True
except ImportError:
    _HAS_SIZE = False   # size_extractor.py が無ければチェックなしで従来動作

_size_cache = {}


def _size_info(title):
    if title in _size_cache:
        return _size_cache[title]
    r = _size_extract(title)
    area = None
    if "set_item" not in r["warning_flags"]:
        for d in r["dimensions"]:
            if (d["confidence"] >= 0.5
                    and d["measurement_target"] != "frame"
                    and "implausible_value" not in d["warnings"]):
                area = d["dimension_1_cm"] * d["dimension_2_cm"]
                break
    info = (area, "set_item" in r["warning_flags"])
    _size_cache[title] = info
    return info


def usable_area(title):
    """タイトル → 面積cm2 or None。比較に使える寸法だけ返す"""
    if not (_HAS_SIZE and title):
        return None
    return _size_info(title)[0]


def is_set_item(title):
    if not (_HAS_SIZE and title):
        return False
    return _size_info(title)[1]


def summarize(prices):
    if not prices:
        return None
    return {
        "n": len(prices),
        "median": statistics.median(prices),
        "p25": pct(prices, 25),
        "min": min(prices),
        "max": max(prices),
    }


def select_actionable_price(listing_type, collection_price, observation_price,
                            observation_confidence, observation_source,
                            buyout_latest=None, buyout_at_collect=None,
                            buyout_latest_confidence=None):
    """候補判定に使ってよい現在価格を選ぶ。

    競りの収集時価格は、時間経過後には単なる古い下限であり購入可能価格ではない。
    信頼済みの最新観測が無い競りは、明示的な即決価格がある場合だけ評価する。
    固定価格は収集時価格へフォールバックできるが、鮮度不足として返す。

    ★2026-07-27: 即決(buyout)にも現在価格と同じ信頼度ゲートを掛ける。
      以前は buyout_latest を無検査で採用していたため、隔離済み(信頼度0)の
      旧パーサ即決価格がダッシュボードに出ていた(國輝¥52,800の事故)。
      観測由来の即決は信頼度>=0.80のときだけ使う。sale_record由来の
      収集時即決(buyout_at_collect)は隔離対象外なので従来通り使える。
    """
    source = observation_source or ""
    trusted = (observation_price is not None
               and observation_confidence is not None
               and float(observation_confidence) >= 0.80
               and source not in ("jsonld_offer_ambiguous", "not_found"))

    # 即決の採否: 観測由来のみ。信頼度ゲートを通ったものだけ使う。
    # ★2026-07-29: sale_record の収集時即決(buyout_at_collect)へのフォールバックを
    #   廃止した。あれは収集時に本文パースで取った値で、関連商品枠の即決を
    #   掴んでいる(國輝¥52,800、#7現在¥3,850に対し即決¥1,900、#9現在¥20,000に対し
    #   即決¥1,980 = 即決が現在より安いのはオークションの仕組み上ありえない)。
    #   観測側を洗っても、ここでフォールバックすると汚染値が復活していた。
    buyout_obs_ok = (buyout_latest is not None
                     and buyout_latest_confidence is not None
                     and float(buyout_latest_confidence) >= 0.80)
    buyout = buyout_latest if buyout_obs_ok else None

    if trusted:
        basis = ("buyout_observation" if "buyout_fallback" in source
                 else "current_observation")
        return {
            "price": observation_price,
            "buyout": buyout,
            "trusted_observation": True,
            "observation_rejected": False,
            "basis": basis,
        }

    rejected = observation_price is not None
    # ★listing_type が auction または不明(NULL)で、信頼できる即決があれば使う。
    #   国輝のように listing_type=NULL でも即決フォールバックの対象にする
    if listing_type == "auction" or listing_type is None:
        if buyout is not None:
            return {
                "price": buyout,
                "buyout": buyout,
                "trusted_observation": False,
                "observation_rejected": rejected,
                "basis": "buyout_fallback",
            }
        return {
            "price": None,
            "buyout": None,
            "trusted_observation": False,
            "observation_rejected": rejected,
            "basis": "no_trusted_active_price",
        }

    return {
        "price": collection_price,
        "buyout": buyout,
        "trusted_observation": False,
        "observation_rejected": rejected,
        "basis": "collection_fixed_ask",
    }


# ------------------------------------------------------------
# 候補の評価
# ------------------------------------------------------------
def find_bargains(db_path, canonical_map=None, limit=30, strict=True):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    by_design, by_artist = build_market(con, canonical_map)

    # 今買える品: auction/current と fixed/ask
    # ★重要: DBのレコードは一切消さない。終了した品も相場データとして残す。
    #   ここでは「今まだ買えるか」で候補を絞るだけ。
    #   さらに listing_observation の最新観測を使い、収集時点の古い価格ではなく
    #   直近で確認できた価格・入札数・状態を表示する。
    # 列の有無を確認(migrate_buyout.py 未適用のDBでも動くように)
    sr_cols = [x[1] for x in con.execute("PRAGMA table_info(sale_record)")]
    obs_cols = [x[1] for x in con.execute("PRAGMA table_info(listing_observation)")]
    sr_buyout = ("sr.buyout_price_jpy" if "buyout_price_jpy" in sr_cols else "NULL")
    obs_buyout = ("obs.buyout_price_jpy" if "buyout_price_jpy" in obs_cols else "NULL")
    obs_price_source = ("obs.price_source" if "price_source" in obs_cols else "NULL")
    obs_price_conf = ("obs.price_confidence" if "price_confidence" in obs_cols else "NULL")
    obs_note = ("obs.note" if "note" in obs_cols else "NULL")

    rows = con.execute(f"""
        SELECT sr.txn_id, sr.platform, sr.listing_type, sr.price_kind,
               sr.jpy_converted AS price_at_collect,
               sr.bid_count AS bids_at_collect,
               sr.listing_title_raw, sr.source_url, sr.actual_size_cm,
               sr.auction_result,
               {sr_buyout} AS buyout_at_collect,
               dm.title AS dm_title, am.name AS artist,
               obs.price_jpy   AS price_latest,
               obs.bid_count   AS bids_latest,
               obs.status      AS status_latest,
               obs.time_left   AS time_left_latest,
               obs.observed_at AS observed_at_latest,
               {obs_buyout} AS buyout_latest,
               {obs_price_source} AS price_source_latest,
               {obs_price_conf} AS price_confidence_latest,
               {obs_note} AS observation_note_latest
        FROM sale_record sr
        LEFT JOIN design_master dm ON dm.design_id = sr.design_id
        LEFT JOIN artist_master am ON am.artist_id = dm.artist_id
        LEFT JOIN (
            SELECT o.* FROM listing_observation o
            JOIN (SELECT txn_id, MAX(observed_at) mx
                  FROM listing_observation GROUP BY txn_id) t
              ON o.txn_id = t.txn_id AND o.observed_at = t.mx
        ) obs ON obs.txn_id = sr.txn_id
        WHERE sr.jpy_converted IS NOT NULL
          AND (sr.price_kind = 'current'
               OR (sr.price_kind = 'ask' AND sr.listing_type = 'fixed'))
          -- 収集時点で出品中だったもの(終了済みはDBに残るが候補には出さない)
          AND (sr.auction_result = '出品中' OR sr.auction_result IS NULL)
          -- 最新観測で終了・落札・消滅が確認されたものは、もう買えないので除外
          AND (obs.status IS NULL
               OR obs.status NOT IN ('落札', '終了', '終了(不落札)', '終了(価格不明)', '消滅'))
    """).fetchall()

    # AI分類(classify_titles.py の結果)を読む。無ければ空のまま。
    class_map = {}
    claim_map = {}
    if con.execute("""SELECT 1 FROM sqlite_master
                      WHERE type='table' AND name='title_classification'
                   """).fetchone():
        class_map = {r[0]: r[1] for r in con.execute(
            "SELECT raw_title, item_class FROM title_classification")}
        # 摺りの主張(claim)。列がまだ無いDBでも動くようにガードする
        tc_cols = [r[1] for r in con.execute(
            "PRAGMA table_info(title_classification)")]
        if "impression_claim" in tc_cols:
            claim_map = {r[0]: r[1] for r in con.execute(
                """SELECT raw_title, impression_claim
                   FROM title_classification
                   WHERE impression_claim IS NOT NULL""")}
    cand_artist_map = {}
    if class_map:
        cand_artist_map = {r[0]: r[1] for r in con.execute(
            """SELECT raw_title, artist FROM title_classification
               WHERE artist IS NOT NULL""")}

    candidates = []
    excluded = {"replica": 0, "non_print": 0, "no_design": 0,
                "size_incompatible": 0,
                "no_market": 0, "unclassified": 0}
    for r in rows:
        title = r["dm_title"] or r["listing_title_raw"] or ""
        # 最新観測は「値がある」だけでは採用しない。意味ラベル付きで
        # confidence>=0.80 のものだけを候補スコアへ入れる。旧観測や
        # JSON-LDだけの曖昧値は表示用に残しても、価格判断には使わない。
        obs_conf = r["price_confidence_latest"]
        obs_source = r["price_source_latest"] or ""
        selected_price = select_actionable_price(
            r["listing_type"], r["price_at_collect"], r["price_latest"],
            obs_conf, obs_source, r["buyout_latest"], r["buyout_at_collect"],
            buyout_latest_confidence=obs_conf)
        price = selected_price["price"]
        buyout = selected_price["buyout"]
        obs_trusted = selected_price["trusted_observation"]
        observation_rejected = selected_price["observation_rejected"]
        pricing_basis = selected_price["basis"]
        if price is None:
            excluded["no_trusted_active_price"] = excluded.get(
                "no_trusted_active_price", 0) + 1
            continue
        bids = r["bids_latest"] if r["bids_latest"] is not None else r["bids_at_collect"]
        price_is_stale = not obs_trusted
        # AIの作家判定を最優先(SEOスパムの作家名羅列に反応しない)
        _raw = r["listing_title_raw"] or title
        artist = cand_artist_map.get(_raw) or artist_from_title(
            title, r["artist"] or "不明")

        # --- 除外の判定 ---
        # ★AI分類(classify_titles.py)があればそれを最優先する。
        #   キーワード一致より正確で、「後摺」(真正な木版画)と
        #   「複製」(印刷物)を取り違えない。分類が無いタイトルだけ
        #   従来のキーワード判定にフォールバックする。
        # ★raw_title を先に引く(class_mapのキーはraw_title)。
        #   design_id付きの旧レコードは title=dm_title になり、
        #   titleだけで引くとAI分類が素通りして複製が候補に紛れていた。
        #   相場構築側(build_market)と同じ引き方に揃える。
        cls = (class_map.get(_raw) or class_map.get(title)) if class_map else None
        if cls:
            if cls in ("mechanical_repro", "recarved_or_repro"):
                excluded["replica"] += 1
                continue
            if cls in ("book_or_page", "not_woodblock"):
                excluded["non_print"] += 1
                continue
            if cls == "unknown":
                # 判断材料がない品。相場を汚すので候補にしない
                excluded["unclassified"] += 1
                continue
        else:
            # --- 除外: 複製 ---
            # ★rawも見る。「複製」等はdm_titleではなく出品タイトル側に書かれる
            if has_any(title, REPLICA_WORDS) or has_any(_raw, REPLICA_WORDS):
                excluded["replica"] += 1
                continue
            # --- 除外: 版画本体でないもの(切手・カタログ・書籍等) ---
            if has_any(title, NON_PRINT_WORDS) or has_any(_raw, NON_PRINT_WORDS):
                excluded["non_print"] += 1
                continue

        # --- 相場を引く(図柄優先、なければ作家) ---
        dkey = design_key(title, canonical_map)
        market = by_design.get(dkey) if dkey else None
        market_level = "design"
        if not market or not market["source_final"]:
            if strict:
                # 図柄が特定できないものは比較の土台が弱すぎるので候補にしない。
                # (作家相場は¥3,000〜¥200万と幅が広すぎて「割安」を判定できない)
                excluded["no_design"] += 1
                # ★2026-07-30 診断: 同定できないタイトルの実物を集める。
                #   415件が何で落ちているか(未登録図柄/曖昧/表記ゆれ)を
                #   推測でなく現物で判断するため
                _fn = find_bargains
                _fn.nodesign_samples = getattr(_fn, "nodesign_samples", [])
                if len(_fn.nodesign_samples) < 40:
                    _fn.nodesign_samples.append(
                        (artist or "作家不明", (r["listing_title_raw"] or "")[:60]))
                continue
            market = by_artist.get(artist)
            market_level = "artist"
        if not market or not market["source_final"]:
            excluded["no_market"] += 1
            continue

        src = summarize(market["source_final"])
        exit_f = summarize(market["exit_final"])
        exit_a = summarize(market["exit_ask"])

        ratio = price / src["median"] if src["median"] else None
        # ★2026-07-30 診断: 70%ルールで落ちる品の実態を測る。
        #   日本市場内の割引率で切っているが、事業は日本→海外の裁定取引なので
        #   本来は輸出マージン(eBay実売 / ヤフオク価格)が判定軸のはず。
        #   まず「捨てている613件に輸出妙味があるか」を数えるための計測。
        if ratio is not None:
            _fn = find_bargains
            _fn.funnel = getattr(_fn, "funnel", {
                "total": 0, "cheap_jp": 0, "has_exit": 0,
                "export_2x": 0, "export_3x": 0, "dropped_by_ratio": 0})
            f = _fn.funnel
            f["total"] += 1
            if ratio <= 0.7:
                f["cheap_jp"] += 1
            else:
                f["dropped_by_ratio"] += 1
            if exit_f and exit_f.get("p25"):
                f["has_exit"] += 1
                m = exit_f["p25"] / price if price else 0
                if m >= 2.0:
                    f["export_2x"] += 1
                if m >= 3.0:
                    f["export_3x"] += 1
        if ratio is None or ratio > 0.7:   # 相場の70%超は「割安」と呼ばない
            continue

        # --- サイズ互換チェック(SIZE_HANDLING.md v1.1 5.3節) ---
        # ★非互換なら割安率を出さない。「13%激安」の誤表示より
        #   「比較不能」の方が安全。
        size_note = None
        size_penalty = 0
        _st = r["listing_title_raw"] or title
        cand_area = usable_area(_st)
        if is_set_item(_st):
            size_note = "セット物 — 面積比較適用外"
        elif cand_area:
            m_areas = sorted(market.get("areas", []))
            if len(m_areas) >= 3:
                m_median = m_areas[len(m_areas) // 2]
                fold = max(cand_area, m_median) / max(
                    min(cand_area, m_median), 1.0)
                if fold >= 4.0:
                    # 非互換: 候補にしない(誤った激安表示を止める)
                    excluded["size_incompatible"] += 1
                    continue
                elif fold >= 2.0:
                    size_penalty = -15
                    size_note = (f"サイズ乖離(面積比{fold:.1f}倍) — "
                                 f"割安率は参考値")
            else:
                size_note = "サイズ未確認(相場側の寸法データ不足)"
        else:
            size_note = None   # 候補の寸法不明。注記なし(大半がこれのため)

        # 即決価格と現在入札額は意味が違うので、別々に保持する。
        buyout_ratio = (buyout / src["median"]) if (buyout and src["median"]) else None

        # --- スコアリング ---
        score = (1 - ratio) * 100          # 安いほど高スコア(相場比50%なら50点)
        notes = []
        if size_penalty:
            score += size_penalty
        if size_note:
            notes.append(size_note)
        # 摺りの主張(タイトル明記のみ。実物の確定ではない)。
        # claim_mapがあればそれを使い、無ければ従来のキーワードで代替。
        raw = r["listing_title_raw"] or title
        claim = claim_map.get(raw)
        if claim is not None:
            if claim == "early_or_lifetime_claimed":
                score += 15; notes.append("初摺・生前の表記(主張)")
            elif claim == "later_claimed":
                score -= 10; notes.append("後摺表記")
            elif claim == "no_claim":
                # ★摺りの記載なし。市場実態として後摺の可能性が高い層。
                #   ただし断定はしない。リスクとして小さく割り引くだけ。
                score -= 5; notes.append("摺り記載なし(後摺の可能性)")
        else:
            if has_any(title, EARLY_WORDS):
                score += 15; notes.append("初摺表記")
            if has_any(title, LATER_WORDS):
                score -= 10; notes.append("後摺表記")
        if has_any(title, PUBLISHER_WORDS):
            score += 10; notes.append("版元名あり")
        # ★「真作保証」への加点は廃止した。
        #   出品者の宣伝文句であって初摺の証拠にならない(実データで39%に付いている)。
        #   表示だけ残し、点は動かさない。
        if has_any(title, GUARANTEE_WORDS):
            notes.append("真作表記(主張のみ・加点なし)")
        if ratio < 0.10:
            score -= 25; notes.append("安すぎ注意(要実物確認)")
        if market_level == "artist":
            score -= 15; notes.append("図柄同定できず作家相場で比較")
        if src["n"] < 5:
            excluded["low_n"] = excluded.get("low_n", 0) + 1
            continue
        if src["n"] < 8:
            score -= 15; notes.append(f"相場データ{src['n']}件のみ(参考値)")
        if bids == 0 and r["listing_type"] == "auction":
            notes.append("入札0")
        if observation_rejected:
            why = r["observation_note_latest"] or (
                f"source={obs_source or '-'} conf={obs_conf if obs_conf is not None else '-'}")
            notes.append("最新観測価格は信頼度不足のため不採用: " + str(why)[:100])
        if pricing_basis == "buyout_fallback":
            notes.append("現在額は不採用。明示的な即決価格で評価")
        elif pricing_basis == "collection_fixed_ask":
            notes.append("固定価格は収集時点(信頼済み観測なし)")
        elif price_is_stale:
            notes.append("価格は信頼済み最新観測ではない")
        elif r["observed_at_latest"]:
            # ★observed_at は UTC 保存(鉄則9)。
            #   表示は +9h して JST、鮮度判定は UTC の現在時刻と比べる。
            #   以前は datetime.now()(Mac上ではJST)と直接比較していて、
            #   「観測が古い(24h超)」が実際は15時間で誤点灯していた。
            obs_str = str(r["observed_at_latest"])[:16]
            try:
                from datetime import datetime, timedelta, timezone
                obs_dt = datetime.strptime(obs_str, "%Y-%m-%d %H:%M")
                jst = obs_dt + timedelta(hours=9)
                notes.append("観測 " + jst.strftime("%m-%d %H:%M") + " JST")
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                if (now_utc - obs_dt).total_seconds() > 86400:
                    notes.append("観測が古い(24h超)")
            except ValueError:
                notes.append("観測 " + obs_str)
        # ★残り時間は意味のある値のときだけ出す。
        #   観測時のパースが崩れて「時間」だけの断片になることがあり、
        #   それをそのまま出すと「残り 時間」という無意味な表示になる。
        tl = r["time_left_latest"]
        if tl and re.search(r"\d", str(tl)):
            notes.append("残り " + str(tl))

        candidates.append({
            "txn_id": r["txn_id"],
            "artist": artist,
            "title": title[:60],
            "platform": r["platform"],
            "listing_type": r["listing_type"],
            "price": price,
            "bid_count": bids,
            "time_left": r["time_left_latest"],
            "observed_at": r["observed_at_latest"],
            "price_source": obs_source if obs_trusted else pricing_basis,
            "price_confidence": float(obs_conf) if obs_trusted else None,
            "pricing_basis": pricing_basis,
            "observation_price_rejected": observation_rejected,
            "url": r["source_url"],
            "design_key": dkey,
            "market_level": market_level,
            "ratio": round(ratio, 3),
            "buyout": buyout,
            "buyout_ratio": round(buyout / src["median"], 3) if (buyout and src["median"]) else None,
            "state": (
                "sold" if r["status_latest"] in ("落札", "終了", "終了(価格不明)", "消滅")
                else ("stock" if r["listing_type"] == "fixed"
                      else ("bid" if (bids or 0) > 0
                            else "nobid"))),
            "score": round(score, 1),
            "source_median": src["median"], "source_n": src["n"],
            "exit_final_p25": exit_f["p25"] if exit_f else None,
            "exit_final_n": exit_f["n"] if exit_f else 0,
            "exit_ask_median": exit_a["median"] if exit_a else None,
            "notes": notes,
        })

    candidates.sort(key=lambda c: -c["score"])
    con.close()
    result = candidates[:limit]
    # 除外の内訳を付帯情報として持たせる(表示用。listのままなので既存呼び出しは壊れない)
    find_bargains.last_excluded = excluded
    find_bargains.last_total = len(candidates)
    return result


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "shinhanga.db"
    out_json = None
    if "--json" in sys.argv:
        i = sys.argv.index("--json")
        out_json = sys.argv[i + 1] if len(sys.argv) > i + 1 else "bargains.json"

    # AI正規化マップがあれば読む(normalize_titles.py の出力)
    canonical_map = None
    try:
        with open("canonical_titles.json", encoding="utf-8") as f:
            canonical_map = json.load(f)
        print(f"AI正規化マップ読込: {len(canonical_map)}件")
    except FileNotFoundError:
        print("AI正規化マップなし(ルールベースのみで同定)")

    strict = "--loose" not in sys.argv
    cands = find_bargains(db, canonical_map, strict=strict)

    ex = getattr(find_bargains, "last_excluded", {})
    total = getattr(find_bargains, "last_total", len(cands))
    print(f"\n{'='*72}")
    print(f"掘り出し物候補: {len(cands)}件 (条件を満たした全件: {total})")
    if ex:
        print(f"除外 — 複製・復刻 {ex.get('replica',0)} / "
              f"非版画(切手・カタログ等) {ex.get('non_print',0)} / "
              f"分類不能 {ex.get('unclassified',0)} / "
              f"サイズ非互換 {ex.get('size_incompatible',0)} / "
              f"図柄同定不能 {ex.get('no_design',0)} / "
              f"相場データなし {ex.get('no_market',0)} / "
              f"信頼価格なし {ex.get('no_trusted_active_price',0)}")
        ns = getattr(find_bargains, "nodesign_samples", [])
        if ns and "--nodesign" in sys.argv:
            print(f"\n[図柄同定不能のサンプル {len(ns)}件]")
            for a, t in ns:
                print(f"  {a} | {t}")
        f = getattr(find_bargains, "funnel", None)
        if f:
            print(f"\n[漏斗診断] 全条件通過 {f['total']}件 / "
                  f"日本相場70%以下 {f['cheap_jp']}件 / "
                  f"70%超で除外 {f['dropped_by_ratio']}件")
            print(f"           eBay実売データあり {f['has_exit']}件 / "
                  f"輸出2倍以上 {f['export_2x']}件 / 3倍以上 {f['export_3x']}件")
        if ex.get('no_trusted_active_price', 0) > 0:
            print("  ※『信頼価格なし』は導入直後に多発する(旧観測を隔離したため)。"
                  "新observe実行の翌日から自然に減る")
        if strict and ex.get("no_design", 0) > 0:
            print("  ※図柄同定不能を候補に含めるには --loose、"
                  "同定率を上げるには normalize_titles.py を実行")
    print(f"{'='*72}")
    for i, c in enumerate(cands, 1):
        price = f"¥{int(c['price']):,}"
        med = f"¥{int(c['source_median']):,}"
        exit_info = ""
        if c["exit_final_p25"]:
            exit_info = f" / 出口25%点 ¥{int(c['exit_final_p25']):,}(n={c['exit_final_n']})"
        print(f"\n{i}. [{c['score']:.0f}点] {c['artist']} — {c['title']}")
        print(f"   {c['platform']} {price} ({c['bid_count'] if c['bid_count'] is not None else '-'}入札)"
              f" = 仕入相場中央値{med}(n={c['source_n']})の {c['ratio']*100:.0f}%{exit_info}")
        if c["notes"]:
            print(f"   注記: {', '.join(c['notes'])}")

    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(cands, f, ensure_ascii=False, indent=1)
        print(f"\nJSON出力: {out_json}")


if __name__ == "__main__":
    main()
