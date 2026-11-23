"""Monta channels.json só com canais que estão respondendo.

Para cada canal junta todos os endereços conhecidos (listas do iptv-org,
base completa de streams do iptv-org e a lista Free-TV), testa primeiro o
que funcionou da última vez e, se falhar, os alternativos até achar um que
abra. A memória fica em state.json, para canais que caíram voltarem sozinhos.

Roda todo dia pelo GitHub Actions (.github/workflows/channels.yml).
"""
import hashlib, io, json, os, re, sys, time, unicodedata, urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGOS = os.path.join(ROOT, "logos")
STATE = os.path.join(ROOT, "state.json")
BASE = "https://iptv-org.github.io/iptv/"
STREAMS_API = "https://iptv-org.github.io/api/streams.json"
FREETV = "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"
UA = ("Mozilla/5.0 (iPad; CPU OS 12_5_7 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/12.1.2 Mobile/15E148 Safari/604.1")
IN_CI = bool(os.environ.get("GITHUB_ACTIONS"))
WORKERS = int(os.environ.get("WORKERS", "40"))
MAX_TRIES = 6  # endereços testados por canal, no máximo

PT_SOURCES = ["languages/por.m3u", "countries/br.m3u", "countries/pt.m3u"]
CATEGORIES = [  # (id, nome, fonte, máximo de canais estrangeiros)
    ("series", "Séries", "categories/series.m3u", 60),
    ("news", "Notícias", "categories/news.m3u", 30),
    ("kids", "Infantil", "categories/kids.m3u", 30),
    ("movies", "Filmes", "categories/movies.m3u", 40),
    ("music", "Música", "categories/music.m3u", 30),
    ("religious", "Religiosos", "categories/religious.m3u", 20),
    ("sports", "Esportes", "categories/sports.m3u", 20),
    ("doc", "Documentários", "categories/documentary.m3u", 20),
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_get(url, limit=200_000, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.geturl(), r.read(limit)


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def clean_name(n):
    n = re.sub(r"\s*\(\d{3,4}p\)", "", n)
    n = re.sub(r"\s*\[(Geo-blocked|Not 24/7)\]", "", n)
    return n.replace(" Ⓨ", "").replace(" Ⓖ", "").strip()


def parse_m3u(text):
    out, cur, opts = [], None, False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            name = line.rsplit('",', 1)[-1] if '",' in line else line.split(",", 1)[-1]
            attr = lambda k: (re.search(k + r'="([^"]*)"', line) or [None, ""])[1]
            tid = attr("tvg-id")
            cur = {"name": name.strip(), "logo": attr("tvg-logo"), "group": attr("group-title"),
                   "id": tid.split("@")[0] if tid else ""}
            opts = False
        elif line.startswith("#EXTVLCOPT") or line.startswith("#KODIPROP"):
            opts = True  # exige referer/user-agent: o Safari não consegue enviar
        elif line and not line.startswith("#") and cur:
            if not opts:
                cur["url"] = line
                out.append(cur)
            cur = None
    return out


def key_of(entry):
    return entry["id"] or ("name:" + norm(entry["name"]))


def check_stream(url):
    """(ok, qualidade, motivo). ok=True se é HLS H.264 respondendo agora."""
    if "youtube.com" in url or "youtu.be" in url:
        return False, "", "youtube"
    try:
        status, final, body = http_get(url)
        text = body.decode("utf-8", "replace")
        if status != 200 or "#EXTM3U" not in text[:300]:
            return False, "", "morto"
        codecs = " ".join(re.findall(r'CODECS="([^"]+)"', text))
        if ("hvc1" in codecs or "hev1" in codecs) and "avc1" not in codecs:
            return False, "", "hevc"
        if "#EXT-X-STREAM-INF" in text:  # master: testa a primeira variante
            m = re.search(r"^(?!#)(\S+)", text, re.M)
            if m:
                s2, _, b2 = http_get(urljoin(final, m.group(1)), 50_000)
                if s2 != 200 or b"#EXTM3U" not in b2[:300]:
                    return False, "", "morto"
        heights = [int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", text) if int(h) <= 1080]
        return True, (f"{max(heights)}p" if heights else ""), ""
    except Exception:
        return False, "", "morto"


def logo_file(url):
    if not url or url.lower().split("?")[0].endswith(".svg"):
        return ""
    name = hashlib.sha1(url.encode()).hexdigest()[:14] + ".png"
    path = os.path.join(LOGOS, name)
    if os.path.exists(path):
        return "logos/" + name
    try:
        from PIL import Image
        _, _, data = http_get(url, 5_000_000, 15)
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        im.thumbnail((160, 100), Image.LANCZOS)
        im.save(path, "PNG", optimize=True)
        return "logos/" + name
    except Exception:
        return ""


def main():
    os.makedirs(LOGOS, exist_ok=True)
    t0 = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    state = {}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)

    def load(url):
        try:
            return http_get(url, 50_000_000, 90)[2].decode("utf-8", "replace")
        except Exception as e:
            log("falha ao baixar", url, e)
            return ""

    pt_lists = [parse_m3u(load(BASE + s)) for s in PT_SOURCES]
    cat_lists = [(cid, name, parse_m3u(load(BASE + src)), cap) for cid, name, src, cap in CATEGORIES]

    # ---- Canais que queremos (por chave) e onde aparecem ----
    info = {}           # chave -> {name, logo, group, pt}
    candidates = {}     # chave -> [urls] na ordem de preferência

    def add_candidate(k, url):
        lst = candidates.setdefault(k, [])
        if url not in lst:
            lst.append(url)

    pt_keys = set()
    for lst in pt_lists:
        for e in lst:
            pt_keys.add(key_of(e))
    for lst in pt_lists + [c[2] for c in cat_lists]:
        for e in lst:
            k = key_of(e)
            if k not in info:
                info[k] = {"name": clean_name(e["name"]), "logo": e["logo"], "group": e["group"].replace(";", " · "),
                           "geo": "[Geo-blocked]" in e["name"]}
            elif not info[k]["logo"] and e["logo"]:
                info[k]["logo"] = e["logo"]
            add_candidate(k, e["url"])

    # ---- Fontes alternativas ----
    by_name = {}
    for k in info:
        by_name.setdefault(norm(info[k]["name"]), k)
    extra = 0
    try:
        for s in json.loads(load(STREAMS_API) or "[]"):
            if s.get("referrer") or s.get("user_agent") or not s.get("channel"):
                continue
            if s["channel"] in info:
                n = len(candidates[s["channel"]]); add_candidate(s["channel"], s["url"]); extra += len(candidates[s["channel"]]) - n
    except Exception as e:
        log("streams.json ignorado:", e)
    for e in parse_m3u(load(FREETV)):
        k = e["id"] if e["id"] in info else by_name.get(norm(e["name"]))
        if k:
            n = len(candidates[k]); add_candidate(k, e["url"]); extra += len(candidates[k]) - n
            if not info[k]["logo"] and e["logo"]:
                info[k]["logo"] = e["logo"]
    log(f"{len(info)} canais, {sum(len(v) for v in candidates.values())} endereços ({extra} de fontes alternativas)")

    # ---- Testa: último endereço bom primeiro, depois https, depois http ----
    def try_channel(k):
        urls = candidates[k]
        last = state.get(k, {}).get("u")
        urls = sorted(urls, key=lambda u: (u != last, u.startswith("http:")))[:MAX_TRIES]
        reasons = []
        for u in urls:
            ok, q, why = check_stream(u)
            if ok:
                return k, u, q, len(reasons)
            reasons.append(why)
        if IN_CI and info[k]["geo"] and k in pt_keys and "hevc" not in reasons:
            # O GitHub testa de fora do Brasil; canais BR com bloqueio geográfico costumam abrir aqui.
            return k, (last or urls[0]), "", -1
        return k, None, "", len(reasons)

    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(try_channel, list(info)))

    alive, swapped, revived = {}, 0, 0
    for k, u, q, tries in results:
        prev = state.get(k, {})
        if u:
            if prev.get("u") and prev["u"] != u:
                swapped += 1
            if prev.get("down"):
                revived += 1
            alive[k] = (u, q)
            state[k] = {"u": u, "ok": today}
        else:
            if k in state:
                state[k]["down"] = state[k].get("down", 0) + 1
    # Esquece canais que estão fora do ar há mais de 30 verificações.
    state = {k: v for k, v in state.items() if v.get("down", 0) <= 30}
    log(f"{len(alive)} canais no ar · {swapped} trocaram de fonte · {revived} voltaram ({time.time() - t0:.0f}s)")

    # ---- Categorias ----
    def order(keys):
        return sorted(keys, key=lambda k: (k not in pt_keys, alive[k][0].startswith("http:"), info[k]["name"].lower()))

    def uniq(seq):
        seen, out = set(), []
        for x in seq:
            if x not in seen:
                seen.add(x); out.append(x)
        return out

    cats = [("pt", "Em português", order(uniq(k for k in (key_of(e) for l in pt_lists for e in l) if k in alive)))]
    for cid, name, lst, cap in cat_lists:
        keys = uniq(key_of(e) for e in lst if key_of(e) in alive)
        pt = [k for k in keys if k in pt_keys]
        other = sorted([k for k in keys if k not in pt_keys],
                       key=lambda k: (alive[k][0].startswith("http:"), -int((alive[k][1] or "0p")[:-1] or 0)))[:cap]
        cats.append((cid, name, order(pt + other)))

    used = uniq(k for _, _, keys in cats for k in keys)
    index = {k: i for i, k in enumerate(used)}
    with ThreadPoolExecutor(16) as ex:
        logos = list(ex.map(lambda k: logo_file(info[k]["logo"]), used))

    channels = []
    for k, logo in zip(used, logos):
        u, q = alive[k]
        item = {"n": info[k]["name"], "u": u, "g": info[k]["group"]}
        if logo: item["l"] = logo
        if k in pt_keys: item["pt"] = 1
        if q: item["q"] = q
        channels.append(item)

    out = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cats": [{"id": cid, "name": name, "items": [index[k] for k in keys]} for cid, name, keys in cats if keys],
        "ch": channels,
    }
    with open(os.path.join(ROOT, "channels.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    keep = {os.path.basename(l) for l in logos if l}
    try:
        keep |= build_radios()
    except Exception as e:  # rádios são extra: não derrubam a lista de canais
        log("rádios ignoradas:", e)
        keep |= {fn for fn in os.listdir(LOGOS)}
    for fn in os.listdir(LOGOS):
        if fn not in keep:
            os.remove(os.path.join(LOGOS, fn))

    for cid, name, keys in cats:
        log(f"  {name}: {len(keys)}")
    log(f"total {len(channels)} canais, {len(keep)} logos, {time.time() - t0:.0f}s")


# ------------------------------------------------------------------ Rádios
RADIO_API = ("https://de1.api.radio-browser.info/json/stations/search?countrycode=BR"
             "&hidebroken=true&order=clickcount&reverse=true&limit=400")
RADIO_CATS = [  # (id, nome, palavras nas tags)
    ("news", "Notícias", ("news", "talk", "journalism", "noticias", "jornalismo")),
    ("sports", "Esportes", ("sport", "futebol", "esporte")),
    ("gospel", "Gospel", ("gospel", "christian", "cristã", "crista", "religio", "catolica", "católica", "evangel")),
    ("sertanejo", "Sertanejo", ("sertanejo", "country", "modao", "modão")),
    ("mpb", "MPB", ("mpb", "brazilian music", "musica brasileira", "samba", "bossa")),
    ("pop", "Pop e Rock", ("pop", "rock", "hits", "chr", "dance")),
    ("flash", "Flashback", ("flashback", "80s", "90s", "70s", "oldies", "classic", "nostalgia")),
]


def check_radio(url, is_hls):
    try:
        status, final, body = http_get(url, 16_000, 8)
        if status != 200:
            return False
        if is_hls:
            return b"#EXTM3U" in body[:300]
        return len(body) > 2000
    except Exception:
        return False


def build_radios():
    t0 = time.time()
    req = urllib.request.Request(RADIO_API, headers={"User-Agent": "tv-ipad/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        stations = json.loads(r.read())
    seen, cands = set(), []
    for s in stations:
        codec = (s.get("codec") or "").upper()
        if codec not in ("MP3", "AAC", "AAC+") and not s.get("hls"):
            continue
        name = re.sub(r"\s+", " ", s["name"]).strip()
        k = norm(name)
        if not k or k in seen:
            continue
        seen.add(k)
        cands.append({"n": name, "u": s["url_resolved"], "hls": bool(s.get("hls")),
                      "logo": s.get("favicon") or "", "tags": (s.get("tags") or "").lower()})
    # https primeiro (http pode ser bloqueado numa página https)
    cands.sort(key=lambda c: c["u"].startswith("http:"))
    with ThreadPoolExecutor(WORKERS) as ex:
        ok = list(ex.map(lambda c: check_radio(c["u"], c["hls"]), cands))
    alive = [c for c, good in zip(cands, ok) if good][:200]
    with ThreadPoolExecutor(16) as ex:
        logos = list(ex.map(lambda c: logo_file(c["logo"]), alive))
    out = []
    for c, logo in zip(alive, logos):
        tags = c["tags"]
        item = {"n": c["n"], "u": c["u"], "c": [cid for cid, _, words in RADIO_CATS if any(w in tags for w in words)]}
        if logo:
            item["l"] = logo
        out.append(item)
    data = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cats": [{"id": cid, "name": name} for cid, name, _ in RADIO_CATS], "radios": out}
    with open(os.path.join(ROOT, "radios.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log(f"rádios: {len(out)} no ar de {len(cands)} ({time.time() - t0:.0f}s)")
    return {os.path.basename(l) for l in logos if l}


if __name__ == "__main__":
    if "--radios" in sys.argv:
        os.makedirs(LOGOS, exist_ok=True)
        build_radios()
    else:
        main()
