"""otbor_model — общее ядро отбора: профили мастерских, оценка поста, градации.

Используется двумя сторонами:
  • otbor.py — строит модель (model.json), считает отчёты, сводки и отсев каналов;
  • коллектор лент — перед публикацией оценивает пост и не публикует шлак.
Только стандартная библиотека: коллектор живёт в своём venv и тянуть зависимости не должен.
"""
from __future__ import annotations
import datetime as dt, glob, json, math, os, re, statistics
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "model.json")
MASTERSKIE = "/root/masterskie"
REESTR = f"{MASTERSKIE}/shablony/REESTR-ISTOCHNIKOV.md"

# Знание мастерских. Папки проектов (конкретные клиенты) исключены: это не интерес мастерской.
KOMNATY = {
    "prod": ["/root/prod/README.md", "/root/prod/tools/*.md", "/root/prod/mehanika/*.md", f"{MASTERSKIE}/razbory/prod-*.md"],
    "dizain": ["/root/dizain/README.md", "/root/dizain/MAP.md", "/root/dizain/tools/*.md",
               "/root/dizain/.claude/skills/*/SKILL.md", f"{MASTERSKIE}/razbory/dizain-*.md"],
    "seo": ["/root/seo/README.md", "/root/seo/MAP.md", "/root/seo/instruments/[!a]*.md",
            "/root/seo/skills/*/SKILL.md", f"{MASTERSKIE}/razbory/seo-*.md"],
    "видео": [f"{MASTERSKIE}/razbory/video-*.md"],
    "эксплуатация": [f"{MASTERSKIE}/razbory/ekspl-*.md"],
    "здание": [f"{MASTERSKIE}/razbory/zdanie-*.md", f"{MASTERSKIE}/razbory/snabzhenie-*.md",
               f"{MASTERSKIE}/shablony/STANDART-SKILLA.md"],
}
REESTR_KOMNATA = {"кандидат «видео»": "видео", "кандидат «эксплуатация»": "эксплуатация", "снабжение": "здание"}
# Параллельные рубрики: свой раздел сводки, но в выбор мастерской, градации, шлак и отсев не входят —
# публикация и сводки мастерских от них не меняются. idf общий, документы рубрики в idf не идут.
# «бренд» — новости для «Адмирал читает новость» (/root/lichnyy-brend/STRATEGIYA-zametnost-AI-ekspert.md §4.5), 21.10.2026.
PARALLEL = {"бренд": ["/root/lichnyy-brend/otbor/BREND-PROFIL.md"]}
PCT_PARALLEL = 97     # кандидат — верхние 3% корпуса по оценке рубрики

# Градации. Пороги — перцентили оценки по всему корпусу, пересчитываются при сборке модели.
GRADES = ("сейчас", "очередь", "идея", "фон", "шлак")
PCT_SEYCHAS, PCT_OCHERED, PCT_IDEYA = 97, 90, 50
# Шлак — не публикуется коллектором. 25%, а не глубже: на 40% под нож шли новости об агентах и
# релизах (замер 16.09.2026). Новинки из шлака спасаются. Поднимать порог — по накопленным оценкам.
PCT_SHLAK = 25
SHORT_K = 12          # у поста из n токенов оценка × n/(n+K): случайное совпадение слов — не сигнал
TEMPLATE_HEAD, TEMPLATE_MIN = 5, 3   # рубрика-шаблон: одинаковые первые 5 слов в канале ≥ 3 раз — оценка ×0,5
# «Идея» — новинка (вышел, запустили, открытый код…), ни одной мастерской не близкая всерьёз.
NOVELTY = re.compile(r"\b(вышел|вышла|вышло|выпустил\w*|выкатил\w*|запустил\w*|представил\w*|анонсировал\w*|"
                     r"релиз\w*|опенсорс\w*|open source|github|стартап\w*|новый инструмент|новая модель|"
                     r"научили|сделали|придумали|собрали)\b")

# ── реклама: те же правила, что в коллекторе ───────────────────────────────────
AD = [re.compile(r"\berid\s+(?=[0-9a-z]*\d)[0-9a-z]{8,}\b"),
      re.compile(r"\bреклама\b[^\n]{0,80}?\b(?:инн|огрн|огрнип)\s+\d{10,15}\b"),
      re.compile(r"\bреклама\b[^\n]{0,80}?\bо\s+рекламодателе\b")]
def is_ad(t: str) -> bool:
    return any(r.search(t) for r in AD)

# ── токены ────────────────────────────────────────────────────────────────────
STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот
от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь
там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже
себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были
куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них какая
много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю
между это также которые который которая которых своих свой свои очень просто the and for with this that
from are you your our was were have has not but can will all its into about more than they them their what when which
who how also just one out new use using used via""".split())
_URL = re.compile(r"https?://\S+|www\.\S+")
_NW = re.compile(r"[^0-9a-zа-яё]+")

def norm(t: str) -> str:
    return _NW.sub(" ", _URL.sub(" ", (t or "").lower())).strip()

def stem(w: str) -> str:
    if re.match(r"[а-яё]", w):
        return w[:6] if len(w) > 6 else w
    return w[:-1] if len(w) > 4 and w.endswith("s") else w

def tokens(text: str) -> list[str]:
    return [stem(w) for w in text.split() if len(w) >= 3 and w not in STOP and not w.isdigit()]

def template_head(text: str) -> str:
    return " ".join(text.split()[:TEMPLATE_HEAD])

# ── профили ───────────────────────────────────────────────────────────────────
def load_docs():
    """([(комната, путь, текст, вес)], текст реестра)."""
    docs = []
    for room, pats in KOMNATY.items():
        for pat in pats:
            for f in sorted(glob.glob(pat)):
                docs.append((room, f, norm(open(f, encoding="utf-8", errors="replace").read()), 1))
    reestr = open(REESTR, encoding="utf-8").read()
    for line in reestr.split("## Хотим следить")[1].split("\n## ")[0].splitlines():
        c = [x.strip() for x in line.strip("|").split("|")]
        if len(c) >= 3 and c[0] not in ("Комната", "---"):
            docs.append((REESTR_KOMNATA.get(c[0], c[0]), "Хотим следить: " + c[1], norm(" ".join(c[1:])), 3))
    for line in reestr.split("## Источники")[1].split("\n## ")[0].splitlines():
        c = [x.strip() for x in line.strip("|").split("|")]
        if len(c) >= 3 and c[1].startswith("http"):
            room = REESTR_KOMNATA.get(c[0], c[0])
            if room in KOMNATY:
                docs.append((room, "реестр: " + c[1], norm(c[1].replace("/", " ") + " " + c[2]), 2))
    return docs, reestr

def idf_of(bags) -> dict[str, float]:
    df = Counter()
    for b in bags:
        df.update(set(b))
    n = len(bags)
    return {t: math.log((1 + n) / (1 + d)) + 1 for t, d in df.items()}

def vec(toks, idf, weight=1):
    tf = Counter(toks)
    v = {t: weight * (1 + math.log(c)) * idf.get(t, 0) for t, c in tf.items()}
    s = math.sqrt(sum(x * x for x in v.values())) or 1
    return {t: x / s for t, x in v.items()}

def room_vectors(docs, idf, exclude_rx=None):
    acc = {r: defaultdict(float) for r in KOMNATY}
    for room, path, text, w in docs:
        if room not in acc or (exclude_rx and exclude_rx.search(text)):
            continue
        for t, x in vec(tokens(text), idf, w).items():
            acc[room][t] += x
    out = {}
    for r, v in acc.items():
        s = math.sqrt(sum(x * x for x in v.values())) or 1
        out[r] = {t: x / s for t, x in v.items()}
    return out

def raw_scores(toks, rooms, idf):
    pv = vec(toks, idf)
    return {r: sum(x * rv.get(t, 0.0) for t, x in pv.items()) for r, rv in rooms.items()}

def damp(n_toks: int, template: bool) -> float:
    return n_toks / (n_toks + SHORT_K) * (0.5 if template else 1.0)

def score_corpus(posts, rooms, idf):
    """posts: dict с toks и template. Возвращает [{комната: z}] и stats {комната: (mean, std)}."""
    raws = [raw_scores(p["toks"], rooms, idf) for p in posts]
    stats = {r: (statistics.fmean(x[r] for x in raws), statistics.pstdev([x[r] for x in raws]) or 1) for r in rooms}
    out = []
    for p, raw in zip(posts, raws):
        k = damp(len(p["toks"]), p.get("template", False))
        out.append({r: (raw[r] - stats[r][0]) / stats[r][1] * k for r in rooms})
    return out, stats

def mark_templates(posts):
    heads = Counter((p["source"], template_head(p["text"])) for p in posts)
    for p in posts:
        p["template"] = heads[(p["source"], template_head(p["text"]))] >= TEMPLATE_MIN
    return {f"{s}\t{h}": n for (s, h), n in heads.items() if n >= TEMPLATE_MIN}

def percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(len(sorted_vals) * pct / 100)) - 1))
    return sorted_vals[i]

def grade_of(z: float, text: str, cut: dict) -> str:
    if z >= cut["сейчас"]:
        return "сейчас"
    if z >= cut["очередь"]:
        return "очередь"
    if z >= cut["идея"] and NOVELTY.search(text):
        return "идея"
    if z < cut["шлак"] and not NOVELTY.search(text):
        return "шлак"
    return "фон"

# ── модель: сборка и применение ───────────────────────────────────────────────
def build(posts):
    """posts — список dict(source, text) без рекламы, с текстом. Возвращает (model, posts с room/z/grade)."""
    for p in posts:
        p["toks"] = tokens(p["text"])
    heads = mark_templates(posts)
    docs, reestr = load_docs()
    idf = idf_of([p["toks"] for p in posts] + [tokens(d[2]) for d in docs])
    rooms = room_vectors(docs, idf)
    scores, stats = score_corpus(posts, rooms, idf)
    zs = []
    for p, s in zip(posts, scores):
        p["room"], p["z"] = max(s.items(), key=lambda kv: kv[1])
        zs.append(p["z"])
    zs.sort()
    cut = {"сейчас": percentile(zs, PCT_SEYCHAS), "очередь": percentile(zs, PCT_OCHERED),
           "идея": percentile(zs, PCT_IDEYA), "шлак": percentile(zs, PCT_SHLAK)}
    for p in posts:
        p["grade"] = grade_of(p["z"], p["text"], cut)
    model = {"built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "version": 1,
             "cut": cut, "stats": stats, "rooms": rooms, "idf": idf, "templates": heads,
             "percentiles": {"сейчас": PCT_SEYCHAS, "очередь": PCT_OCHERED, "идея": PCT_IDEYA, "шлак": PCT_SHLAK},
             "docs": len(docs), "posts": len(posts), "parallel": build_parallel(posts, idf)}
    return model, posts, docs, reestr, idf, rooms

def build_parallel(posts, idf):
    """{рубрика: {vec, stats, cut}} по PARALLEL; рубрика без документов пропускается."""
    out = {}
    for name, pats in PARALLEL.items():
        texts = [norm(open(f, encoding="utf-8", errors="replace").read()) for pat in pats for f in sorted(glob.glob(pat))]
        if not texts:
            continue
        acc = defaultdict(float)
        for t in texts:
            for k, x in vec(tokens(t), idf).items():
                acc[k] += x
        s = math.sqrt(sum(x * x for x in acc.values())) or 1
        v = {k: x / s for k, x in acc.items()}
        raws = [sum(x * v.get(t, 0.0) for t, x in vec(p["toks"], idf).items()) for p in posts]
        m, sd = statistics.fmean(raws), statistics.pstdev(raws) or 1
        zs = sorted((r - m) / sd * damp(len(p["toks"]), p.get("template", False)) for r, p in zip(raws, posts))
        out[name] = {"vec": v, "stats": (m, sd), "cut": percentile(zs, PCT_PARALLEL), "docs": len(texts)}
    return out

def parallel_scores(model, source: str, text_norm: str) -> dict:
    """{рубрика: z} по параллельным рубрикам модели; пустой текст или старая модель — {}."""
    if not text_norm or not model.get("parallel"):
        return {}
    toks = tokens(text_norm)
    pv = vec(toks, model["idf"])
    k = damp(len(toks), f"{source}\t{template_head(text_norm)}" in model["templates"])
    return {name: (sum(x * P["vec"].get(t, 0.0) for t, x in pv.items()) - P["stats"][0]) / P["stats"][1] * k
            for name, P in model["parallel"].items()}

def save(model, path=MODEL_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)

def load(path=MODEL_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def grade_post(model, source: str, text_norm: str):
    """Оценка одного нового поста по готовой модели: (градация, мастерская, z).
    Пустой текст — None: судить не по чему, решает вызывающая сторона."""
    if not text_norm:
        return None
    toks = tokens(text_norm)
    raw = raw_scores(toks, model["rooms"], model["idf"])
    template = f"{source}\t{template_head(text_norm)}" in model["templates"]
    k = damp(len(toks), template)
    z = {r: (raw[r] - m) / s * k for r, (m, s) in model["stats"].items()}
    room, best = max(z.items(), key=lambda kv: kv[1])
    return grade_of(best, text_norm, model["cut"]), room, round(best, 3)
