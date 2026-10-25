"""HYPE BOT - tek dosya. Reddit'ten (resmi API, salt okunur) veri toplar, HYPE / YON / FEAR / GREED /
MANIPULASYON skorlarini hesaplar ve SQLite'a kaydeder.

GUVENLIK
  - API anahtarlari bu dosyada, ortam degiskenlerinde, logda veya veritabaninda TUTULMAZ.
    `setup` komutu onlari gizli girisle (ekranda gorunmez) alir ve isletim sisteminin kasasina kaydeder
    (Windows Kimlik Bilgisi Yoneticisi; macOS Keychain; Linux Secret Service).
  - Yazar adlari, kurulumda rastgele uretilen ve yine kasada duran bir tuzla hash'lenir; veritabani sizsa bile
    kullanici adlarina geri donulemez. Metinler RETENTION_DAYS gun sonra silinir.
  - Reddit baglantisi salt okunurdur (yazma, oylama, mesaj yetkisi yok). Ag erisimi sadece Reddit'e
    (ve --claude secilirse Anthropic API'sine) yapilir.
  - `score` ve `status` komutlari veritabanini SALT OKUNUR acar ve hicbir anahtara ihtiyac duymaz.

KOMUTLAR
  python hype_bot.py setup             # anahtarlari bir kez gir (kendi terminalinde)
  python hype_bot.py run --loop 10     # her 10 dakikada bir topla + skorla
  python hype_bot.py score             # son tamamlanmis pencerenin skorlari (salt okunur)
  python hype_bot.py status            # kaynak sagligi ve veri miktari (salt okunur)
  python hype_bot.py forget            # kasadaki tum anahtarlari sil
  python hype_bot.py selftest          # anahtarsiz, agsiz cevrimdisi test

Gerekli paketler: pip install praw keyring pandas numpy   (Claude siniflandirici icin: anthropic)
"""
import argparse
import getpass
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

# =============================================================================
# AYARLAR
# =============================================================================
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "hype.db")
KEYRING_SERVICE = "hype-bot"
RETENTION_DAYS = 60
WINDOW = "1D"                       # skor penceresi (1D veya 1h)
BASELINE_WINDOWS, MIN_BASELINE = 14, 5
SUBREDDITS = ["CryptoCurrency", "CryptoMarkets", "Bitcoin", "ethereum", "solana", "dogecoin",
              "SatoshiStreetBets", "CryptoMoonShots"]
FETCH_LIMIT = 300
STALE_HOURS = 2

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("hype_bot")
for noisy in ("praw", "prawcore", "urllib3", "httpx", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)    # istek ayrintilari (basliklar) loglanmasin


# =============================================================================
# 1) KIMLIK BILGISI KASASI
# =============================================================================
SECRET_KEYS = ("reddit_client_id", "reddit_client_secret", "reddit_user_agent", "hash_salt", "anthropic_api_key")


def get_secret(name: str) -> str | None:
    import keyring
    return keyring.get_password(KEYRING_SERVICE, name)


def set_secret(name: str, value: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, name, value)


def delete_secret(name: str) -> None:
    import keyring
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except Exception:
        pass


def cmd_setup(_args):
    print("Anahtarlar ekranda GORUNMEZ ve sadece isletim sistemi kasasina kaydedilir.\n")
    cid = getpass.getpass("Reddit client id: ").strip()
    csec = getpass.getpass("Reddit client secret: ").strip()
    user = input("Reddit kullanici adin (user agent icin, ornek: ahmet123): ").strip()
    if not cid or not csec:
        sys.exit("Iptal: client id ve secret bos olamaz.")
    set_secret("reddit_client_id", cid)
    set_secret("reddit_client_secret", csec)
    set_secret("reddit_user_agent", f"python:hype-bot:0.2 (by u/{user or 'unknown'})")
    if not get_secret("hash_salt"):
        set_secret("hash_salt", secrets.token_hex(32))           # yazar hash'leri icin rastgele tuz
    if input("Claude siniflandirici icin Anthropic API anahtari eklensin mi? (e/h): ").strip().lower() == "e":
        set_secret("anthropic_api_key", getpass.getpass("Anthropic API anahtari: ").strip())
    del cid, csec
    print("\nKaydedildi. Baglanti test ediliyor (salt okunur, 1 gonderi)...")
    try:
        next(iter(reddit_client().subreddit("CryptoCurrency").new(limit=1)))
        print("Reddit baglantisi BASARILI.")
    except Exception as e:
        print(f"Reddit baglantisi BASARISIZ: {type(e).__name__}. Anahtarlari kontrol edip setup'i tekrar calistir.")


def cmd_forget(_args):
    for k in SECRET_KEYS:
        delete_secret(k)
    print("Kasadaki tum hype-bot anahtarlari silindi. (Veritabani silinmedi.)")


# =============================================================================
# 2) MESAJ, COIN TESPITI, SINIFLANDIRMA
# =============================================================================
@dataclass
class Message:
    source: str
    channel: str
    author: str
    text: str
    ts: datetime
    followers: int = 0          # Reddit'te gonderi puani
    msg_id: str = ""


@dataclass
class Label:
    coins: list = field(default_factory=list)
    stance: str = "neutral"
    intensity: float = 0.0
    emotion: str = "none"
    is_promo: bool = False
    is_spam: bool = False


COIN_ALIASES = {
    "BTC": ["btc", "bitcoin", "xbt"], "ETH": ["eth", "ethereum", "ether"], "SOL": ["sol", "solana"],
    "XRP": ["xrp", "ripple"], "DOGE": ["doge", "dogecoin"], "PEPE": ["pepe"], "BNB": ["bnb"],
    "AVAX": ["avax", "avalanche"], "ADA": ["cardano"], "LINK": ["chainlink"], "SHIB": ["shib", "shiba inu", "shiba"],
    "DOT": ["polkadot"], "LTC": ["ltc", "litecoin"], "TON": ["toncoin"], "SUI": ["sui"], "TRX": ["trx", "tron"],
}
_ALIAS_RE = {c: re.compile(r"(?<![a-z0-9])[$#]?(?:%s)(?![a-z0-9])" % "|".join(map(re.escape, a)), re.I)
             for c, a in COIN_ALIASES.items()}
_CASHTAG = re.compile(r"[$#]([A-Za-z]{2,10})\b")


def extract_coins(text: str) -> list:
    found = {c for c, rx in _ALIAS_RE.items() if rx.search(text)}
    found |= {m.upper() for m in _CASHTAG.findall(text) if m.upper() in COIN_ALIASES}
    return sorted(found)


LEX = {
    "bull": ["moon", "pump", "ath", "rally", "bullish", "breakout", "send it", "uçacak", "ucacak", "roket",
             "yükseliş", "yukselis", "boğa", "boga", "alım fırsatı", "gem", "100x", "🚀", "📈"],
    "bear": ["dump", "crash", "bearish", "rug", "rekt", "düşüş", "dusus", "çöküş", "çöktü", "coktu",
             "ayı", "dibe", "scam", "dolandırıcı", "📉"],
    "fear": ["panic", "panik", "korku", "scared", "afraid", "liquidat", "likide", "likidasyon", "hack",
             "exploit", "kaçın", "batıyor", "depeg", "insolvent", "iflas"],
    "greed": ["fomo", "kaçırma", "lambo", "zengin", "son şans", "last chance", "don't miss", "100x", "1000x", "garanti"],
    "promo": ["presale", "airdrop", "giveaway", "çekiliş", "join", "katıl", "vip", "sinyal grubu", "signal group",
              "referral", "promo code", "dm me", "link in bio", "t.me/", "guaranteed", "whitelist"],
}


class LexiconClassifier:
    name = "sozluk"

    def classify(self, texts):
        return [self._one(t) for t in texts]

    def _one(self, text):
        t = text.lower() + " "
        h = {k: sum(t.count(w) for w in v) for k, v in LEX.items()}
        stance = "bullish" if h["bull"] > h["bear"] else "bearish" if h["bear"] > h["bull"] else "neutral"
        shout = text.count("!") + sum(ch.isupper() for ch in text) / max(len(text), 1) * 10
        intensity = min(1.0, 0.25 * max(h["bull"], h["bear"], h["fear"], h["greed"]) + 0.05 * shout)
        emotion = ("panic" if h["fear"] >= 2 else "fear" if h["fear"] else "fomo" if h["greed"] and h["bull"]
                   else "greed" if h["greed"] else "calm" if stance == "neutral" else "none")
        return Label(extract_coins(text), stance, round(intensity, 3), emotion, h["promo"] > 0, len(text.strip()) < 4)


class ClaudeClassifier:
    """Istege bagli, UCRETLI. Anahtar kasadan okunur. Varsayilan model Claude Opus 5.5; daha ucuzu icin
    model="claude-haiku-5-5". Reddedilen veya bozuk yanit gelen grup sozlukle etiketlenir."""
    name = "claude"
    BATCH = 25
    SYSTEM = ("You label short crypto social-media messages (Turkish and English, slang, irony) for a sentiment "
              "index. For each message return coins (tickers it is about), stance (bullish/bearish/neutral, irony "
              "by real meaning), intensity 0..1, emotion (greed/fomo/fear/panic/anger/calm/none), is_promo "
              "(ads, shilling, presales, airdrops, signal/VIP groups, referral links, guaranteed-profit claims), "
              "is_spam (bot-like or off-topic). Label what is written, not whether it is true. Exactly one "
              "result per input, same order, using the given index.")
    SCHEMA = {"type": "object", "additionalProperties": False, "required": ["results"], "properties": {"results": {
        "type": "array", "items": {"type": "object", "additionalProperties": False,
                                   "required": ["index", "coins", "stance", "intensity", "emotion", "is_promo", "is_spam"],
                                   "properties": {"index": {"type": "integer"},
                                                  "coins": {"type": "array", "items": {"type": "string"}},
                                                  "stance": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
                                                  "intensity": {"type": "number"},
                                                  "emotion": {"type": "string", "enum": ["greed", "fomo", "fear", "panic",
                                                                                         "anger", "calm", "none"]},
                                                  "is_promo": {"type": "boolean"}, "is_spam": {"type": "boolean"}}}}}}

    def __init__(self, model="claude-opus-5-5"):
        import anthropic
        key = get_secret("anthropic_api_key")
        if not key:
            sys.exit("Anthropic anahtari kasada yok. `python hype_bot.py setup` ile ekle veya --claude kullanma.")
        self.client, self.model = anthropic.Anthropic(api_key=key), model
        self.fallback, self.cache = LexiconClassifier(), {}

    def classify(self, texts):
        todo = list(dict.fromkeys(t for t in texts if t not in self.cache))
        for i in range(0, len(todo), self.BATCH):
            chunk = todo[i:i + self.BATCH]
            for t, lb in zip(chunk, self._batch(chunk)):
                self.cache[t] = lb
        return [self.cache[t] for t in texts]

    def _batch(self, chunk):
        payload = "\n".join(json.dumps({"index": i, "text": t[:1000]}, ensure_ascii=False) for i, t in enumerate(chunk))
        kw = dict(model=self.model, max_tokens=8000, system=self.SYSTEM,
                  messages=[{"role": "user", "content": f"Label these {len(chunk)} messages:\n{payload}"}],
                  output_config={"effort": "low", "format": {"type": "json_schema", "schema": self.SCHEMA}})
        try:
            if self.model.startswith("claude-haiku"):
                r = self.client.messages.create(**kw)
            else:
                r = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
            if r.stop_reason != "end_turn":
                return self.fallback.classify(chunk)
            res = {x["index"]: x for x in json.loads(next(b.text for b in r.content if b.type == "text"))["results"]}
        except Exception as e:
            log.warning("Claude siniflandirma hatasi (%s), sozluk kullaniliyor", type(e).__name__)
            return self.fallback.classify(chunk)
        out = []
        for i, t in enumerate(chunk):
            x = res.get(i)
            if x is None:
                out.append(self.fallback.classify([t])[0])
                continue
            coins = sorted({c.upper().lstrip("$#") for c in x["coins"]} | set(extract_coins(t)))
            out.append(Label(coins, x["stance"], float(min(max(x["intensity"], 0), 1)), x["emotion"],
                             x["is_promo"], x["is_spam"]))
        return out


# =============================================================================
# 3) DEPOLAMA
# =============================================================================
class Store:
    def __init__(self, path=DB_PATH, read_only=False, salt=None):
        self.salt = salt
        if read_only:
            if not os.path.exists(path):
                sys.exit(f"Veritabani yok: {path}. Once `run` calistir.")
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (msg_id TEXT PRIMARY KEY, source TEXT, channel TEXT,
                author_hash TEXT, text TEXT, ts TEXT, followers INTEGER, inserted_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
            CREATE TABLE IF NOT EXISTS health (source TEXT, run_at TEXT, fetched INTEGER, new INTEGER,
                ok INTEGER, error TEXT);
            CREATE TABLE IF NOT EXISTS scores (pencere TEXT, coin TEXT, mesaj REAL, yazar REAL, HYPE REAL,
                HAM_HYPE REAL, YON REAL, FEAR REAL, GREED REAL, MANIPULASYON REAL, GUVEN REAL, etiket TEXT,
                PRIMARY KEY (pencere, coin));
        """)

    def _hash(self, source, author):
        return hashlib.sha256(f"{self.salt}|{source}|{author}".encode()).hexdigest()[:16]

    def save(self, msgs):
        now = _utcnow().isoformat(timespec="seconds")
        rows = [(m.msg_id, m.source, m.channel, self._hash(m.source, m.author), m.text,
                 m.ts.isoformat(timespec="seconds"), m.followers, now) for m in msgs if m.msg_id]
        before = self.db.total_changes
        self.db.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?)", rows)
        self.db.commit()
        return self.db.total_changes - before

    def load(self, since):
        cur = self.db.execute("SELECT source, channel, author_hash, text, ts, followers, msg_id FROM messages "
                              "WHERE ts >= ? ORDER BY ts", (since.isoformat(timespec="seconds"),))
        return [Message(s, ch, a, t, datetime.fromisoformat(ts), f, mid) for s, ch, a, t, ts, f, mid in cur]

    def save_scores(self, df):
        d = df.reset_index()
        d["pencere"] = d["pencere"].astype(str)
        cols = ["pencere", "coin", "mesaj", "yazar", "HYPE", "HAM_HYPE", "YON", "FEAR", "GREED", "MANIPULASYON",
                "GUVEN", "etiket"]
        rows = [tuple(None if (isinstance(v, float) and math.isnan(v)) else v for v in r) for r in d[cols].itertuples(index=False)]
        self.db.executemany(f"INSERT OR REPLACE INTO scores VALUES ({','.join('?' * len(cols))})", rows)
        self.db.commit()

    def log_health(self, source, fetched, new, error=None):
        self.db.execute("INSERT INTO health VALUES (?,?,?,?,?,?)",
                        (source, _utcnow().isoformat(timespec="seconds"), fetched, new, int(error is None), error))
        self.db.commit()

    def last_success(self, source):
        row = self.db.execute("SELECT MAX(run_at) FROM health WHERE source=? AND ok=1", (source,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def purge(self, days=RETENTION_DAYS):
        cutoff = (_utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
        n = self.db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,)).rowcount
        self.db.commit()
        return n


# =============================================================================
# 4) REDDIT TOPLAYICI (resmi API, salt okunur)
# =============================================================================
def reddit_client():
    import praw
    cid, csec, ua = (get_secret(k) for k in ("reddit_client_id", "reddit_client_secret", "reddit_user_agent"))
    if not cid or not csec:
        sys.exit("Reddit anahtarlari kasada yok. Once kendi terminalinde: python hype_bot.py setup")
    r = praw.Reddit(client_id=cid, client_secret=csec, user_agent=ua or "python:hype-bot:0.2")
    r.read_only = True
    return r


def fetch_reddit(reddit, subreddits=SUBREDDITS, limit=FETCH_LIMIT):
    ts = lambda x: datetime.fromtimestamp(x.created_utc, tz=timezone.utc).replace(tzinfo=None)
    author = lambda x: str(x.author) if x.author is not None else "[deleted]"
    out = []
    for name in subreddits:
        sub = reddit.subreddit(name)
        for p in sub.new(limit=limit):
            out.append(Message("reddit", f"r/{name}", author(p), f"{p.title}\n{p.selftext or ''}".strip()[:2000],
                               ts(p), max(int(p.score), 0), f"reddit:t3_{p.id}"))
        for c in sub.comments(limit=limit):
            out.append(Message("reddit", f"r/{name}", author(c), (c.body or "")[:2000], ts(c),
                               max(int(c.score), 0), f"reddit:t1_{c.id}"))
    return out


# =============================================================================
# 5) SKOR MOTORU
# =============================================================================
SOURCE_WEIGHT = {"x": 1.0, "reddit": 1.0, "forum": 1.0, "telegram": 0.8, "instagram": 0.7}
POLARITY = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}
AUTHOR_CAP, DUP_MIN = 3, 3


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def score_messages(msgs, classifier, window=WINDOW):
    df = pd.DataFrame([m.__dict__ for m in msgs])
    df["ts"] = pd.to_datetime(df["ts"])
    df["win"] = df["ts"].dt.floor(window)
    df["norm"] = df["text"].str.lower().str.replace(r"https?://\S+|t\.me/\S+", " ", regex=True) \
        .str.replace(r"[0-9]+", "#", regex=True).str.replace(r"\W+", " ", regex=True).str.strip()
    df["dup"] = df.groupby(["win", "norm"])["norm"].transform("size") >= DUP_MIN
    labels = classifier.classify(df["text"].tolist())
    for f in ("coins", "stance", "intensity", "emotion", "is_promo", "is_spam"):
        df[f] = [getattr(lb, f) for lb in labels]
    df["w"] = (df["source"].map(SOURCE_WEIGHT).fillna(0.8) * (1 + np.log10(1 + df["followers"].clip(lower=0)) / 3)
               * (~df["is_spam"]))
    df["target"] = df["coins"].map(lambda c: list(c) + ["MARKET"])
    df = df.drop(columns=["text"]).explode("target")
    rank = df.groupby(["win", "target", "author"]).cumcount()
    df["w"] = df["w"] * np.where(rank < AUTHOR_CAP, 1.0, 0.2)
    df["organic"] = ~df["is_promo"] & ~df["is_spam"]
    df["wo"] = df["w"] * df["organic"]
    df["pol"] = df["stance"].map(POLARITY) * (0.5 + 0.5 * df["intensity"])

    rows = []
    for (win, coin), g in df.groupby(["win", "target"]):
        wo, org = g["wo"].sum(), g[g["organic"]]
        promo_w = (g["w"] * g["is_promo"]).sum()
        share = lambda mask: org.loc[mask, "wo"].sum() / wo if wo > 0 else 0.0
        by_author = g.groupby("author")["w"].sum().sort_values(ascending=False)
        rows.append({"win": win, "coin": coin, "mesaj": len(g), "yazar": org["author"].nunique(),
                     "att": math.log1p(wo), "att_all": math.log1p(wo + promo_w),
                     "sent": (org["wo"] * org["pol"]).sum() / wo if wo > 0 else 0.0,
                     "fear_raw": 0.6 * share(org["emotion"].isin(["fear", "panic"])) + 0.4 * share(org["stance"] == "bearish"),
                     "greed_raw": 0.6 * share(org["emotion"].isin(["greed", "fomo"])) + 0.4 * share(org["stance"] == "bullish"),
                     "promo_share": promo_w / max(promo_w + wo, 1e-9), "dup_share": g["dup"].mean(),
                     "conc": by_author.head(3).sum() / by_author.sum() if by_author.sum() > 0 else 0.0})
    s = pd.DataFrame(rows)
    out = []
    for coin, g in s.groupby("coin"):
        g = g.set_index("win").sort_index()
        g = g.reindex(pd.date_range(g.index.min(), g.index.max(), freq=window))
        for c in ("mesaj", "yazar", "att", "att_all"):
            g[c] = g[c].fillna(0)
        for col, name in (("att", "HYPE"), ("att_all", "HAM_HYPE")):
            past = g[col].shift(1).rolling(BASELINE_WINDOWS, min_periods=MIN_BASELINE)
            z = (g[col] - past.mean()) / past.std().replace(0, np.nan)
            g[name] = (100 * z.map(lambda v: _phi(v) if pd.notna(v) else np.nan)).round(0)
        g["YON"] = (100 * g["sent"]).round(0)
        g["FEAR"] = (100 * g["fear_raw"].clip(0, 1)).round(0)
        g["GREED"] = (100 * g["greed_raw"].clip(0, 1)).round(0)
        manip = 0.45 * g["promo_share"] + 0.35 * g["dup_share"] + 0.20 * ((g["conc"] - 0.3).clip(lower=0) / 0.7)
        g["MANIPULASYON"] = (100 * manip.clip(0, 1)).round(0)
        g["GUVEN"] = (1 - np.exp(-g["yazar"] / 15)).round(2)
        g["etiket"] = [_label(r) for r in g.itertuples()]
        g["coin"] = coin
        out.append(g)
    res = pd.concat(out)[["coin", "mesaj", "yazar", "HYPE", "HAM_HYPE", "YON", "FEAR", "GREED", "MANIPULASYON",
                          "GUVEN", "etiket"]]
    res.index.name = "pencere"
    return res


def _label(r):
    if pd.isna(r.HYPE) or r.GUVEN < 0.3:
        return "yetersiz veri"
    if r.HAM_HYPE >= 90 and r.HYPE < 75 and r.MANIPULASYON >= 45:
        return "SUNI HYPE (reklam/shill, organik ilgi yok)"
    parts = []
    if r.HYPE >= 90:
        parts.append("ASIRI HYPE")
    elif r.HYPE >= 75:
        parts.append("yuksek ilgi")
    if parts:
        parts += ["SUPHELI/SUNI" if r.MANIPULASYON >= 45 else "organik",
                  "olumlu" if r.YON > 15 else "olumsuz" if r.YON < -15 else "karisik"]
    if r.FEAR >= 50:
        parts.append("KORKU")
    elif r.GREED >= 50:
        parts.append("ACGOZLULUK")
    return ", ".join(parts) or "normal"


# =============================================================================
# 6) KOMUTLAR
# =============================================================================
def run_cycle(store, reddit, classifier):
    try:
        msgs = fetch_reddit(reddit)
        new = store.save(msgs)
        store.log_health("reddit", len(msgs), new)
        log.info("reddit: %d mesaj cekildi, %d yeni", len(msgs), new)
    except Exception as e:
        store.log_health("reddit", 0, 0, type(e).__name__)      # sadece hata turu; ayrinti/basliklar yazilmaz
        log.error("reddit HATA: %s", type(e).__name__)
    store.purge()
    msgs = store.load(_utcnow() - timedelta(days=BASELINE_WINDOWS + 7))
    if msgs:
        store.save_scores(score_messages(msgs, classifier))


def cmd_run(args):
    salt = get_secret("hash_salt")
    if not salt:
        sys.exit("Kurulum yapilmamis. Once kendi terminalinde: python hype_bot.py setup")
    store, reddit = Store(salt=salt), reddit_client()
    clf = ClaudeClassifier() if args.claude else LexiconClassifier()
    while True:
        run_cycle(store, reddit, clf)
        if not args.loop:
            break
        time.sleep(args.loop * 60)


def cmd_score(args):
    store = Store(args.db or DB_PATH, read_only=True)
    cur = pd.Timestamp(_utcnow()).floor(WINDOW).isoformat(sep=" ")
    df = pd.read_sql_query("SELECT * FROM scores WHERE pencere < ? AND pencere = "
                           "(SELECT MAX(pencere) FROM scores WHERE pencere < ?)", store.db, params=(cur, cur))
    if df.empty:
        print("Henuz tamamlanmis pencere skoru yok.")
        return
    last = store.last_success("reddit")
    if last is None or _utcnow() - last > timedelta(hours=STALE_HOURS):
        print(f"UYARI: son {STALE_HOURS} saatte basarili toplama yok (son: {last}). Skorlar eski olabilir.\n")
    print(f"Pencere: {df.pencere.iloc[0]} (tamamlanmis)\n")
    with pd.option_context("display.width", 200, "display.max_rows", 100):
        print(df.drop(columns="pencere").set_index("coin").sort_values("HYPE", ascending=False, na_position="last").to_string())


def cmd_status(args):
    store = Store(args.db or DB_PATH, read_only=True)
    q = lambda sql: store.db.execute(sql).fetchall()
    print("Mesaj sayisi:", q("SELECT source, COUNT(*), MIN(ts), MAX(ts) FROM messages GROUP BY source"))
    print("Son 5 toplama:", q("SELECT run_at, fetched, new, ok, error FROM health ORDER BY run_at DESC LIMIT 5"))
    print("Skor pencere sayisi:", q("SELECT COUNT(DISTINCT pencere) FROM scores")[0][0])


def cmd_selftest(_args):
    """Anahtarsiz, agsiz test: sahte Reddit nesneleri -> gecici veritabani -> skor."""
    from types import SimpleNamespace
    import random
    random.seed(1)
    now = _utcnow().replace(minute=0, second=0, microsecond=0)
    posts, cmts = [], []
    for d in range(12, -1, -1):
        day = (now - timedelta(days=d)).replace(hour=12)
        for i in range(140 if d == 1 else 20):
            coin = "SOL" if d == 1 else random.choice(["BTC", "ETH", "SOL", "DOGE"])
            ts = (day + timedelta(minutes=random.randint(-600, 600))).replace(tzinfo=timezone.utc).timestamp()
            title = f"{coin} rally başladı 🚀 ath geliyor" if d == 1 else f"{coin} bugün yatay, holding"
            posts.append(SimpleNamespace(id=f"p{d}_{i}", title=title, selftext="", author=f"u{random.randint(1, 500)}",
                                         created_utc=ts, score=random.randint(0, 200)))
            cmts.append(SimpleNamespace(id=f"c{d}_{i}", body=f"{coin} ne olur sizce", author=None, created_utc=ts + 60, score=0))
    sub = SimpleNamespace(new=lambda limit: posts[:limit], comments=lambda limit: cmts[:limit])
    fake = SimpleNamespace(subreddit=lambda name: sub)
    path = os.path.join(tempfile.mkdtemp(), "selftest.db")
    store = Store(path, salt=secrets.token_hex(16))
    msgs = fetch_reddit(fake, subreddits=["test"], limit=10_000)
    assert store.save(msgs) == len(msgs) and store.save(msgs) == 0, "tekrar eleme bozuk"
    assert all(len(r[0]) == 16 for r in store.db.execute("SELECT author_hash FROM messages")), "hash bozuk"
    store.log_health("reddit", len(msgs), len(msgs))
    res = score_messages(store.load(now - timedelta(days=30)), LexiconClassifier())
    store.save_scores(res)
    spike = res[(res.coin == "SOL") & (res.index == pd.Timestamp(now - timedelta(days=1)).floor("D"))]
    assert not spike.empty and spike["HYPE"].iloc[0] >= 90, spike
    ro = Store(path, read_only=True)
    try:
        ro.db.execute("DELETE FROM messages")
        raise AssertionError("salt okunur mod yazmaya izin verdi")
    except sqlite3.OperationalError:
        pass
    print(f"SELFTEST OK: {len(msgs)} sahte mesaj, tekrar eleme, yazar hash'i, salt okunur erisim, "
          f"SOL patlamasi HYPE={spike['HYPE'].iloc[0]:.0f}")


def main():
    ap = argparse.ArgumentParser(description="Hype bot (Reddit, guvenli kurulum)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("setup").set_defaults(fn=cmd_setup)
    sp.add_parser("forget").set_defaults(fn=cmd_forget)
    r = sp.add_parser("run")
    r.add_argument("--loop", type=float, default=0, help="dakika; 0 = bir kez")
    r.add_argument("--claude", action="store_true", help="Claude siniflandirici (UCRETLI)")
    r.set_defaults(fn=cmd_run)
    for name, fn in (("score", cmd_score), ("status", cmd_status)):
        p = sp.add_parser(name)
        p.add_argument("--db", default=None)
        p.set_defaults(fn=fn)
    sp.add_parser("selftest").set_defaults(fn=cmd_selftest)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
