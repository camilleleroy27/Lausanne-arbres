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

# ====== Mode mobile / compact (UI responsive légère) ======
MOBILE_COMPACT = st.sidebar.toggle("📱 Mode compact (mobile)", value=True)

# CSS responsive pour petits écrans
st.markdown("""
<style>
/* Réduit les marges globales sur mobile */
@media (max-width: 640px){
  .block-container { padding: 0.6rem 0.7rem !important; }
  .stSidebar { width: 78vw !important; } /* tiroir un peu plus large */
}
/* Légende plus petite et moins intrusive sur mobile */
@media (max-width: 640px){
  #legend-card { left: 12px !important; bottom: 12px !important; }
  #legend-card details { font-size: 12px !important; max-width: 180px !important; }
}
/* Affine les boutons/inputs sur mobile pour le touch */
@media (max-width: 640px){
  button, .stButton>button { padding: .5rem .8rem !important; font-size: 0.95rem !important; }
  .stSelectbox, .stTextInput, .stNumberInput { font-size: .95rem !important; }
}
/* Évite que la carte déborde horizontalement */
[data-testid="stHorizontalBlock"] { overflow: visible !important; }
</style>
""", unsafe_allow_html=True)

# Hauteur de carte adaptée (plus grande en mode mobile compact)
MAP_HEIGHT = 520
if MOBILE_COMPACT:
    MAP_HEIGHT = 620  # plus de hauteur utile sur petit écran

# Petite astuce UX : info pour replier la barre latérale sur mobile
if MOBILE_COMPACT:
    st.caption("📱 Astuce mobile : replie la barre latérale via l’icône ☰ pour profiter de toute la largeur de la carte.")

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
    "Noix", "Sureau", "Noisette", "Faînes",
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
    "Faînes": "#A0522D",
    # champignons
    "Bolets": "#8B4513",
    "Chanterelles": "orange",
    "Morilles": "black"
}
MUSHROOM_SET = {"Bolets", "Chanterelles", "Morilles"}

# ============================================================
# 4) Barre latérale — ordre : Filtres → Recherche → Ajout/Suppression → Refresh
# ============================================================
# … ton bloc sidebar (filtres, recherche, ajout/suppression, refresh) inchangé …

# ============================================================
# 6) Carte
# ============================================================
# … ton bloc folium, markers, etc. inchangé …

# Légende repliable
def legend_pin_dataurl(name: str) -> str:
    col = colors.get(name, "green")
    if name in MUSHROOM_SET:
        return build_pin_svg(col, glyph_mushroom_white(), w=18, h=24)
    else:
        return build_pin_svg(col, glyph_tree_white(), w=18, h=24)

legend_rows = []
for name in sorted(set(CATALOG)):
    img = legend_pin_dataurl(name)
    legend_rows.append(f"""
        <div style="display:flex; align-items:center; gap:8px; margin:4px 0;">
          <img src="{img}" width="16" height="16" />
          <span>{name}</span>
        </div>
    """)
legend_body = "".join(legend_rows)

legend_open_attr = "open" if not MOBILE_COMPACT else ""

legend_html = f"""
<style>
  #legend-card summary {{ list-style: none; cursor: pointer; font-weight: 600; }}
  #legend-card summary::-webkit-details-marker {{ display: none; }}
  #legend-card summary::after {{ content: "▸"; margin-left: 8px; font-size: 12px; opacity: .6; }}
  #legend-card details[open] summary::after {{ content: "▾"; }}
</style>
<div id="legend-card" style="position: fixed; bottom: 24px; left: 24px; z-index: 9999;">
  <details {legend_open_attr} style="background:#fff;border:1px solid #ccc;border-radius:10px;padding:8px 10px;box-shadow:0 2px 10px rgba(0,0,0,0.15);max-width:240px;font-size:13px;">
    <summary>📖 Légende</summary>
    <div style="margin-top: 8px; max-height: 240px; overflow: auto;">{legend_body}</div>
  </details>
</div>
"""

m.get_root().html.add_child(folium.Element(legend_html))

# Affichage carte
st_folium(m, width=None, height=MAP_HEIGHT)

# ============================================================
# 7) Stats & export
# ============================================================
with st.expander("📊 Statistiques & export", expanded=not MOBILE_COMPACT):
    counts = Counter(t["name"] for t in filtered)
    total = len(filtered)
    if total == 0:
        st.write("Aucun point (vérifie les filtres).")
    else:
        st.write(f"Total : **{total}**")
        st.markdown("\n".join(f"- {k} : **{counts[k]}**" for k in sorted(counts)))

    st.markdown("---")
    _df_full = _read_df()
    if "is_deleted" not in _df_full.columns:
        _df_full["is_deleted"] = "0"
    _df_full["is_deleted"] = _normalize_is_deleted(_df_full["is_deleted"])
    _df_export = _df_full[_df_full["is_deleted"] != "1"][["name","lat","lon","seasons"]].copy()
    st.download_button(
        "⬇️ Télécharger tous les points (CSV)",
        data=_df_export.to_csv(index=False),
        file_name="arbres_lausanne.csv",
        mime="text/csv",
    )

st.caption(f"🌳 Points affichés : {len(filtered)} / {len(st.session_state['trees'])}")
