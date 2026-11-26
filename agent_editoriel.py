import os
import sys
import json
import argparse
from datetime import datetime
import requests
from google import genai
from google.genai import types
from google.genai.errors import APIError

# 1. Arguments CLI (Températures, Jour, Sujet de l'editorial, Dossier de prompts & Mode de restitution)
parser = argparse.ArgumentParser(description="Agent Éditorial Multi-Températures guidé par postures quotidiennes.")
parser.add_argument(
    "-j", "--jour", 
    type=str, 
    default="LUNDI", 
    help="Nom du jour de la semaine (ex: LUNDI, MARDI... DIMANCHE) pour sélectionner la posture et le Google Doc"
)

parser.add_argument(
    "-s", "--subject", 
    type=str, 
    default="", 
    help="Sujet qui apparait dans le titre de l'editorial"
)

parser.add_argument(
    "-t1", "--temp1", 
    type=float, 
    default=0.9, 
    help="Température pour la Passe 1 (Génération créative selon la posture, défaut: 0.9)"
)
parser.add_argument(
    "-t2", "--temp2", 
    type=float, 
    default=0.1, 
    help="Température pour la Passe 2 (Modération & sobriété, défaut: 0.1)"
)
parser.add_argument(
    "-p", "--prompts-dir", 
    type=str, 
    default="prompts", 
    help="Dossier contenant les prompts d'analyse (par défaut: prompts)"
)
parser.add_argument(
    "-o", "--output-mode", 
    type=str, 
    choices=["BOTH", "T1", "T2"], 
    default="BOTH", 
    help="Sélection du contenu à publier dans le Google Doc : BOTH (T1+T2), T1 (seulement Passe 1), T2 (seulement Passe 2). Défaut: BOTH"
)
args = parser.parse_args()

# 2. Variables d'environnement & Client Gemini
api_key = os.environ.get("GEMINI_API_KEY")
webhook_url = os.environ.get("WEBHOOK_URL")
webhook_secret = os.environ.get("WEBHOOK_SECRET")

if not api_key or not webhook_url or not webhook_secret:
    print("❌ Erreur critique : GEMINI_API_KEY, WEBHOOK_URL et WEBHOOK_SECRET sont obligatoires.", file=sys.stderr)
    sys.exit(1)

client = genai.Client(api_key=api_key)

# 3. Chargement dynamique des prompts T1 et T2
def charger_prompt_t1(jour: str, prompts_dir: str) -> str:
    fichier_jour = os.path.join(prompts_dir, f"agent_editorial_T1_{jour.upper()}.txt")
    if os.path.exists(fichier_jour):
        print(f"  └─ 📄 Prompt T1 chargé depuis '{fichier_jour}'")
        with open(fichier_jour, "r", encoding="utf-8") as f:
            return f.read()
    else:
        print(f"❌ Erreur critique : Le fichier prompt T1 '{fichier_jour}' est introuvable.", file=sys.stderr)
        sys.exit(1)

def charger_prompt_t2(prompts_dir: str) -> str:
    fichier_t2 = os.path.join(prompts_dir, "agent_editorial_T2.txt")
    if os.path.exists(fichier_t2):
        print(f"  └─ 📄 Prompt T2 chargé depuis '{fichier_t2}'")
        with open(fichier_t2, "r", encoding="utf-8") as f:
            return f.read()
    else:
        print(f"❌ Erreur critique : Le fichier prompt T2 '{fichier_t2}' est introuvable.", file=sys.stderr)
        sys.exit(1)

# 4. Récupération du dernier rapport brut depuis Drive via Webhook
def recuperer_dernier_rapport() -> str:
    print("  └─ ⚙️ [Webhook] Récupération du dernier rapport de veille...")
    payload = {
        "secret": webhook_secret,
        "action": "get_latest_report"
    }
    try:
        res = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "success":
                contenu = data.get("content", "")
                print(f"  └─ ✅ Rapport récupéré ({len(contenu)} caractères).")
                return contenu
            else:
                raise RuntimeError(f"Erreur Apps Script: {data.get('message')}")
        else:
            raise RuntimeError(f"Erreur HTTP: {res.status_code}")
    except Exception as e:
        print(f"❌ Erreur critique lors de la récupération du rapport : {e}", file=sys.stderr)
        sys.exit(1)

# 5. Execution de la double passe avec filtrage de sortie selon output_mode
def executer_double_passe(texte_brut: str, temp1: float, temp2: float, jour: str, prompts_dir: str, output_mode: str, subject: str) -> str:
    print(f"\n🧠 [Algorithme Multi-Températures] Exécution pour [{jour.upper()}] (Mode={output_mode} | T1={temp1} | T2={temp2})...")
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    entete_date = f"La date du jour est le {today_str}. Tu dois prendre en compte cette date si dans la suite des instructions, un critère de date est donné.\n\n"

    # --- PASSE 1 : Posture spécifique du Jour (T1) ---
    prompt_t1_template = charger_prompt_t1(jour, prompts_dir)
    prompt_p1 = entete_date + prompt_t1_template.format(texte_brut=texte_brut)

    print(f"  ├─ 🔥 Passe 1 (T1 = {temp1}) : Rédaction selon la posture du {jour.upper()}...")
    res_p1 = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt_p1,
        config=types.GenerateContentConfig(temperature=temp1)
    )
    texte_p1 = res_p1.text.strip()
    print("  │  ✅ Passe 1 terminée.")

    # --- PASSE 2 : Sobriété & Modération (T2) ---
    # Toujours exécutée car T2 a besoin de texte_p1
    prompt_t2_template = charger_prompt_t2(prompts_dir)
    prompt_p2 = entete_date + prompt_t2_template.format(texte_p1=texte_p1)

    print(f"  └─ 🧊 Passe 2 (T2 = {temp2}) : Relecture et modération...")
    res_p2 = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt_p2,
        config=types.GenerateContentConfig(temperature=temp2)
    )
    texte_p2 = res_p2.text.strip()
    print("  └─ ✅ Passe 2 terminée.")

    # Formatage du document final selon le mode choisi
    if output_mode == "T1":
        print("  └─ 📝 Restitution sélectionnée : PASSE 1 uniquement (T1)")
        document_final = f"""# Éditorial du {jour.capitalize()} {subject.capitalize()} (Passe 1 - T1={temp1})

---

{texte_p1}
"""
    elif output_mode == "T2":
        print("  └─ 📝 Restitution sélectionnée : PASSE 2 uniquement (T2)")
        document_final = f"""# Éditorial du {jour.capitalize()} {subject.capitalize()} (Note Consolidée - T2={temp2})

---

{texte_p2}
"""
    else:  # BOTH
        print("  └─ 📝 Restitution sélectionnée : COMPLÈTE (T1 + T2)")
        document_final = f"""# Éditorial du {jour.capitalize()} {subject.capitalize()}

---

## 🟠 Version Exploration & Posture du Jour (Température {temp1})

{texte_p1}

---

## 🔵 Version Note Consolidée & Modérée (Température {temp2})

{texte_p2}
"""

    return document_final

# 6. Publication Webhook
def publier_editoriel(contenu: str, jour: str):
    print(f"\n🚀 [Webhook] Envoi de la publication pour [{jour.upper()}]...")
    payload = {
        "secret": webhook_secret,
        "action": "publish_editoriel",
        "jour": jour.upper(),
        "content": contenu
    }
    try:
        res = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        if res.status_code == 200 and res.json().get("status") == "success":
            print(f"  └─ ✅ Éditorial du {jour.upper()} publié et archivé dans Google Drive avec succès.")
        else:
            print(f"❌ Échec de la publication : {res.text}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"❌ Erreur de publication Webhook : {e}", file=sys.stderr)
        sys.exit(1)

# 7. Pipeline principal
if __name__ == "__main__":
    try:
        jour_clean = args.jour.upper().strip()
        subject_clean = args.subject.upper().strip()
        mode_clean = args.output_mode.upper().strip()
        print(f"⚙️ Lancement : Jour = {jour_clean} | Mode = {mode_clean} | Temp1 = {args.temp1} | Temp2 = {args.temp2}")
        
        texte_brut = recuperer_dernier_rapport()
        if not texte_brut or len(texte_brut.strip()) < 100:
            print("⚠️ Le rapport de veille brut me semble vide ou incomplet. Annulation.", file=sys.stderr)
            sys.exit(1)
            
        analyse_combinee = executer_double_passe(
            texte_brut, 
            temp1=args.temp1, 
            temp2=args.temp2, 
            jour=jour_clean,
            subject=subject_clean,
            prompts_dir=args.prompts_dir,
            output_mode=mode_clean
        )
        publier_editoriel(analyse_combinee, jour=jour_clean)
        print(f"\n🎉 Traitement éditorial ({jour_clean} - Mode {mode_clean}) terminé avec succès !")
    except APIError as api_err:
        print(f"\n❌ Erreur API Gemini : {api_err}", file=sys.stderr)
        sys.exit(1)
    except Exception as err:
        print(f"\n❌ Erreur inattendue : {err}", file=sys.stderr)
        sys.exit(1)