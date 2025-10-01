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
# 0) Garde-fou secrets (Google Sheets requis)
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
        "👉 Paramètres → Secrets :\n"
        "- [gcp_service_account] (bloc TOML avec la clé JSON)\n"
        "- gsheets_spreadsheet_url (à la racine **ou** dans le bloc gcp_service_account)\n"
        "Optionnel : gsheets_worksheet_name (défaut 'points')"
    )
    st.stop()

# ============================================================
# 1) Persistance Google Sheets (auto-fix entête + locale FR)
# ============================================================
EXPECTED_HEADER = ["id","name","lat","lon","seasons","is_deleted","updated_at"]

def _serialize_seasons(lst):
    return "|".join(lst or [])

def _parse_seasons(s):
    if pd.isna(s) or not str(s).strip():
        return []
    return [x.strip() for x in str(s).split("|")]

def _to_float(x):
    """Accepte '46,5191' ou '46.5191' (et espaces fines)."""
    s = str(x).strip().replace("\u202f", "").replace(" ", "")
    s = s.replace(",", ".")
    return float(s)

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

    url = st.secrets.get("gsheets_spreadsheet_url") or st.secrets["gcp_service_account"].get("gsheets_spreadsheet_url")
    ws_name = st.secrets.get("gsheets_worksheet_name") or st.secrets["gcp_service_account"].get("gsheets_worksheet_name", "points")

    sh = gc.open_by_url(url) if str(url).startswith(("http://", "https://")) else gc.open_by_key(url)
    try:
        ws = sh.worksheet(ws_name)
    except Exception:
        ws = sh.add_worksheet(title=ws_name, rows=1000, cols=10)
        ws.update("A1:G1", [EXPECTED_HEADER])
    return ws

def _ensure_header(ws) -> None:
    """Répare silencieusement l'entête si absente/cassée (pas de bouton nécessaire)."""
    values = ws.get_all_values()
    if not values:
        ws.update("A1:G1", [EXPECTED_HEADER])
        return
    headers = values[0]
    # si l'entête ne contient pas l'essentiel -> on réécrit l'entête correcte
    if not set(["id", "name", "lat", "lon"]).issubset(set(headers)):
        ws.update("A1:G1", [EXPECTED_HEADER])

@st.cache_data(ttl=10)
def _read_df():
    """Lit toutes les lignes depuis Google Sheets (auto-fix entête)."""
    ws = _gsheets_open()
    _ensure_header(ws)

    rows = ws.get_all_records()
    if not rows:
        df = pd.DataFrame(columns=EXPECTED_HEADER)
    else:
        df = pd.DataFrame(rows)

    # Colonnes manquantes -> par défaut (par prudence)
    for col in EXPECTED_HEADER:
        if col not in df.columns:
            df[col] = "" if col != "is_deleted" else "0"

    # Normaliser
    df["is_deleted"] = df["is_deleted"].astype(str).replace("", "0")

    return df

def _invalidate_cache():
    st.cache_data.clear()

def load_items():
    """Retourne les items (non supprimés) comme liste de dicts (lat/lon robustes)."""
    df = _read_df()
    df = df[df["is_deleted"] != "1"].copy()
    items = []
    for _, row in df.iterrows():
        try:
            lat = _to_float(row["lat"])
            lon = _to_float(row["lon"])
        except Exception:
            continue  # ignore lignes invalides
        items.append({
            "id": str(row.get("id", "")),
            "name": row.get("name", ""),
            "lat": lat,
            "lon": lon,
            "seasons": _parse_seasons(row.get("seasons", "")),
        })
    return items

def add_item(name: str, lat: float, lon: float, seasons: list):
    """Append d'un nouvel item (UUID) dans la feuille (RAW pour éviter la locale)."""
    ws = _gsheets_open()
    _ensure_header(ws)
    row = [
        str(uuid.uuid4()),
        name,
        float(lat),
        float(lon),
        _serialize_seasons(seasons or []),
        "0",
        _now_iso(),
    ]
    ws.append_row(row, value_input_option="RAW")
    _invalidate_cache()

def soft_delete_item(item_id: str):
    """Marque is_deleted=1 pour l'élément correspondant à item_id."""
    ws = _gsheets_open()
    _ensure_header(ws)
    values = ws.get_all_values()
    if not values:
        return
    headers = values[0]
    try:
        id_col = headers.index("id")
        isdel_col = headers.index("is_deleted")
        upd_col = headers.index("updated_at")
    except ValueError:
        return
    for r_idx in range(1, len(values)):  # 1 = sauter l'entête
        if values[r_idx][id_col] == item_id:
            ws.update_cell(r_idx+1, isdel_col+1, "1")
            ws.update_cell(r_idx+1, upd_col+1, _now_iso())
            _invalidate_cache()
            return

# ============================================================
# 2) État (session) — toujours resynchroniser à chaque run
# ============================================================
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
# 4) Actions (incl. mode test icônes + debug)
# ============================================================
st.sidebar.markdown("---")

# 🧪 Interrupteur : forcer des icônes folium simples (bypass SVG)
use_simple_icons = st.sidebar.checkbox("🧪 Icônes simples (test)", value=False)

if st.sidebar.button("🔄 Rafraîchir les données"):
    _invalidate_cache()
    st.session_state["trees"] = load_items()
    st.rerun()

with st.sidebar.expander("🔍 Debug (temporaire)"):
    try:
        _df = _read_df()
        st.write("Lignes lues depuis Google Sheets :", len(_df))
        st.write("Colonnes :", list(_df.columns))
        st.dataframe(_df.head(10))
    except Exception as e:
        st.error(f"Erreur lecture DF : {e}")

# ============================================================
# 5) Filtres + recherche
# ============================================================
st.sidebar.header("Filtres")
basemap_label_to_tiles = {
    "CartoDB positron (clair)": "CartoDB positron",
    "OpenStreetMap": "OpenStreetMap",
}
basemap_label = st.sidebar.selectbox("Type de carte", list(basemap_label_to_tiles.keys()), index=0)
basemap = basemap_label_to_tiles[basemap_label]

items = st.session_state["trees"]
all_types = sorted(set([t["name"] for t in items] + CATALOG))
all_seasons = ["printemps", "été", "automne", "hiver"]

selected_types = st.sidebar.multiselect("Catégorie(s) à afficher", options=all_types, default=[])
selected_seasons = st.sidebar.multiselect("Saison(s) de récolte", options=all_seasons, default=[])

# --- Recherche d'adresse / rue ---
st.sidebar.markdown("---")
st.sidebar.subheader("🔎 Rechercher une adresse / rue")
BBOX_SW = (46.47, 6.48)
BBOX_NE = (46.60, 6.80)

def geocode_address_biased(q: str, commune: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    if not HAS_GEOPY:
        return None, None, None
    geolocator = Nominatim(user_agent="carte_arbres_lausanne_app")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1, swallow_exceptions=True)
    trials = []
    if commune and not commune.startswith("Auto"):
        trials += [f"{q}, {commune}, Vaud, Switzerland", f"{q}, {commune}, Switzerland"]
    trials += [f"{q}, Lausanne District, Vaud, Switzerland", f"{q}, Vaud, Switzerland", f"{q}, Switzerland", q]
    for query in trials:
        loc = geocode(query, country_codes="ch", viewbox=(BBOX_SW, BBOX_NE), bounded=False, addressdetails=True, exactly_one=True)
        if loc:
            return float(loc.latitude), float(loc.longitude), loc.address
    return None, None, None

COMMUNES = [
    "Auto (région Lausanne)", "Lausanne", "Pully", "Lutry", "Paudex",
    "Épalinges", "Prilly", "Renens", "Crissier",
    "Chavannes-près-Renens", "Ecublens", "Le Mont-sur-Lausanne", "Belmont-sur-Lausanne",
]
addr = st.sidebar.text_input("Adresse (ex: Avenue de Lavaux 10)")
commune_choice = st.sidebar.selectbox("Commune (optionnel)", COMMUNES, index=0)

c1, c2 = st.sidebar.columns(2)
if c1.button("Chercher"):
    if not addr.strip():
        st.sidebar.warning("Saisis une adresse.")
    elif not HAS_GEOPY:
        st.sidebar.error("geopy n'est pas installé (python3 -m pip install geopy).")
    else:
        lat, lon, label = geocode_address_biased(addr.strip(), commune_choice)
        if lat and lon:
            st.session_state["search_center"] = (lat, lon)
            st.session_state["search_label"] = label or f"{addr.strip()} ({commune_choice})"
            st.sidebar.success("Adresse trouvée ✅")
        else:
            st.session_state["search_center"] = None
            st.session_state["search_label"] = ""
            st.sidebar.error("Adresse introuvable. Essaie avec un numéro ou une autre commune.")

if c2.button("Réinitialiser recherche"):
    st.session_state["search_center"] = None
    st.session_state["search_label"] = ""

# Filtrage
filtered = items
if selected_types:
    filtered = [t for t in filtered if t["name"] in selected_types]
if selected_seasons:
    filtered = [t for t in filtered if any(s in selected_seasons for s in t["seasons"])]

# Alerte si aucun point
if len(items) == 0:
    st.warning("Aucun point chargé depuis Google Sheets. Vérifie l’onglet (gsheets_worksheet_name), l’entête (id,name,lat,lon,...) et les lat/lon.")
# ============================================================
# 6) Carte
# ============================================================
default_center = [46.5191, 6.6336]
if st.session_state["search_center"] is not None:
    center = list(st.session_state["search_center"]); zoom = 16
else:
    center = default_center; zoom = 12

m = folium.Map(location=center, zoom_start=zoom, tiles=basemap)

# === Ma maison : Avenue des Collèges 29 ===
HOUSE_LAT = 46.5105
HOUSE_LON = 6.6528
folium.Marker(
    location=[HOUSE_LAT, HOUSE_LON],
    tooltip="Ma maison",
    popup="⛪️ Ma maison — Avenue des Collèges 29",
    icon=folium.DivIcon(html="""
        <div style="font-size:40px; line-height:40px; transform: translate(
