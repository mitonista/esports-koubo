#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eスポーツ公募レーダー / 日次コレクタ

やること
  1. 官公需情報ポータルサイト（中小企業庁）の検索APIに表記ゆれキーワードを投げ、
     全国の入札・公募情報を一括取得する。
     → 全国1,788自治体を個別に叩く必要がない。ここが効率化の肝。
  2. APIに載らない「補助金」「公募型プロポーザル」は、eスポーツ施策を持つ自治体の
     定点URLだけを直接取得して拾う。
  3. 収集した参考URLに HEAD/GET を投げ、生きているリンクだけを掲載する。
  4. data.json を書き出す（index.html が読み込む）。

API仕様: https://www.kkj.go.jp/doc/ja/api_guide.pdf
利用規約上、APIを使う旨の明記と kkj.go.jp へのリンクが必要（index.html に記載済み）。
"""

import json
import re
import sys
import time
import datetime as dt
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path(__file__).with_name("data.json")
SEED = Path(__file__).with_name("seed.json")
ARCHIVE = Path(__file__).with_name("archive.json")

# 受付終了から何日で掲載対象から外すか
RETENTION_DAYS = 30

UA = "esports-koubo-radar/1.0 (+contact: your-mail@example.com)"
KKJ_API = "http://www.kkj.go.jp/api/"

# 表記ゆれ。全角ｅ・カタカナ・英字を網羅する。
# ※APIは「検索式1 AND 検索式2」「OR」に対応。1文字だけのカナは検索エラーになる仕様のため、
#   単独の「ｅ」等は投げない。
KEYWORDS = [
    "eスポーツ",
    "ｅスポーツ",
    "イースポーツ",
    "エレクトロニックスポーツ",
    "eSports",
    "e-Sports",
    "esports",
]

# APIに載りにくい補助金・プロポーザルを拾うための定点監視先。
# ここは「eスポーツ専任部署 / 専任予算がある自治体」だけに絞る。増やしすぎない。
FIXED_SOURCES = [
    ("群馬県 ｅスポーツ・クリエイティブ推進課", "群馬県", "https://www.pref.gunma.jp/soshiki/153/"),
    ("大阪府 eスポーツ",                     "大阪府", "https://www.pref.osaka.lg.jp/o070080/e-sports.html"),
    ("埼玉県 eスポーツ",                     "埼玉県", "https://www.pref.saitama.lg.jp/a0312/esports/info.html"),
    ("富山県 eスポーツ関係人口創出事業補助金", "富山県", "https://www.pref.toyama.jp/140511/esports.html"),
    ("沖縄eスポーツ",                        "沖縄県", "https://okinawa-e-sports.com/"),
    ("北海道湧別町 eスポーツを通じたまちづくり", "北海道", "https://www.town.yubetsu.lg.jp/administration/town/detail.html?content=1224"),
    ("スポーツ庁 公募情報",                  "全国",   "https://www.mext.go.jp/sports/b_menu/boshu/index.htm"),
    ("岡山市eスポーツ産業振興事業補助金",      "岡山県", "https://www.city.okayama.jp/jigyosha/0000030386.html"),
    ("新潟県長岡市 eスポーツ開催経費補助",     "新潟県", "https://www.city.nagaoka.niigata.jp/bosyu/esports.html"),
]

# 「eスポーツ」を含んでいても拾いたくない語（施設清掃、備品廃棄など）
NEGATIVE = re.compile(r"(清掃業務|警備業務|廃棄物|樹木|除草)")

DEADLINE_PAT = [
    # 令和8年8月21日 / 令和８年８月２１日
    (re.compile(r"令和\s*([0-9０-９]{1,2})\s*年\s*([0-9０-９]{1,2})\s*月\s*([0-9０-９]{1,2})\s*日"), "reiwa"),
    # 2026年8月21日 / 2026/8/21 / 2026-08-21
    (re.compile(r"(20[0-9]{2})[年/\-]\s*([0-9]{1,2})[月/\-]\s*([0-9]{1,2})"), "ad"),
]
ZEN = str.maketrans("０１２３４５６７８９", "0123456789")


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def kkj_search(keyword: str, since: str, count: int = 500):
    """官公需情報ポータルサイト検索API。1リクエストで最大1,000件。"""
    q = urllib.parse.urlencode(
        {"Query": keyword, "Count": count, "CFT_Issue_Date": f"{since}/"},
        encoding="utf-8",
    )
    try:
        raw = fetch(f"{KKJ_API}?{q}")
    except Exception as e:
        print(f"  ! KKJ API 失敗 [{keyword}]: {e}", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ! XML パース失敗 [{keyword}]: {e}", file=sys.stderr)
        return []

    if root.find("Error") is not None:
        print(f"  ! API エラー [{keyword}]: {root.findtext('Error')}", file=sys.stderr)
        return []

    out = []
    for sr in root.iter("SearchResult"):
        g = lambda t: (sr.findtext(t) or "").strip()
        out.append({
            "key":       g("Key"),
            "title":     g("ProjectName"),
            "url":       g("ExternalDocumentURI"),
            "org":       g("OrganizationName"),
            "pref":      g("PrefectureName") or "全国",
            "city":      g("CityName"),
            "published": (g("CftIssueDate") or g("Date"))[:10],
            "category":  g("Category"),
            "ptype":     g("ProcedureType"),
            "body":      g("ProjectDescription"),
            "tender":    g("TenderSubmissionDeadline")[:10],
        })
    return out


def parse_deadline(text: str):
    """本文から締切らしき日付を拾う。複数見つかったら最も遅い日付を採用。"""
    found = []
    for pat, kind in DEADLINE_PAT:
        for m in pat.finditer(text or ""):
            a, b, c = (x.translate(ZEN) for x in m.groups())
            y = 2018 + int(a) if kind == "reiwa" else int(a)
            try:
                found.append(dt.date(y, int(b), int(c)))
            except ValueError:
                pass
    if not found:
        return None
    today = dt.date.today()
    future = [d for d in found if d >= today]
    return (max(future) if future else max(found)).isoformat()


def link_alive(url: str):
    """掲載前の生存確認。戻り値は "alive" / "blocked" / "dead"。

    自治体サイトは自動アクセスを 403 で弾くところが多い。
    「弾かれた」と「ページが消えた」は別物なので、区別せずに落とすと
    生きている案件まで消える。403/405/406/429 は blocked として残す。
    """
    if not url or not url.startswith("http"):
        return "dead"
    last = "dead"
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                if 200 <= r.status < 400:
                    return "alive"
        except urllib.error.HTTPError as e:
            if e.code in (403, 405, 406, 429):
                last = "blocked"      # 生きているがロボットを拒否している
            elif e.code in (404, 410):
                return "dead"         # 確実に消えている
            else:
                last = "blocked"
        except Exception:
            last = "blocked"          # 通信エラーは判断保留。落とさない
    return last


def is_expired(item, today: dt.date) -> bool:
    """受付終了から RETENTION_DAYS を過ぎたか。

    落とすのは status == 'closed' のものだけ。受付中・要確認・例年枠は残す。
    定点監視先（kind == '定点'）は締切の概念が無いので常に残す。
    終了扱いなのに日付が一切無い案件は、現在動いている確証が取れないため落とす。
    """
    if item.get("kind") == "定点":
        return False
    if item.get("status") != "closed":
        return False
    ref = item.get("deadline") or item.get("published")
    if not ref:
        return True
    try:
        return (today - dt.date.fromisoformat(ref)).days > RETENTION_DAYS
    except ValueError:
        return True


def classify(rec) -> str:
    t = rec["title"] + rec.get("body", "")
    if "補助金" in t or "助成" in t:
        return "補助金"
    if "プロポーザル" in t or "企画提案" in t or "委託" in t:
        return "委託"
    return "国事業" if rec["pref"] == "全国" else "委託"


def build_item(rec, verified_note):
    deadline = rec.get("tender") or parse_deadline(rec.get("body", ""))
    status = "open"
    if deadline:
        try:
            if dt.date.fromisoformat(deadline) < dt.date.today():
                status = "closed"
        except ValueError:
            deadline = None
    else:
        status = "check"

    body = re.sub(r"\s+", " ", rec.get("body", ""))[:320]
    org = rec["org"] or (rec["pref"] + rec.get("city", ""))
    return {
        "id": rec["key"] or rec["url"],
        "title": rec["title"],
        "org": org,
        "pref": rec["pref"],
        "kind": classify(rec),
        "status": status,
        "published": rec["published"] or None,
        "deadline": deadline,
        "scale": rec.get("ptype") or "—",
        "summary": body or "公告本文は原典を参照してください。",
        "url": rec["url"],
        "subs": [],
        "verify": verified_note,
    }


def build_sources(curated):
    """定点監視ソース一覧。手書きの説明があればそれを使い、無いものだけ自動で補う。"""
    out = list(curated)
    have = {s["u"] for s in out}
    fallback = [
        {"k": "横断検索 / API", "n": "官公需情報ポータルサイト（中小企業庁）",
         "u": "https://www.kkj.go.jp/s/",
         "d": "国・独法・地方公共団体の入札情報を横断検索。検索APIで全国分を一括取得できる。巡回の主力。"},
        {"k": "横断検索", "n": "調達ポータル（デジタル庁）",
         "u": "https://www.p-portal.go.jp/pps-web-biz/UZA01/OZA0101",
         "d": "各府省の調達案件。国発注の補完。"},
    ] + [{"k": "定点", "n": n, "u": u, "d": f"{p}のeスポーツ施策ページ。"}
         for n, p, u in FIXED_SOURCES]
    for s in fallback:
        if s["u"] not in have:
            out.append(s)
            have.add(s["u"])
    return out


def main():
    today = dt.date.today()
    since = (today - dt.timedelta(days=180)).isoformat()
    print(f"== eスポーツ公募レーダー 収集開始 {today} (公告日 {since} 以降) ==")

    # ── 1. 横断検索 ────────────────────────────────
    hits, seen = [], set()
    for kw in KEYWORDS:
        rows = kkj_search(kw, since)
        print(f"  KKJ [{kw}] → {len(rows)} 件")
        for r in rows:
            if not r["url"] or r["key"] in seen:
                continue
            if NEGATIVE.search(r["title"]):
                continue
            seen.add(r["key"])
            hits.append(r)
        time.sleep(1.5)   # 利用規約：サーバー負荷を避ける
    print(f"  重複除去後 {len(hits)} 件")

    # ── 2. リンク生存確認 ──────────────────────────
    items, dead, blocked = [], 0, 0
    for r in hits:
        state = link_alive(r["url"])
        if state == "dead":
            dead += 1
        else:
            note = (f"HTTP確認済（{today}）" if state == "alive"
                    else f"自動確認不可・要目視（{today}）")
            items.append(build_item(r, note))
            blocked += state == "blocked"
        time.sleep(0.4)
    print(f"  掲載可 {len(items)} 件（うち自動確認不可 {blocked} 件）/ リンク切れ {dead} 件")

    # ── 3. 定点ソースをマージ ──────────────────────
    existing = {i["url"] for i in items}
    for name, pref, url in FIXED_SOURCES:
        if url in existing:
            continue
        state = link_alive(url)
        if state == "dead":
            print(f"  ! 定点ソースが消えている: {url}", file=sys.stderr)
            continue
        items.append({
            "id": url, "title": name, "org": name, "pref": pref,
            "kind": "定点", "status": "watch",
            "published": None, "deadline": None, "scale": "—",
            "summary": "eスポーツ施策の定点監視先。横断検索APIに載らない補助金・プロポーザルはここから拾う。",
            "url": url, "subs": [],
            "verify": (f"HTTP確認済（{today}）" if state == "alive"
                       else f"自動確認不可・要目視（{today}）"),
        })

    # ── 4. 手動キュレーション分を上書きマージ ──────
    # ここが要。seed.json が無いと、手で書いた要約・定点ソースの説明が
    # すべて自動生成の素っ気ない文面に置き換わる。必ずリポジトリに置くこと。
    curated_sources = []
    if SEED.exists():
        curated = json.loads(SEED.read_text(encoding="utf-8"))
        by_url = {i["url"]: i for i in items}
        for c in curated.get("items", []):
            by_url[c["url"]] = c          # 手で書いた要約を優先
        items = list(by_url.values())
        curated_sources = curated.get("sources", [])
        print(f"  seed.json を反映：案件 {len(curated.get('items', []))} 件 / "
              f"監視ソース {len(curated_sources)} 件")
    else:
        print("  ! seed.json が見つかりません。手書きの要約は反映されません。",
              file=sys.stderr)

    # ── 5. 受付終了から30日超を掲載対象から外す ───
    expired = [i for i in items if is_expired(i, today)]
    items = [i for i in items if not is_expired(i, today)]
    print(f"  掲載 {len(items)} 件 / 保持期間切れ {len(expired)} 件を archive.json へ退避")

    if expired:
        # 過去案件は消さずに貯める。「例年この時期に出る枠」の判定に前年の日程が要るため。
        past = []
        if ARCHIVE.exists():
            past = json.loads(ARCHIVE.read_text(encoding="utf-8")).get("items", [])
        merged = {i["url"]: i for i in past}
        for i in expired:
            merged[i["url"]] = i
        ARCHIVE.write_text(
            json.dumps({"updated": today.isoformat(), "items": list(merged.values())},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    items.sort(key=lambda i: (i["published"] or "0000-00-00"), reverse=True)

    data = {
        "updated": today.isoformat(),
        "items": items,
        "sources": build_sources(curated_sources),
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"== 書き出し完了: {OUT} / 全{len(items)}件 ==")


if __name__ == "__main__":
    main()
