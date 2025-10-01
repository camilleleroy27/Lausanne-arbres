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
        "👉 Paramètres → Secrets :\n"
        "- [gcp_service_account] (bloc TOML avec la clé JSON)\n"
        "- gsheets_spreadsheet_url (à la racine **ou** dans le bloc gcp_service_account)\n"
        "Optionnel : gsheets_worksheet_name (défaut 'points')"
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

def _to_float(x):
    """Convertit '46,5191' ou '46.5191' (avec espaces fines) en float."""
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

    # ✅ URL/onglet à la racine OU dans gcp_service_account
    url = st.secrets.get("gsheets_spreadsheet_url") or st.secrets["gcp_service_account"].get("gsheets_spreadsheet_url")
    ws_name = st.secrets.get("gsheets_worksheet_name") or st.secrets["gcp_service_account"].get("gsheets_worksheet_name", "points")

    sh = gc.open_by_url(url) if str(url).startswith(("http://", "https://")) else gc.open_by_key(url)
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

def load_items():
    """Retourne les items (non supprimés) comme liste de dicts."""
    df = _read_df()
    df = df[df["is_deleted"] != "1"].copy()
    items = []
    for _, row in df.iterrows():
        try:
            lat = _to_float(row["lat"])
            lon = _to_float(row["lon"])
        except Exception:
            # ignore lignes invalides
            continue
        items.append({
            "id": str(row.get("id", "")),
            "name": row.get("name", ""),
            "lat": lat,
            "lon": lon,
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
        "0",           # is_deleted
        _now_iso(),    # updated_at
    ]
    # ✅ RAW pour éviter que Sheets remette des virgules via la locale
    ws.append_row(row, value_input_option="RAW")
    _invalidate_cache()

def soft_delete_item(item_id: str):
    """Marque is_deleted=1 pour l'élément correspondant à item_id."""
    ws = _gsheets_open()
    values = ws.get_all_values()  # incluant l'entête
    if not values:
        return
    headers = values[0]
    try:
        id_col = headers.index("id")
        isdel_col = headers.index("is_deleted")
        upd_col = headers.index("updated_at")
    except ValueError:
        st.error("Colonnes attendues absentes dans la feuille (id / is_deleted / updated_at).")
        return
    for r_idx in range(1, len(values)):  # sauter l'entête
        if values[r_idx][id_col] == item_id:
            ws.update_cell(r_idx+1, isdel_col+1, "1")   # gspread est 1-indexed
            ws.update_cell(r_idx+1, upd_col+1, _now_iso())
            _invalidate_cache()
            return
    st.warning("ID non trouvé ; rien supprimé.")

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

        submitted_add = st.form_submit_button("Ajouter & enregistrer")
        if submitted_add:
            try:
                add_item(new_name, float(new_lat), float(new_lon), new_seasons or [])
                if new_name not in colors:
                    colors[new_name] = "green"
                st.session_state["trees"] = load_items()
                st.success(f"Ajouté : {new_name} ✅ (persisté)")
                st.rerun()  # 🔁 reconstruit la carte à jour
            except Exception as e:
                st.error(f"Erreur lors de l'ajout : {e}")

    else:  # Supprimer
        trees = st.session_state.get("trees", [])
        if not trees:
            st.info("Aucun point à supprimer.")
            _ = st.form_submit_button("Supprimer définitivement", disabled=True)
        else:
            options_labels, idx_to_id = [], {}
            for i, t in enumerate(trees):
                seasons_txt = _serialize_seasons(t.get("seasons", [])) or "—"
                options_labels.append(f"{i+1}. {t['name']} – {t['lat']:.5f}, {t['lon']:.5f} [{seasons_txt}]")
                idx_to_id[i] = t["id"]

            idx_choice = st.selectbox("Choisis le point à supprimer", options=list(idx_to_id.keys()), format_func=lambda i: options_labels[i])
            confirm = st.checkbox("Je confirme la suppression", value=False)
            submitted_del = st.form_submit_button("Supprimer définitivement", disabled=not confirm)

            if submitted_del and confirm:
                try:
                    soft_delete_item(idx_to_id[idx_choice])
                    st.session_state["trees"] = load_items()
                    st.success("Point supprimé (soft delete) ✅")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erreur lors de la suppression : {e}")

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
        <div style="font-size:40px; line-height:40px; transform: translate(-18px, -32px);">⛪️</div>
    """),
).add_to(m)

cluster = MarkerCluster().add_to(m)

# Pins SVG
PIN_SVG_TEMPLATE = """
<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 36 48">
  <ellipse cx="18" cy="46" rx="7" ry="2.5" fill="rgba(0,0,0,0.25)"/>
  <path d="M18 0 C 8 0, 1 7.5, 1 17 C 1 26.5, 9 31.5, 13 37.5
           C 15 40.5, 16.5 44, 18 48 C 19.5 44, 21 40.5, 23 37.5
           C 27 31.5, 35 26.5, 35 17 C 35 7.5, 28 0, 18 0 Z"
        fill="{FILL}" stroke="rgba(0,0,0,0.35)" stroke-width="1"/>
  <circle cx="18" cy="17" r="9" fill="rgba(255,255,255,0.12)"/>
  {GLYPH}
</svg>
""".strip()

def glyph_tree_white() -> str:
    return """
    <polygon points="18,8 12,13 24,13" fill="white"/>
    <polygon points="18,11 11,16.5 25,16.5" fill="white"/>
    <polygon points="18,14 11,21 25,21" fill="white"/>
    <rect x="16.2" y="21" width="3.6" height="5.5" rx="1.2" fill="white"/>
    """.strip()

def glyph_mushroom_white() -> str:
    return """
    <path d="M9,18 C9,13 13,10 18,10 C23,10 27,13 27,18 L9,18 Z" fill="white"/>
    <rect x="15.5" y="18" width="5" height="7" rx="2" fill="white"/>
    """.strip()

def build_pin_svg(fill_color: str, glyph: str, w=36, h=48) -> str:
    svg = PIN_SVG_TEMPLATE.format(W=w, H=h, FILL=fill_color, GLYPH=glyph)
    return "data:image/svg+xml;charset=UTF-8," + urllib.parse.quote(svg)

def make_custom_pin(fill_color: str, for_mushroom: bool) -> CustomIcon:
    glyph = glyph_mushroom_white() if for_mushroom else glyph_tree_white()
    url = build_pin_svg(fill_color, glyph)
    return CustomIcon(icon_image=url, icon_size=(30, 42), icon_anchor=(15, 40))

# Markers
def add_tree_marker(tree):
    fill = colors.get(tree["name"], "green")
    folium.Marker(
        location=[tree["lat"], tree["lon"]],
        popup=f"{tree['name']}",
        icon=make_custom_pin(fill, for_mushroom=False),
    ).add_to(cluster)

def add_mushroom_marker(tree):
    fill = colors.get(tree["name"], "gray")
    folium.Marker(
        location=[tree["lat"], tree["lon"]],
        popup=f"{tree['name']}",
        icon=make_custom_pin(fill, for_mushroom=True),
    ).add_to(cluster)

for t in filtered:
    if t["name"] in MUSHROOM_SET:
        add_mushroom_marker(t)
    else:
        add_tree_marker(t)

# Repère de recherche
if st.session_state["search_center"] is not None:
    folium.Marker(
        location=center,
        tooltip=st.session_state["search_label"] or "Résultat de recherche",
        popup=st.session_state["search_label"] or "Résultat de recherche",
        icon=folium.Icon(color="blue", icon="search", prefix="fa"),
    ).add_to(m)
    folium.Circle(location=center, radius=35, color="blue", fill=True, fill_opacity=0.15).add_to(m)

# Outils lat/lon
folium.LatLngPopup().add_to(m)
MousePosition(position="topright", separator=" | ", empty_string="", num_digits=6, prefix="📍").add_to(m)

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

legend_html = f"""
<style>
  #legend-card summary {{ list-style: none; cursor: pointer; font-weight: 600; }}
  #legend-card summary::-webkit-details-marker {{ display: none; }}
  #legend-card summary::after {{ content: "▸"; margin-left: 8px; font-size: 12px; opacity: .6; }}
  #legend-card details[open] summary::after {{ content: "▾"; }}
</style>
<div id="legend-card" style="position: fixed; bottom: 24px; left: 24px; z-index: 9999;">
  <details style="background:#fff;border:1px solid #ccc;border-radius:10px;padding:8px 10px;box-shadow:0 2px 10px rgba(0,0,0,0.15);max-width:240px;font-size:13px;">
    <summary>📖 Légende</summary>
    <div style="margin-top: 8px; max-height: 240px; overflow: auto;">{legend_body}</div>
  </details>
</div>
"""
m.get_root().html.add_child(folium.Element(legend_html))

# Affichage carte
st_folium(m, width=900, height=520)

# ============================================================
# 7) Stats & export
# ============================================================
counts = Counter(t["name"] for t in filtered)
total = len(filtered)
st.markdown("**📊 Statistiques (points affichés)**")
if total == 0:
    st.write("Aucun point (vérifie les filtres).")
else:
    st.write(f"Total : **{total}**")
    st.markdown("\n".join(f"- {k} : **{counts[k]}**" for k in sorted(counts)))

st.markdown("---")
_df_full = _read_df()
_df_export = _df_full[_df_full["is_deleted"] != "1"][["name","lat","lon","seasons"]].copy()
st.download_button(
    "⬇️ Télécharger tous les points (CSV)",
    data=_df_export.to_csv(index=False),
    file_name="arbres_lausanne.csv",
    mime="text/csv",
)
st.caption(f"🌳 Points affichés : {len(filtered)} / {len(st.session_state['trees'])}")
