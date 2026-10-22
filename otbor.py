#!/usr/bin/env python3
"""otbor.py — отбор постов лент для мастерских: модель, сводки, отсев каналов.

Режимы:
  otbor.py --model                 пересобрать model.json из знаний мастерских (ежедневно)
  otbor.py --sutki                 сводка за сутки: что пришло каждой мастерской, идеи, отсеянное
  otbor.py                         полный отчёт: проверка на эталоне, верх отбора, польза каналов
  otbor.py --otsev [--primenit]    отсев каналов: 30 дней без интересного — отписка

Базы лент читаются снимком (sqlite backup) — ленты не блокируются. Пишется только своё:
model.json, otchety/, otbor.db (первое появление каналов, журнал отсева).
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, re, sqlite3, statistics, subprocess, sys
from collections import Counter, defaultdict
import otbor_model as M

HERE = os.path.dirname(os.path.abspath(__file__))
OTCHETY = os.path.join(HERE, "otchety")
OTBOR_DB = os.path.join(HERE, "otbor.db")
ISKLYUCHENIYA = os.path.join(HERE, "ne-otpisyvat.txt")
LENTY = {
    "svod": {"dir": "/root/svod_lent", "title": "Сводная лента"},
    "marketing": {"dir": "/root/marketing_lent", "title": "Маркетингми"},
}
SESSION_LOCK = "/root/razvedka/sekrety/tg.session.lock"  # с 22.10.2026 — папка разведки
OTSEV_DNEY = 30                 # окно отсева
OTSEV_MAX_DOLYA = 1 / 3         # предохранитель: больше трети каналов ленты за раз — не применять
INTERESNO = ("сейчас", "очередь")

now = dt.datetime.now(dt.timezone.utc)
iso = lambda d: d.isoformat(timespec="seconds")

# ── данные ────────────────────────────────────────────────────────────────────
def snapshot(path):
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    mem = sqlite3.connect(":memory:"); src.backup(mem); src.close()
    return mem

def tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def load_lenty():
    """{лента: {state, con, posts[], grades[]}}"""
    out = {}
    for key, cfg in LENTY.items():
        con = snapshot(os.path.join(cfg["dir"], "tg_collector_stats.db"))
        state = json.load(open(os.path.join(cfg["dir"], "tg_collector.json"), encoding="utf-8"))
        posts = [{"lenta": key, "id": mid, "source": s, "ts": ts or "", "bad": v == "bad", "deleted": bool(d),
                  "text": t or ""}
                 for mid, s, ts, v, d, t in con.execute(
                     "SELECT our_msg_id, source, posted_ts, COALESCE(verdict,''), deleted, text_norm FROM posts")]
        grades = []
        if "grades" in tables(con):
            has_text = "text_norm" in {r[1] for r in con.execute("PRAGMA table_info(grades)")}
            grades = [dict(zip(("ts", "source", "src_id", "our_id", "grade", "room", "z", "published", "note", "text"), r))
                      for r in con.execute("SELECT ts,source,src_msg_id,our_msg_id,grade,room,z,published,note,"
                                           f"{'text_norm' if has_text else 'NULL'} FROM grades")]
        out[key] = {"state": state, "con": con, "posts": posts, "grades": grades}
    return out

def link(lenta, state, our_id):
    t = state.get("target", {})
    return f"https://t.me/{t['username']}/{our_id}" if t.get("username") else f"https://t.me/c/{t.get('id')}/{our_id}"

def corpus(lenty, with_junk=False):
    """Посты с текстом без рекламы. with_junk — плюс отсеянный коллектором шлак: модель собирается
    по всему, что приходит в ленту, иначе порог шлака сползал бы вверх с каждой пересборкой."""
    out = [p for L in lenty.values() for p in L["posts"] if p["text"] and not M.is_ad(p["text"])]
    if with_junk:
        out += [{"lenta": k, "id": None, "source": g["source"], "ts": g["ts"] or "", "bad": False, "deleted": True,
                 "text": g["text"]} for k, L in lenty.items() for g in L["grades"]
                if g["grade"] == "шлак" and not g["published"] and g.get("text")]
    return out

def write(name, lines):
    os.makedirs(OTCHETY, exist_ok=True)
    path = os.path.join(OTCHETY, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path

# ── модель ────────────────────────────────────────────────────────────────────
def cmd_model(lenty):
    posts = corpus(lenty, with_junk=True)
    model, posts, *_ = M.build(posts)
    M.save(model)
    print(f"Модель: {M.MODEL_PATH}; постов {model['posts']}, документов {model['docs']}; "
          f"градации {dict(Counter(p['grade'] for p in posts))}")

def graded(lenty, model):
    """Оценить посты журнала готовой моделью (для истории, где grades ещё не велись)."""
    for p in corpus(lenty):
        p["grade"], p["room"], p["z"] = M.grade_post(model, p["source"], p["text"])

# ── сводка за сутки ───────────────────────────────────────────────────────────
def cmd_sutki(lenty):
    model = M.load()
    since = iso(now - dt.timedelta(hours=24))
    graded(lenty, model)
    L = [f"# Отбор за сутки — {now:%d.%m.%Y %H:%M} UTC\n",
         f"Окно: с {since[:16].replace('T', ' ')} UTC. Модель собрана {model['built'][:16].replace('T', ' ')} UTC "
         f"из {model['docs']} документов мастерских.\n", "| | " + " | ".join(c["title"] for c in LENTY.values()) + " |",
         "|---|" + "---|" * len(LENTY)]
    def row(label, f):
        L.append(f"| {label} | " + " | ".join(str(f(k)) for k in LENTY) + " |")
    new_pub = {k: [p for p in lenty[k]["posts"] if p["ts"] >= since and p.get("grade")] for k in LENTY}
    g24 = {k: [g for g in lenty[k]["grades"] if (g["ts"] or "") >= since] for k in LENTY}
    row("опубликовано постов", lambda k: sum(1 for p in lenty[k]["posts"] if p["ts"] >= since))
    row("отсеяно рекламы", lambda k: sum(1 for g in g24[k] if g["grade"] == "реклама"))
    row("отсеяно шлака", lambda k: sum(1 for g in g24[k] if g["grade"] == "шлак"))
    row("отсеяно дублей", lambda k: sum(1 for g in g24[k] if g["note"] == "дубль"))
    row("удалено рекламы из канала", lambda k: sum(1 for p in lenty[k]["posts"] if p["deleted"] and p["ts"] >= since))
    for gr in ("сейчас", "очередь", "идея"):
        row(f"«{gr}»", lambda k, gr=gr: sum(1 for p in new_pub[k] if p["grade"] == gr and not p["deleted"]))
    L.append("")
    items = sorted((p for k in LENTY for p in new_pub[k] if not p["deleted"]), key=lambda p: -p["z"])
    L.append("## По мастерским\n")
    any_room = False
    for room in M.KOMNATY:
        rows = [p for p in items if p["room"] == room and p["grade"] in INTERESNO][:10]
        if not rows:
            continue
        any_room = True
        L.append(f"### {room}\n")
        for p in rows:
            mark = "🔴 сейчас" if p["grade"] == "сейчас" else "очередь"
            L.append(f"- **{mark}** `{p['z']:.1f}` · {p['source']} — {p['text'][:160]}… [пост]({link(p['lenta'], lenty[p['lenta']]['state'], p['id'])})")
        L.append("")
    if not any_room:
        L.append("За сутки в «сейчас» и «очередь» ничего не попало.\n")
    # параллельные рубрики (otbor_model.PARALLEL): пост может стоять и выше, в своей мастерской
    for name, P in (model.get("parallel") or {}).items():
        rows = sorted(((M.parallel_scores(model, p["source"], p["text"]).get(name, 0.0), p) for p in items),
                      key=lambda r: -r[0])
        rows = [(z, p) for z, p in rows if z >= P["cut"]][:10]
        if not rows:
            continue
        L.append(f"### {name}\n")
        L.append(f"Параллельная рубрика: верхние {100 - M.PCT_PARALLEL}% по профилю «{name}», независимо от мастерской.\n")
        for z, p in rows:
            L.append(f"- **кандидат** `{z:.1f}` · {p['source']} — {p['text'][:160]}… [пост]({link(p['lenta'], lenty[p['lenta']]['state'], p['id'])})")
        L.append("")
    ideas = [p for p in items if p["grade"] == "идея"][:15]
    L.append("## Идеи на будущее\n")
    L.append("Новинки, которые не легли ни на одну мастерскую всерьёз. Отсюда — строки в «Хотим следить» или кандидаты в новую мастерскую.\n")
    L += [f"- `{p['z']:.1f}` · ближе всего к {p['room']} · {p['source']} — {p['text'][:160]}… "
          f"[пост]({link(p['lenta'], lenty[p['lenta']]['state'], p['id'])})" for p in ideas] or ["За сутки идей нет."]
    path = write(f"sutki-{now:%Y-%m-%d}.md", L)
    latest = os.path.join(OTCHETY, "sutki-posledniy.md")
    if os.path.islink(latest) or os.path.exists(latest):
        os.remove(latest)
    os.symlink(os.path.basename(path), latest)
    print(f"Сводка: {path}; интересного {sum(1 for p in items if p['grade'] in INTERESNO)}, идей {len(ideas)}")

# ── полный отчёт ──────────────────────────────────────────────────────────────
def wilson_low(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    return (p + z * z / (2 * n) - z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / (1 + z * z / n)

def cmd_full(lenty):
    posts = corpus(lenty)
    model, posts, docs, reestr, idf, rooms = M.build(posts)
    N = len(posts)
    order = sorted(posts, key=lambda p: -p["z"])
    # эталон: источники, взятые в разбор; профили — без документов, где источник упомянут
    ev = []
    for name, (room_exp, url) in eval_sources(reestr).items():
        rx = re.compile(r"\b" + re.escape(name) + r"\b")
        hits = [i for i, p in enumerate(posts) if rx.search(p["text"])]
        if not hits:
            continue
        mask = re.compile(r"\b(" + "|".join(re.escape(w) for w in name.split() if len(w) >= 5) + r")\b")
        sc, _ = M.score_corpus(posts, M.room_vectors(docs, idf, exclude_rx=mask), idf)
        pos = {i: k for k, (_, i) in enumerate(sorted(((max(s.values()), i) for i, s in enumerate(sc)), reverse=True))}
        for i in hits:
            ev.append((name, room_exp, posts[i], 1 - pos[i] / N, max(sc[i].items(), key=lambda kv: kv[1])[0]))
    L = [f"# Отбор постов для мастерских — {now:%d.%m.%Y}\n", "## Проверка на ваших решениях\n",
         "Эталон — посты об источниках, которые потом **взяты в разбор**. Профили для каждого строились без "
         "документов, где он упомянут. «Место» — доля постов ниже (100% — верх, 50% — как случайно).\n",
         "| Источник | В ленте | Канал | Место | Мастерская по реестру | Отбор отнёс к |", "|---|---|---|---|---|---|"]
    for name, re_, p, pct, best in sorted(ev, key=lambda r: -r[3]):
        L.append(f"| {name} | {p['ts'][:10]} | {p['source']} | {pct:.0%} | {re_} | {best}{' ✓' if best == re_ else ''} |")
    if ev:
        L.append(f"\n**Медиана места {statistics.median(r[3] for r in ev):.0%}; в верхних 10%: "
                 f"{sum(r[3] >= .9 for r in ev)} из {len(ev)}; мастерская угадана: {sum(r[4] == r[1] for r in ev)} из {len(ev)}.**\n")
    L.append(f"Градации корпуса: {dict(Counter(p['grade'] for p in posts))}. Пороги: "
             f"{ {k: round(v, 2) for k, v in model['cut'].items()} }.\n")
    L.append("## Польза каналов\n")
    L.append("Доля постов канала в «сейчас» и «очередь»; сортировка по нижней границе 95%-интервала Уилсона.\n")
    L.append("| Канал | Лента | Постов | «Сейчас»+«очередь» | Шлак | Польза | Не ниже (95%) |\n|---|---|---|---|---|---|---|")
    by = defaultdict(list)
    for p in posts:
        by[(p["source"], p["lenta"])].append(p)
    rows = []
    for (s, l), ps in by.items():
        n = len(ps); k = sum(p["grade"] in INTERESNO for p in ps); j = sum(p["grade"] == "шлак" for p in ps)
        rows.append((wilson_low(k, n), s, l, n, k, j))
    for low, s, l, n, k, j in sorted(rows, key=lambda r: (-r[0], -r[3])):
        L.append(f"| {s} | {l} | {n} | {k} | {j / n:.0%} | {k / n:.0%} | {low:.0%} |")
    path = write(f"otbor-{now:%Y-%m-%d}.md", L)
    print(f"Отчёт: {path}")
    if ev:
        print(f"Эталон: {len(ev)} постов, медиана {statistics.median(r[3] for r in ev):.0%}, "
              f"в верхних 10%: {sum(r[3] >= .9 for r in ev)}, мастерская угадана: {sum(r[4] == r[1] for r in ev)}")

def eval_sources(reestr):
    out = {}
    for line in reestr.split("## Источники")[1].split("\n## ")[0].splitlines():
        c = [x.strip() for x in line.strip("|").split("|")]
        if len(c) < 3 or not c[1].startswith("http"):
            continue
        m = re.match(r"https?://github\.com/[^/]+/([^/#?]+)", c[1])
        name = m.group(1) if m else re.sub(r"^www\.", "", re.match(r"https?://([^/]+)", c[1]).group(1)).split(".")[-2]
        name = M.norm(name.replace("-", " ").replace("_", " "))
        if len(name) >= 5 and name not in {"skills", "github", "design", "browser"}:
            out[name] = (M.REESTR_KOMNATA.get(c[0], c[0]), c[1])
    return out

# ── отсев каналов ─────────────────────────────────────────────────────────────
def otbor_db():
    con = sqlite3.connect(OTBOR_DB)
    con.execute("CREATE TABLE IF NOT EXISTS kanaly (lenta TEXT, source TEXT, first_seen TEXT, PRIMARY KEY(lenta, source))")
    con.execute("CREATE TABLE IF NOT EXISTS otsev (ts TEXT, lenta TEXT, source TEXT, action TEXT, reason TEXT, stats TEXT)")
    return con

def isklyucheniya():
    if not os.path.exists(ISKLYUCHENIYA):
        return set()
    return {l.strip().lstrip("@").lower() for l in open(ISKLYUCHENIYA, encoding="utf-8")
            if l.strip() and not l.lstrip().startswith("#")}

def zaslug(lenty):
    """{(лента, канал): [источники]} — каналы, чьи посты привели к разбору (реестр).
    Такой канал не отписывается: его ценность подтверждена решением человека, а не моделью."""
    _, reestr = M.load_docs()
    out = defaultdict(set)
    for name in eval_sources(reestr):
        rx = re.compile(r"\b" + re.escape(name) + r"\b")
        for key, Ld in lenty.items():
            for p in Ld["posts"]:
                if p["text"] and rx.search(p["text"]):
                    out[(key, p["source"])].add(name)
    return out

def cmd_otsev(lenty, primenit: bool):
    model = M.load()
    graded(lenty, model)
    db = otbor_db()
    since = iso(now - dt.timedelta(days=OTSEV_DNEY))
    skip = isklyucheniya()
    zasl = zaslug(lenty)
    L = [f"# Отсев каналов — {now:%d.%m.%Y}\n",
         f"Правило: за {OTSEV_DNEY} дней ни одного поста в «сейчас» или «очередь» — отписка. Канал в наблюдении "
         f"меньше {OTSEV_DNEY} дней не трогается. Не отписываются: каналы из `ne-otpisyvat.txt` и каналы, чей пост "
         f"привёл к разбору (реестр источников). Предохранитель: больше "
         f"{OTSEV_MAX_DOLYA:.0%} каналов ленты за раз — не применять, нужна проверка человеком.\n",
         f"Режим: {'**применение**' if primenit else 'только список, без отписки'}.\n"]
    for key, cfg in LENTY.items():
        Ld = lenty[key]; st = Ld["state"]
        active = [s for s in st.get("sources", []) if s not in set(st.get("disabled", []))]
        for s in active:
            db.execute("INSERT OR IGNORE INTO kanaly VALUES (?,?,?)", (key, s, iso(now)))
        db.commit()
        first_seen = dict(db.execute("SELECT source, first_seen FROM kanaly WHERE lenta=?", (key,)))
        grades_start = min((g["ts"] for g in Ld["grades"] if g["ts"]), default=None)
        stats, cands = [], []
        for s in active:
            hist = [p for p in Ld["posts"] if p["source"] == s]
            obs = min([p["ts"] for p in hist if p["ts"]] + [first_seen.get(s, iso(now))])
            # история до журнала оценок — по постам; после — по журналу (там и отсеянное)
            win_posts = [p for p in hist if p["ts"] >= since and (not grades_start or p["ts"] < grades_start)]
            win_grades = [g for g in Ld["grades"] if g["source"] == s and (g["ts"] or "") >= since]
            seen = len(win_posts) + len(win_grades)
            inter = sum(p.get("grade") in INTERESNO for p in win_posts) + sum(g["grade"] in INTERESNO for g in win_grades)
            days = (now - dt.datetime.fromisoformat(obs)).days
            row = {"source": s, "seen": seen, "inter": inter, "days": days,
                   "junk": sum(p.get("grade") == "шлак" for p in win_posts) + sum(g["grade"] == "шлак" for g in win_grades),
                   "ads": sum(M.is_ad(p["text"]) for p in win_posts) + sum(g["grade"] == "реклама" for g in win_grades)}
            stats.append(row)
            if (key, s) in zasl:
                row["zaslugi"] = ", ".join(sorted(zasl[(key, s)]))
            if days >= OTSEV_DNEY and inter == 0 and s.lower() not in skip and "zaslugi" not in row:
                row["reason"] = (f"{OTSEV_DNEY} дней: {seen} постов, интересных 0" if seen else f"{OTSEV_DNEY} дней без постов")
                cands.append(row)
        L.append(f"## {cfg['title']} — активных каналов {len(active)}, кандидатов в отписку {len(cands)}\n")
        saved = [r for r in stats if r.get("zaslugi") and r["days"] >= OTSEV_DNEY and r["inter"] == 0]
        if saved:
            L.append("Защищены заслугами (без интересного за 30 дней, но их пост привёл к разбору): " +
                     "; ".join(f"**{r['source']}** — {r['zaslugi']}" for r in saved) + ".\n")
        guard = bool(active) and len(cands) > len(active) * OTSEV_MAX_DOLYA
        if cands:
            L.append("| Канал | Постов за 30 дней | Интересных | Шлак | Реклама | В наблюдении, дней | Причина |\n|---|---|---|---|---|---|---|")
            L += [f"| {c['source']} | {c['seen']} | 0 | {c['junk']} | {c['ads']} | {c['days']} | {c['reason']} |" for c in cands]
            L.append("")
        if guard:
            L.append(f"⚠️ **Предохранитель:** кандидатов {len(cands)} из {len(active)} — больше {OTSEV_MAX_DOLYA:.0%}. "
                     f"Не применяю: вероятна ошибка модели, нужна проверка человеком.\n")
        if primenit and cands and not guard:
            keys = [c["source"] for c in cands]
            cmd = ["flock", "-w", "300", SESSION_LOCK, "timeout", "-s", "INT", "-k", "30", "600",
                   os.path.join(cfg["dir"], ".venv/bin/python"), "tg_collector.py", "unsubscribe", *keys,
                   "--reason", f"отсев: {OTSEV_DNEY} дней без интересного"]
            r = subprocess.run(cmd, cwd=cfg["dir"], capture_output=True, text=True)
            action = "отписка" if r.returncode == 0 else f"сбой отписки (код {r.returncode})"
            for c in cands:
                db.execute("INSERT INTO otsev VALUES (?,?,?,?,?,?)", (iso(now), key, c["source"], action, c["reason"],
                                                                    json.dumps(c, ensure_ascii=False)))
            db.commit()
            L.append(f"Применено: {action} — {', '.join(keys)}.\n")
        elif guard:
            for c in cands:
                db.execute("INSERT INTO otsev VALUES (?,?,?,?,?,?)", (iso(now), key, c["source"], "предохранитель",
                                                                    c["reason"], json.dumps(c, ensure_ascii=False)))
            db.commit()
        L.append("<details><summary>Все активные каналы</summary>\n")
        L.append("| Канал | Постов за 30 дней | Интересных | Шлак | Реклама | В наблюдении, дней |\n|---|---|---|---|---|---|")
        L += [f"| {r['source']} | {r['seen']} | {r['inter']} | {r['junk']} | {r['ads']} | {r['days']} |"
              for r in sorted(stats, key=lambda r: (r["inter"], -r["seen"]))]
        L.append("\n</details>\n")
    path = write(f"otsev-{now:%Y-%m-%d}.md", L)
    print(f"Отсев: {path}")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", action="store_true")
    ap.add_argument("--sutki", action="store_true")
    ap.add_argument("--otsev", action="store_true")
    ap.add_argument("--primenit", action="store_true", help="с --otsev: действительно отписаться")
    a = ap.parse_args()
    lenty = load_lenty()
    if a.model:
        cmd_model(lenty)
    elif a.sutki:
        cmd_sutki(lenty)
    elif a.otsev:
        cmd_otsev(lenty, a.primenit)
    else:
        cmd_full(lenty)

if __name__ == "__main__":
    main()
