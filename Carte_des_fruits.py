import streamlit as st
import folium
from streamlit_folium import st_folium
from collections import Counter
from folium.plugins import MarkerCluster, MousePosition
from folium.features import CustomIcon
import pandas as pd
from typing import Optional, Tuple
import urllib.parse
import uuid
from datetime import datetime

# --- géocodage (optionnel) ---
try:
    from geopy.geocoders import Nominatim
    from geopy.extra.rate_limiter import RateLimiter
    HAS_GEOPY = True
except Exception:
    HAS_GEOPY = False

st.set_page_config(page_title="Arbres & champignons – Lausanne", layout="wide")
st.title("Carte des arbres fruitiers & champignons à Lausanne")

# ============================================================
# 0) Mode persistant OBLIGATOIRE (Google Sheets) + garde-fou tolérant
# ============================================================
has_gcp = "gcp_service_account" in st.secrets
url_root = st.secrets.get("gsheets_spreadsheet_url")
url_in_gcp = st.secrets.get("gcp_service_account", {}).get("gsheets_spreadsheet_url") if has_gcp else None
url_any = url_root or url_in_gcp

missing = []
if not has_gcp:
    missing.append("gcp_service_account")
if not url_any:
    missing.append("gsheets_spreadsheet_url")

if missing:
    st.error(
        "Configuration manquante pour le stockage persistant : "
        + ", ".join(missing)
        + ".\n\n"
        "👉 Mets dans Paramètres → Secrets :\n"
        "- [gcp_service_account] (bloc TOML avec la clé JSON)\n"
        "- gsheets_spreadsheet_url (à la racine **ou** dans le bloc gcp_service_account)\n"
        "Optionnel : gsheets_worksheet_name (à la racine ou dans gcp_service_account ; défaut 'points')"
    )
    st.stop()

# ============================================================
# 1) Persistance (Google Sheets uniquement)
# ============================================================

def _serialize_seasons(lst):
    return "|".join(lst or [])

def _parse_seasons(s):
    if pd.isna(s) or not str(s).strip():
        return []
    return [x.strip() for x in str(s).split("|")]

def _now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def _gsheets_open():
    import gspread
    from google.oauth2.service_account import Credentials

    creds_info = st.secrets["gcp_service_account"]
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)

    # ✅ Lis l'URL et le nom d’onglet soit à la racine, soit (si jamais) dans le bloc gcp_service_account
    url = st.secrets.get("gsheets_spreadsheet_url") or st.secrets["gcp_service_account"].get("gsheets_spreadsheet_url")
    ws_name = st.secrets.get("gsheets_worksheet_name") or st.secrets["gcp_service_account"].get("gsheets_worksheet_name", "points")

    # Ouvre par URL complète ou par ID pur
    sh = gc.open_by_url(url) if str(url).startswith(("http://", "https://")) else gc.open_by_key(url)

    # Onglet
    try:
        ws = sh.worksheet(ws_name)
    except Exception:
        ws = sh.add_worksheet(title=ws_name, rows=1000, cols=10)
        ws.update("A1:G1", [["id", "name", "lat", "lon", "seasons", "is_deleted", "updated_at"]])
    return ws

@st.cache_data(ttl=10)
def _read_df():
    """Lit toutes les lignes depuis Google Sheets (avec cache court)."""
    ws = _gsheets_open()
    rows = ws.get_all_records()
    if not rows:
        return pd.DataFrame(columns=["id","name","lat","lon","seasons","is_deleted","updated_at"])
    df = pd.DataFrame(rows)
    if "is_deleted" not in df.columns:
        df["is_deleted"] = "0"
    if "seasons" not in df.columns:
        df["seasons"] = ""
    return df

def _invalidate_cache():
    st.cache_data.clear()

def _normalize_is_deleted(series: pd.Series) -> pd.Series:
    """Uniformise is_deleted en chaîne '0'/'1' robuste aux formats bizarres."""
    return (
        series.astype(str).str.strip()
              .str.replace("\u202f", "", regex=False)  # espace fine insécable
              .str.replace(" ", "", regex=False)
              .str.replace(",", ".", regex=False)
              .str.extract(r"(\d+)")
              .fillna("0")
    )

def _to_float_or_none(v):
    """Parse tolérant pour lat/lon: accepte virgule décimale, espaces, etc."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    s = s.replace("\u202f", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def load_items():
    """Retourne les items (non supprimés) comme liste de dicts."""
    df = _read_df()

    # Uniformise is_deleted en '0'/'1'
    if "is_deleted" not in df.columns:
        df["is_deleted"] = "0"
    df["is_deleted"] = _normalize_is_deleted(df["is_deleted"])

    # Ne garde que les lignes non supprimées
    df = df[df["is_deleted"] != "1"].copy()

    items = []
    for _, row in df.iterrows():
        lat = _to_float_or_none(row.get("lat"))
        lon = _to_float_or_none(row.get("lon"))
        if lat is None or lon is None:
            # ignore lignes invalides
            continue
        items.append({
            "id": str(row.get("id")),
            "name": row.get("name"),
            "lat": float(lat),
            "lon": float(lon),
            "seasons": _parse_seasons(row.get("seasons", "")),
        })
    return items

def add_item(name: str, lat: float, lon: float, seasons: list):
    """Append d'un nouvel item (UUID) dans la feuille."""
    ws = _gsheets_open()
    row = [
        str(uuid.uuid4()),
        name,
        float(lat),
        float(lon),
        _serialize_seasons(seasons or []),
        0,             # is_deleted (nombre)
        _now_iso(),    # updated_at
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    _invalidate_cache()

def soft_delete_item(item_id: str) -> bool:
    """Marque is_deleted=1 et met à jour updated_at pour l'item donné (1 seule mise à jour)."""
    import gspread
    from gspread.utils import rowcol_to_a1

    ws = _gsheets_open()
    values = ws.get_all_values()  # inclut l'entête

    if not values:
        return False

    headers = values[0]
    try:
        id_col     = headers.index("id") + 1          # 1-indexed
        isdel_col  = headers.index("is_deleted") + 1
        upd_col    = headers.index("updated_at") + 1
    except ValueError:
        st.error("Colonnes attendues absentes (id / is_deleted / updated_at).")
        return False

    # Trouve la ligne correspondant à l'ID (ignorer entête)
    row_idx = None
    for r in range(2, len(values) + 1):  # 1-indexed; démarre à la 2e ligne
        if values[r-1][id_col-1] == str(item_id):
            row_idx = r
            break

    if row_idx is None:
        st.warning("ID non trouvé ; rien supprimé.")
        return False

    # Construit la plage A1 pour les deux cellules à mettre à jour
    start_a1 = rowcol_to_a1(row_idx, isdel_col)
    end_a1   = rowcol_to_a1(row_idx, upd_col)
    rng = f"{start_a1}:{end_a1}"

    # Mise à jour en 1 appel : [ [is_deleted, updated_at] ]
    ws.update(
        rng,
        [[ "1", _now_iso() ]],
        value_input_option="RAW"
    )

    _invalidate_cache()
    return True

# ============================================================
# 2) État (session)
# ============================================================
if "trees" not in st.session_state:
    st.session_state["trees"] = load_items()

if "search_center" not in st.session_state:
    st.session_state["search_center"] = None
if "search_label" not in st.session_state:
    st.session_state["search_label"] = ""

# ============================================================
# 3) Catalogue & couleurs
# ============================================================
CATALOG = [
    "Pomme", "Poire", "Figue", "Grenade", "Kiwi", "Nèfle", "Kaki",
    "Noix", "Sureau", "Noisette",
    # champignons
    "Bolets", "Chanterelles", "Morilles",
]
colors = {
    "Figue": "purple",
    "Pomme": "red",
    "Kiwi": "green",
    "Noix": "darkgreen",
    "Grenade": "darkred",
    "Nèfle": "pink",
    "Noisette": "beige",
    "Poire": "lightgreen",
    "Kaki": "orange",
    "Sureau": "black",
    # champignons
    "Bolets": "#8B4513",
    "Chanterelles": "orange",
    "Morilles": "black",
}
MUSHROOM_SET = {"Bolets", "Chanterelles", "Morilles"}

# ============================================================
# 4) Actions & outils (placés AVANT filtrage + carte)
# ============================================================
st.sidebar.markdown("---")
if st.sidebar.button("🔄 Rafraîchir les données"):
    _invalidate_cache()
    st.session_state["trees"] = load_items()
    st.rerun()

st.sidebar.subheader("➕/➖ Ajouter ou supprimer un point")
mode = st.sidebar.radio("Choisir mode", ["Ajouter", "Supprimer"], index=0, horizontal=True, label_visibility="collapsed")

with st.sidebar.form("add_or_delete_form"):
    if mode == "Ajouter":
        new_name = st.selectbox("Catégorie", options=sorted(set(CATALOG + [t["name"] for t in st.session_state["trees"]])), index=0)
        col_a, col_b = st.columns(2)
        with col_a:
            new_lat = st.number_input("Latitude", value=46.519100, format="%.6f")
        with col_b:
            new_lon = st.number_input("Longitude", value=6.633600, format="%.6f")
        all_seasons = ["printemps", "été", "automne", "hiver"]
        new_seasons = st.multiselect("Saison(s)", options=all_seasons, default=["automne"])

        submitted_add = st.form_submit_button("Aj_
