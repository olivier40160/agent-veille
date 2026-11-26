import json
import os
import sys
import re
import time
import random
import base64
import hashlib
import argparse
from datetime import datetime, timedelta
from urllib.parse import urlparse, quote_plus
import requests
import feedparser
from bs4 import BeautifulSoup
from ddgs import DDGS
from google import genai
from google.genai import types

# 1. Arguments CLI
parser = argparse.ArgumentParser(description="Agent de veille technologique et stratégique modulaire.")
parser.add_argument("-t", "--temperature", type=float, default=0.4, help="Température Gemini (défaut: 0.4)")
parser.add_argument("--use-memory", action="store_true", help="Activer la mémoire persistante Google Drive (désactivée par défaut)")
parser.add_argument("-n", "--no-memory", action="store_true", help="Désactiver la mémoire Drive (comportement par défaut)")
parser.add_argument("-r", "--retry-delay", type=int, default=60, help="Délai de pause avant retry global sujet (défaut: 60s)")
parser.add_argument("-m", "--max-retries", type=int, default=2, help="Nombre max de tentatives globales par sujet (défaut: 2)")
parser.add_argument("--web-retries", type=int, default=2, help="Nombre max de tentatives locales par requête Web (défaut: 2)")
parser.add_argument("--web-retry-delay", type=int, default=5, help="Délai de base (en secondes) avant retry local Web (défaut: 5s)")
parser.add_argument("--ttl-days", type=int, default=30, help="Durée de rétention en jours des items en mémoire (défaut: 30)")
parser.add_argument("-s", "--sujet", type=str, default=None, help="Traiter uniquement un sujet spécifique (id ou libellé)")
parser.add_argument("-c", "--config", type=str, default="sujets_veille.json", help="Chemin du fichier JSON des sujets")
args = parser.parse_args()

USE_MEMORY = bool(args.use_memory and not args.no_memory)

# 2. API & Env
api_key = os.environ.get("GEMINI_API_KEY")
github_token = os.environ.get("GITHUB_TOKEN")
webhook_url = os.environ.get("WEBHOOK_URL")
webhook_secret = os.environ.get("WEBHOOK_SECRET")

if not api_key or not webhook_url or not webhook_secret:
    print("❌ Erreur critique : GEMINI_API_KEY, WEBHOOK_URL et WEBHOOK_SECRET sont obligatoires.", file=sys.stderr)
    sys.exit(1)

client = genai.Client(api_key=api_key)
CONFIG_FILE = args.config
PROMPTS_DIR = "prompts"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MEMOIRE_CACHE = {"items_vus": {}, "derniere_mise_a_jour": None}
LAST_GNEWS_CALL = 0.0  # Horodatage pour espacer les requêtes news.google.com

# Session HTTP persistante (sans 'br' dans Accept-Encoding pour éviter les flux Brotli non décompressés)
HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive"
})

STATS = {
    "rss_nouveaux": 0,
    "rss_ignores_memoire": 0,
    "rss_fallbacks_gnews": 0,
    "rss_fallbacks_web": 0,
    "rss_erreurs": 0,
    "web_nouveaux": 0,
    "web_ignores_memoire": 0,
    "web_no_results": 0,
    "webpage_nouveaux": 0,
    "webpage_ignores_memoire": 0,
    "webpage_erreurs": 0,
    "github_nouveaux": 0,
    "github_ignores_memoire": 0,
    "memoire_purges_ttl": 0
}


# 3. Gestion Mémoire Drive (optionnelle via --use-memory)
def nettoyer_et_normaliser_memoire(raw_data: dict) -> dict:
    now = datetime.now()
    now_str = now.strftime(DATE_FORMAT)
    limite_date = now - timedelta(days=args.ttl_days)

    raw_items = raw_data.get("items_vus", {})
    items_valides = {}
    purges = 0

    if isinstance(raw_items, list):
        for item_id in raw_items:
            items_valides[item_id] = now_str
    elif isinstance(raw_items, dict):
        for item_id, date_str in raw_items.items():
            try:
                item_dt = datetime.strptime(date_str, DATE_FORMAT)
                if item_dt >= limite_date:
                    items_valides[item_id] = date_str
                else:
                    purges += 1
            except ValueError:
                items_valides[item_id] = now_str

    STATS["memoire_purges_ttl"] += purges
    return {
        "items_vus": items_valides,
        "derniere_mise_a_jour": raw_data.get("derniere_mise_a_jour")
    }


def charger_memoire_drive() -> dict:
    global MEMOIRE_CACHE
    if not USE_MEMORY:
        print("  └─ ℹ️ Mode sans mémoire actif (défaut) : collecte fraîche complète.")
        MEMOIRE_CACHE = {"items_vus": {}, "derniere_mise_a_jour": None}
        return MEMOIRE_CACHE

    print(f"  └─ ⚙️ [Mémoire Drive] Chargement de la mémoire (TTL = {args.ttl_days} jours)...")
    payload = {"secret": webhook_secret, "action": "get_memory"}
    try:
        res = requests.post(webhook_url, json=payload, timeout=(15, 60))
        if res.status_code == 200 and res.json().get("status") == "success":
            raw_mem = res.json().get("data", {"items_vus": {}, "derniere_mise_a_jour": None})
            MEMOIRE_CACHE = nettoyer_et_normaliser_memoire(raw_mem)
            print(f"  └─ ✅ Mémoire active chargée ({len(MEMOIRE_CACHE['items_vus'])} éléments valides).")
            return MEMOIRE_CACHE
    except Exception as e:
        print(f"❌ Erreur critique chargement mémoire Drive : {e}", file=sys.stderr)
        sys.exit(1)
    return MEMOIRE_CACHE


def sauvegarder_memoire_drive():
    global MEMOIRE_CACHE
    if not USE_MEMORY:
        return

    MEMOIRE_CACHE = nettoyer_et_normaliser_memoire(MEMOIRE_CACHE)
    MEMOIRE_CACHE["derniere_mise_a_jour"] = datetime.now().strftime(DATE_FORMAT)
    payload = {
        "secret": webhook_secret,
        "action": "save_memory",
        "memory_data": MEMOIRE_CACHE
    }
    try:
        res = requests.post(webhook_url, json=payload, timeout=(15, 60))
        if res.status_code == 200:
            print("  └─ ✅ [Mémoire Drive] Sauvegarde réussie sur Google Drive.")
    except Exception as e:
        print(f"⚠️ Erreur sauvegarde mémoire Drive : {e}", file=sys.stderr)


def enregistrer_nouveaux_items(nouveaux_ids: list):
    if not USE_MEMORY:
        return
    global MEMOIRE_CACHE
    now_str = datetime.now().strftime(DATE_FORMAT)
    for item_id in nouveaux_ids:
        MEMOIRE_CACHE["items_vus"][item_id] = now_str


# 4. Outillage de Collecte
def decoder_url_gnews(url: str) -> str:
    """Extrait l'URL éditeur réelle depuis un lien RSS news.google.com lorsque encodée en clair."""
    if "news.google.com/rss/articles/" not in url:
        return url
    try:
        encoded = url.split("/articles/")[1].split("?")[0]
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded)
        match = re.search(rb'https?://[^\x00-\x1f\x7f"<>\\^`{|}\s]+', decoded_bytes)
        if match:
            real_url = match.group(0).decode("utf-8", errors="ignore")
            second_http = real_url.find("http", 8)
            if second_http != -1:
                real_url = real_url[:second_http]
            return real_url
    except Exception:
        pass
    return url.split("?oc=")[0]


def respecter_cadence_gnews():
    """Garantit un délai minimal de 12 à 17.5s entre deux requêtes vers news.google.com pour éviter HTTP 429."""
    global LAST_GNEWS_CALL
    now = time.time()
    ecoule = now - LAST_GNEWS_CALL
    min_interval = random.uniform(12.0, 17.5)
    if LAST_GNEWS_CALL > 0 and ecoule < min_interval:
        attente = min_interval - ecoule
        print(f"     │  ⏳ Cadence Google News : pause de {attente:.1f}s...")
        time.sleep(attente)
    LAST_GNEWS_CALL = time.time()


def amorcer_cookies_domaine(parsed_url):
    """Visite la page d'accueil du domaine avec des en-têtes navigateur pour initialiser les cookies WAF/session."""
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}/"
    nav_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1"
    }
    try:
        HTTP_SESSION.get(base_url, headers=nav_headers, timeout=12)
    except Exception:
        pass


def collecter_web(query: str, limit: int = 3, max_web_retries: int = None, web_retry_delay: int = None) -> str:
    retries = max_web_retries if max_web_retries is not None else args.web_retries
    base_delay = web_retry_delay if web_retry_delay is not None else args.web_retry_delay

    print(f"     ├─ 🔍 Web : '{query}'...")
    exclus = MEMOIRE_CACHE.get("items_vus", {}) if USE_MEMORY else {}

    for attempt in range(1, retries + 1):
        if attempt == 1:
            time.sleep(random.uniform(4.5, 8.0))
        else:
            pause = base_delay + random.uniform(2.0, 7.5)
            print(f"     │  ⏳ Pause de {pause:.1f}s avant tentative Web ({attempt}/{retries})...")
            time.sleep(pause)

        try:
            results = []
            nouveaux = []
            ignores_local = 0

            with DDGS() as ddgs:
                search_results = list(ddgs.text(query, max_results=max(5, limit * 2)))
                if not search_results:
                    raise ValueError("No results found (liste vide)")

                for r in search_results:
                    link = r.get("href")
                    if not link:
                        continue
                    if link not in exclus:
                        results.append(f"• Titre: {r.get('title')} | Extrait: {r.get('body')} | Lien: {link}")
                        nouveaux.append(link)
                        if len(results) >= limit:
                            break
                    else:
                        ignores_local += 1

            STATS["web_nouveaux"] += len(nouveaux)
            STATS["web_ignores_memoire"] += ignores_local
            detail_mem = f", {ignores_local} ignoré(s) (mémoire)" if USE_MEMORY else ""
            print(f"     │  ℹ️ Web : {len(nouveaux)} retenu(s){detail_mem}.")

            enregistrer_nouveaux_items(nouveaux)
            return "\n".join(results)

        except Exception as e:
            if attempt < retries:
                print(f"     │  ⚠️ Échec tentative Web ({attempt}/{retries}) : {e}")
            else:
                STATS["web_no_results"] += 1
                print(f"     │  ❌ Web ignoré après {retries} tentatives : {e}")
                return ""
    return ""


def collecter_rss(url: str, limit: int = 3, keywords: list = None, _is_fallback: bool = False) -> str:
    prefixe = "📡 RSS (Fallback GNews)" if _is_fallback else "📡 RSS"
    kw_label = f" [mots-clés: {', '.join(keywords)}]" if keywords else ""
    print(f"     ├─ {prefixe}{kw_label} : {url}...")
    exclus = MEMOIRE_CACHE.get("items_vus", {}) if USE_MEMORY else {}

    parsed = urlparse(url)
    base_domain_url = f"{parsed.scheme}://{parsed.netloc}/"
    rss_headers = {
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        "Referer": base_domain_url
    }
    is_gnews = "news.google.com" in url

    try:
        if is_gnews:
            respecter_cadence_gnews()
        else:
            time.sleep(random.uniform(2.5, 6.0))

        res = HTTP_SESSION.get(url, headers=rss_headers, timeout=14)

        # Retry 1 fois en cas de rate-limit (429) ou blocage ponctuel (403/503) avec amorçage cookies
        if res.status_code == 429:
            pause_429 = random.uniform(22.0, 30.0)
            print(f"     │  ⏳ HTTP 429 détecté -> Pause de {pause_429:.1f}s avant nouvelle tentative...")
            time.sleep(pause_429)
            if is_gnews:
                respecter_cadence_gnews()
            res = HTTP_SESSION.get(url, headers=rss_headers, timeout=14)

        elif res.status_code in (403, 503) and not is_gnews and not _is_fallback:
            pause_waf = random.uniform(5.0, 8.0)
            print(f"     │  ⏳ HTTP {res.status_code} -> Amorçage cookies sur {base_domain_url} + retry après {pause_waf:.1f}s...")
            amorcer_cookies_domaine(parsed)
            time.sleep(pause_waf)
            res = HTTP_SESSION.get(url, headers=rss_headers, timeout=14)

        if res.status_code in (403, 404, 503):
            STATS["rss_erreurs"] += 1
            domaine = parsed.netloc.replace("www.", "")

            if not _is_fallback and not is_gnews:
                STATS["rss_fallbacks_gnews"] += 1
                gnews_query = quote_plus(f"site:{domaine} when:14d")
                gnews_url = f"https://news.google.com/rss/search?q={gnews_query}&hl=en-US&gl=US&ceid=US:en"
                print(f"     │  ⚠️ RSS bloqué/absent (HTTP {res.status_code}) -> Fallback RSS Google News 'site:{domaine}'...")
                return collecter_rss(gnews_url, limit=limit, keywords=keywords, _is_fallback=True)

            STATS["rss_fallbacks_web"] += 1
            print(f"     │  ⚠️ Fallback GNews bloqué (HTTP {res.status_code}) -> Fallback Web 'site:{domaine}'...")
            return collecter_web(f"site:{domaine}", limit=limit)

        if res.status_code != 200:
            STATS["rss_erreurs"] += 1
            print(f"     │  ⚠️ RSS ignoré (HTTP {res.status_code})")
            return ""

        feed = feedparser.parse(res.content)

        # Si HTTP 200 mais 0 entrée (challenge anti-bot HTML ou flux vide/illisible) -> Fallback GNews
        if not feed.entries:
            STATS["rss_erreurs"] += 1
            domaine = parsed.netloc.replace("www.", "")
            if not _is_fallback and not is_gnews:
                STATS["rss_fallbacks_gnews"] += 1
                gnews_query = quote_plus(f"site:{domaine} when:14d")
                gnews_url = f"https://news.google.com/rss/search?q={gnews_query}&hl=en-US&gl=US&ceid=US:en"
                print(f"     │  ⚠️ RSS vide ou HTML (0 entrée) -> Fallback RSS Google News 'site:{domaine}'...")
                return collecter_rss(gnews_url, limit=limit, keywords=keywords, _is_fallback=True)
            print("     │  ℹ️ RSS : 0 entrée exploitable.")
            return ""

        # Tri chronologique strict du plus récent au plus ancien
        entries_triees = sorted(
            feed.entries,
            key=lambda e: e.get("published_parsed") or e.get("updated_parsed") or (1970, 1, 1, 0, 0, 0, 0, 0, 0),
            reverse=True
        )

        # Si des mots-clés sont fournis : on place d'abord les entrées correspondantes (toujours triées par date),
        # puis les autres entrées récentes en complément.
        if keywords:
            kw_lower = [k.lower() for k in keywords if k]
            prioritaires = []
            secondaires = []
            for entry in entries_triees:
                texte_cherche = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
                if any(kw in texte_cherche for kw in kw_lower):
                    prioritaires.append(entry)
                else:
                    secondaires.append(entry)
            entries_ordonnees = prioritaires + secondaires
        else:
            entries_ordonnees = entries_triees

        results = []
        nouveaux = []
        ignores_local = 0

        for entry in entries_ordonnees:
            raw_link = entry.get("link", entry.get("title"))
            link = decoder_url_gnews(raw_link) if is_gnews else raw_link

            if link not in exclus and raw_link not in exclus:
                title = entry.get("title", "Sans titre")
                if is_gnews and " - " in title:
                    title = title.rsplit(" - ", 1)[0].strip()

                date_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                if date_struct:
                    date_pub = time.strftime("%Y-%m-%d", date_struct)
                else:
                    date_pub = entry.get("published", entry.get("updated", "Date inconnue"))

                results.append(f"• Date: {date_pub} | Titre: {title} | Lien: {link}")
                nouveaux.append(link)
                if len(results) >= limit:
                    break
            else:
                ignores_local += 1

        STATS["rss_nouveaux"] += len(nouveaux)
        STATS["rss_ignores_memoire"] += ignores_local
        detail_mem = f", {ignores_local} ignoré(s) (mémoire)" if USE_MEMORY else ""
        print(f"     │  ℹ️ RSS (trié par date) : {len(nouveaux)} retenu(s){detail_mem}.")

        enregistrer_nouveaux_items(nouveaux)
        return "\n".join(results)
    except Exception as e:
        STATS["rss_erreurs"] += 1
        print(f"     │  ⚠️ RSS ignoré : {e}")
        return ""


def collecter_webpage(url: str, max_chars: int = 4000, selector: str = "") -> str:
    sel_label = f" [selector: '{selector}']" if selector else ""
    print(f"     ├─ 🌐 Page Web{sel_label} : {url}...")
    exclus = MEMOIRE_CACHE.get("items_vus", {}) if USE_MEMORY else {}
    page_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        time.sleep(random.uniform(4.5, 6.5))
        res = HTTP_SESSION.get(url, headers=page_headers, timeout=15)
        if res.status_code != 200:
            STATS["webpage_erreurs"] += 1
            print(f"     │  ⚠️ Page ignorée (HTTP {res.status_code})")
            return ""

        soup = BeautifulSoup(res.content, "html.parser")
        titre = soup.title.string.strip() if soup.title and soup.title.string else url

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()

        if selector:
            cibles = soup.select(selector)
            if not cibles:
                cibles = [soup]
        else:
            cibles = [soup]

        lignes = []
        for bloc in cibles:
            lignes.extend([line.strip() for line in bloc.get_text(separator="\n").splitlines() if line.strip()])
        texte_propre = "\n".join(lignes)

        content_hash = hashlib.sha256(texte_propre.encode("utf-8")).hexdigest()[:12]
        mem_key = f"{url}#{selector}#{content_hash}" if selector else f"{url}#{content_hash}"

        if USE_MEMORY and mem_key in exclus:
            STATS["webpage_ignores_memoire"] += 1
            print("     │  ℹ️ Page Web : contenu inchangé (ignorée).")
            return ""

        extrait = texte_propre[:max_chars]
        STATS["webpage_nouveaux"] += 1
        print(f"     │  ℹ️ Page Web : {len(extrait)} car. extraits.")

        enregistrer_nouveaux_items([mem_key])
        return f"• Source directe: {titre} ({url})\nExtrait du contenu :\n{extrait}"

    except Exception as e:
        STATS["webpage_erreurs"] += 1
        print(f"     │  ⚠️ Page Web ignorée : {e}")
        return ""


def collecter_github(query: str, language: str = "", limit: int = 3) -> str:
    lang_label = f" [{language}]" if language else ""
    print(f"     ├─ 🐙 GitHub{lang_label} : '{query}'...")
    exclus = MEMOIRE_CACHE.get("items_vus", {}) if USE_MEMORY else {}
    q = f"{query}+created:>2026-01-01"
    if language:
        q += f"+language:{language}"
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc"
    headers = {"User-Agent": "Python-Agent-Veille"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            return ""
        items = res.json().get("items", [])
        results = []
        nouveaux = []
        ignores_local = 0

        for repo in items:
            full_name = repo['full_name']
            if full_name not in exclus:
                date_creation = (repo.get("created_at") or "Inconnue")[:10]
                date_maj = (repo.get("pushed_at") or repo.get("updated_at") or "Inconnue")[:10]

                results.append(
                    f"• Créé le: {date_creation} | MàJ: {date_maj} | Repo: {full_name} | "
                    f"Desc: {repo['description']} | Lien: {repo['html_url']}"
                )
                nouveaux.append(full_name)
                if len(results) >= limit:
                    break
            else:
                ignores_local += 1

        STATS["github_nouveaux"] += len(nouveaux)
        STATS["github_ignores_memoire"] += ignores_local
        detail_mem = f", {ignores_local} ignoré(s) (mémoire)" if USE_MEMORY else ""
        print(f"     │  ℹ️ GitHub : {len(nouveaux)} retenu(s){detail_mem}.")

        enregistrer_nouveaux_items(nouveaux)
        return "\n".join(results)
    except Exception as e:
        print(f"     │  ⚠️ GitHub ignoré : {e}")
        return ""


def exécuter_collecte_sujet(sujet_obj: dict) -> list:
    donnees = []
    for src in sujet_obj.get("sources", []):
        stype = src.get("type")
        limit = src.get("limit", 3)
        if stype == "rss":
            res = collecter_rss(
                src.get("url"),
                limit=limit,
                keywords=src.get("keywords")
            )
        elif stype == "search":
            res = collecter_web(src.get("query"), limit=limit)
        elif stype == "webpage":
            res = collecter_webpage(
                src.get("url"),
                src.get("max_chars", 4000),
                src.get("selector", "")
            )
        elif stype == "github":
            res = collecter_github(src.get("query"), src.get("language", ""), limit=limit)
        else:
            res = ""
        if res:
            donnees.append(res)
    return donnees


# 5. Prompts
def obtenir_prompt_sujet(sujet_obj: dict) -> str:
    sujet_id = sujet_obj.get("id")
    if not sujet_id:
        sujet_id = re.sub(r'[^a-z0-9_]', '', sujet_obj.get("sujet", "").lower().replace(" ", "_"))

    prompt_file = os.path.join(PROMPTS_DIR, f"{sujet_id}.txt")
    default_file = os.path.join(PROMPTS_DIR, "default.txt")

    if os.path.exists(prompt_file):
        with open(prompt_file, "r", encoding="utf-8") as f:
            return f.read()
    elif os.path.exists(default_file):
        with open(default_file, "r", encoding="utf-8") as f:
            return f.read()
    else:
        return """Rédige une synthèse sur le sujet '{sujet}' basée sur ces données :
{donnees}
Instructions : Pas de titre H2. 1-2 phrases de contexte général, développements récents (<1 mois) avec enjeux et enseignements. Explications en texte brut, terminer obligatoirement par la liste des liens [Titre](URL)."""


# 6. Webhooks (Global et Mise à jour unitaire)
def publier_rapport(content: str):
    print("\n  └─ ⚙️ [Outil Webhook] Publication globale sur Google Docs...")
    payload = {"secret": webhook_secret, "content": content}
    res = requests.post(webhook_url, json=payload, timeout=(15, 120))
    if res.status_code == 200 and res.json().get("status") == "success":
        print("  └─ ✅ Publication globale réussie.")
        sauvegarder_memoire_drive()
    else:
        print(f"❌ Échec de la publication Webhook : {res.text}", file=sys.stderr)
        sys.exit(1)


def publier_sujet_unique(sujet_title: str, section_content: str):
    print(f"\n  └─ ⚙️ [Outil Webhook] Mise à jour ciblée du sujet '{sujet_title}' dans Google Docs...")
    payload = {
        "secret": webhook_secret,
        "action": "update_single_subject",
        "sujet_title": sujet_title,
        "content": section_content
    }
    res = requests.post(webhook_url, json=payload, timeout=(15, 120))
    if res.status_code == 200 and res.json().get("status") == "success":
        msg = res.json().get("message", "Sujet mis à jour")
        print(f"  └─ ✅ {msg}")
        sauvegarder_memoire_drive()
    else:
        print(f"❌ Échec de la mise à jour ciblée Webhook : {res.text}", file=sys.stderr)
        sys.exit(1)


# 7. Pipeline principal
if __name__ == "__main__":
    mode_label = f"Sujet unique ('{args.sujet}')" if args.sujet else "Rapport complet"
    mem_label = f"Activée (TTL {args.ttl_days}j)" if USE_MEMORY else "Désactivée (défaut)"
    print(
        f"⚙️ Paramètres : Mode={mode_label} | Temp={args.temperature} | Mémoire={mem_label} | "
        f"Sujet Retry={args.max_retries}x ({args.retry_delay}s) | Web Retry={args.web_retries}x ({args.web_retry_delay}s)"
    )

    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Fichier {CONFIG_FILE} introuvable.", file=sys.stderr)
        sys.exit(1)

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        sujets_list = json.load(f)

    if args.sujet:
        cible = args.sujet.strip().lower()
        sujets_filtres = [
            s for s in sujets_list
            if (s.get("id", "").lower() == cible) or (s.get("sujet", "").strip().lower() == cible)
        ]
        if not sujets_filtres:
            ids_dispos = [s.get("id", s.get("sujet")) for s in sujets_list]
            print(f"❌ Erreur : Sujet '{args.sujet}' introuvable dans {CONFIG_FILE}.", file=sys.stderr)
            print(f"   IDs disponibles : {', '.join(ids_dispos)}", file=sys.stderr)
            sys.exit(1)
        sujets_list = sujets_filtres

    charger_memoire_drive()

    sections_redigees = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    entete_date = f"La date du jour est le {today_str}. Tu dois prendre en compte cette date si dans la suite des instructions, un critère de date est donné.\n\n"

    print("\n🔄 PHASE 1 & 2 : Traitement modulaire par sujet avec Retry...")

    for sujet_obj in sujets_list:
        sujet_nom = sujet_obj.get("sujet", "Sujet sans nom")
        print(f"\n📌 Traitement du sujet : '{sujet_nom}'")

        donnees_sujet = []
        for tentative in range(1, args.max_retries + 1):
            if tentative > 1:
                print(f"     ⏳ [Retry Sujet {tentative}/{args.max_retries}] Pause de {args.retry_delay}s avant nouvelle tentative sur '{sujet_nom}'...")
                time.sleep(args.retry_delay)

            donnees_sujet = exécuter_collecte_sujet(sujet_obj)

            if donnees_sujet:
                break
            else:
                print(f"     ⚠️ Tentative globale {tentative}/{args.max_retries} n'a retourné aucune donnée pour '{sujet_nom}'.")

        if not donnees_sujet:
            print(f"     └─ ⚪ Aucune donnée collectée après {args.max_retries} tentative(s).")
            sections_redigees.append((sujet_nom, "Aucune actualité majeure relevée cette semaine."))
            continue

        texte_brut = "\n".join(donnees_sujet)
        template_prompt = obtenir_prompt_sujet(sujet_obj)
        prompt_final = entete_date + template_prompt.format(sujet=sujet_nom, donnees=texte_brut)

        try:
            print(f"     └─ 🧠 Synthèse LLM personnalisée (temp={args.temperature})...")
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt_final,
                config=types.GenerateContentConfig(
                    temperature=args.temperature,
                    max_output_tokens=8192
                )
            )
            section_texte = response.text.strip()
            sections_redigees.append((sujet_nom, section_texte))
            print("     └─ ✅ Rédigé avec succès.")
        except Exception as e:
            print(f"     └─ ⚠️ Erreur synthèse LLM : {e}", file=sys.stderr)
            sections_redigees.append((sujet_nom, "Erreur lors de la synthèse des données."))

    # Publication (ciblée ou globale)
    print("\n🚀 PHASE 3 : Publication...")
    if args.sujet:
        titre_cible, contenu_cible = sections_redigees[0]
        publier_sujet_unique(titre_cible, contenu_cible)
    else:
        blocs_formates = [f"## {titre}\n\n{contenu}" for titre, contenu in sections_redigees]
        rapport_complet = "# Veille Stratégique & Technologique Hebdomadaire\n\n" + "\n\n---\n\n".join(blocs_formates)
        publier_rapport(rapport_complet)

    total_nouveaux = (
        STATS["rss_nouveaux"] +
        STATS["web_nouveaux"] +
        STATS["webpage_nouveaux"] +
        STATS["github_nouveaux"]
    )
    total_memoire = (
        STATS["rss_ignores_memoire"] +
        STATS["web_ignores_memoire"] +
        STATS["webpage_ignores_memoire"] +
        STATS["github_ignores_memoire"]
    )

    print("\n" + "=" * 60)
    print("📊 BILAN CHIFFRÉ DE LA COLLECTE")
    print("=" * 60)
    print(f"  📡 RSS    : {STATS['rss_nouveaux']} retenu(s) | {STATS['rss_ignores_memoire']} ignoré(s) (mémoire) | {STATS['rss_erreurs']} erreur(s) ({STATS['rss_fallbacks_gnews']} fallback GNews, {STATS['rss_fallbacks_web']} fallback Web)")
    print(f"  🔍 Web    : {STATS['web_nouveaux']} retenu(s) | {STATS['web_ignores_memoire']} ignoré(s) (mémoire) | {STATS['web_no_results']} 'No results found'")
    print(f"  🌐 Pages  : {STATS['webpage_nouveaux']} retenue(s) | {STATS['webpage_ignores_memoire']} inchangée(s) (mémoire) | {STATS['webpage_erreurs']} erreur(s)")
    print(f"  🐙 GitHub : {STATS['github_nouveaux']} retenu(s) | {STATS['github_ignores_memoire']} ignoré(s) (mémoire)")
    print("-" * 60)
    print(f"  ✅ Total éléments collectés      : {total_nouveaux}")
    if USE_MEMORY:
        print(f"  🧠 Total doublons filtrés (mémoire): {total_memoire}")
        print(f"  🧹 Entrées purgées (TTL > {args.ttl_days}j)   : {STATS['memoire_purges_ttl']}")
    print(f"  ⚠️ Total requêtes Web sans résultat : {STATS['web_no_results']}")
    print("=" * 60)

    print("\n✅ Processus terminé avec succès !")