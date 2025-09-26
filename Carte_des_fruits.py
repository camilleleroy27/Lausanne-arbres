import streamlit as st
import folium
from streamlit_folium import st_folium
from collections import Counter
from folium.plugins import MarkerCluster, MousePosition
from folium.features import CustomIcon
import pandas as pd
from pathlib import Path
from typing import Optional, Tuple
import urllib.parse

# --- géocodage (optionnel) ---
try:
    from geopy.geocoders import Nominatim
    from geopy.extra.rate_limiter import RateLimiter
    HAS_GEOPY = True
except Exception:
    HAS_GEOPY = False

st.set_page_config(page_title="Arbres & champignons – Lausanne", layout="wide")
st.title("Carte des arbres fruitiers & champignons à Lausanne")

# ------------------ Données par défaut ------------------
initial_items = []  # on démarre vide

# ------------------ Persistance (CSV local) ------------------
DATA_CSV = Path("arbres_points.csv")

def _serialize_seasons(lst):
    return "|".join(lst or [])

def _parse_seasons(s):
    if pd.isna(s) or not str(s).strip():
        return []
    return [x.strip() for x in str(s).split("|")]

def load_items():
    if DATA_CSV.exists():
        df = pd.read_csv(DATA_CSV)
        items = []
        for _, row in df.iterrows():
            items.append({
                "name": row["name"],
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "seasons": _parse_seasons(row.get("seasons", "")),
            })
        return items
    return initial_items.copy()

def save_items(items):
    df = pd.DataFrame([{
        "name": t["name"],
        "lat": t["lat"],
        "lon": t["lon"],
        "seasons": _serialize_seasons(t.get("seasons", [])),
    } for t in items])
    df.to_csv(DATA_CSV, index=False)

# ------------------ État (session) ------------------
if "trees" not in st.session_state:
    st.session_state["trees"] = load_items()

if "search_center" not in st.session_state:
    st.session_state["search_center"] = None
if "search_label" not in st.session_state:
    st.session_state["search_label"] = ""

items = st.session_state["trees"]

# ------------------ Catalogue & couleurs ------------------
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
    "Bolets": "#8B4513",       # brun
    "Chanterelles": "orange",  # orange
    "Morilles": "black",       # noir
}
MUSHROOM_SET = {"Bolets", "Chanterelles", "Morilles"}

# ------------------ Barre latérale : filtres + recherche ------------------
st.sidebar.header("Filtres")

basemap_label_to_tiles = {
    "CartoDB positron (clair)": "CartoDB positron",
    "OpenStreetMap": "OpenStreetMap",
}
basemap_label = st.sidebar.selectbox("Type de carte", list(basemap_label_to_tiles.keys()), index=0)
basemap = basemap_label_to_tiles[basemap_label]

all_types = sorted(set([t["name"] for t in items] + CATALOG))
all_seasons = ["printemps", "été", "automne", "hiver"]

selected_types = st.sidebar.multiselect("Catégorie(s) à afficher", options=all_types, default=[])
selected_seasons = st.sidebar.multiselect("Saison(s) de récolte", options=all_seasons, default=[])

# --- Recherche d'adresse / rue ---
st.sidebar.markdown("---")
st.sidebar.subheader("🔎 Rechercher une adresse / rue")

BBOX_SW = (46.47, 6.48)  # (lat_sud, lon_ouest)
BBOX_NE = (46.60, 6.80)  # (lat_nord, lon_est)

def geocode_address_biased(q: str, commune: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    if not HAS_GEOPY:
        return None, None, None

    geolocator = Nominatim(user_agent="carte_arbres_lausanne_app")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1, swallow_exceptions=True)

    trials = []
    if commune and not commune.startswith("Auto"):
        trials += [
            f"{q}, {commune}, Vaud, Switzerland",
            f"{q}, {commune}, Switzerland",
        ]
    trials += [
        f"{q}, Lausanne District, Vaud, Switzerland",
        f"{q}, Vaud, Switzerland",
        f"{q}, Switzerland",
        q,
    ]

    for query in trials:
        loc = geocode(
            query,
            country_codes="ch",
            viewbox=(BBOX_SW, BBOX_NE),
            bounded=False,
            addressdetails=True,
            exactly_one=True,
        )
        if loc:
            return float(loc.latitude), float(loc.longitude), loc.address

    return None, None, None

COMMUNES = [
    "Auto (région Lausanne)",
    "Lausanne", "Pully", "Lutry", "Paudex",
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

# ------------------ Filtrage ------------------
filtered = items
if selected_types:
    filtered = [t for t in filtered if t["name"] in selected_types]
if selected_seasons:
    filtered = [t for t in filtered if any(s in selected_seasons for s in t["seasons"])]

# ------------------ Carte ------------------
default_center = [46.5191, 6.6336]
if st.session_state["search_center"] is not None:
    center = list(st.session_state["search_center"])
    zoom = 16
else:
    center = default_center
    zoom = 12

m = folium.Map(location=center, zoom_start=zoom, tiles=basemap)

# === Ma maison : Avenue des Collèges 29 (coordonnées fournies) ===
HOUSE_LAT = 46.5105
HOUSE_LON = 6.6528

folium.Marker(
    location=[HOUSE_LAT, HOUSE_LON],
    tooltip="Ma maison",
    popup="⛪️ Ma maison — Avenue des Collèges 29",
    icon=folium.DivIcon(
        html="""
        <div style="font-size:40px; line-height:40px; transform: translate(-18px, -32px);">
            ⛪️
        </div>
        """
    ),
).add_to(m)
# === fin maison ===

cluster = MarkerCluster().add_to(m)

# ------------------ Pin SVG commun + glyphes ------------------
PIN_SVG_TEMPLATE = """
<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 36 48">
  <ellipse cx="18" cy="46" rx="7" ry="2.5" fill="rgba(0,0,0,0.25)"/>
  <path d="M18 0
           C 8 0, 1 7.5, 1 17
           C 1 26.5, 9 31.5, 13 37.5
           C 15 40.5, 16.5 44, 18 48
           C 19.5 44, 21 40.5, 23 37.5
           C 27 31.5, 35 26.5, 35 17
           C 35 7.5, 28 0, 18 0 Z"
        fill="{FILL}" stroke="rgba(0,0,0,0.35)" stroke-width="1"/>
  <circle cx="18" cy="17" r="9" fill="rgba(255,255,255,0.12)"/>
  {GLYPH}
</svg>
""".strip()

def glyph_tree_white() -> str:
    # Sapin joufflu, compact, avec étage du milieu élargi
    return """
    <!-- étage haut -->
    <polygon points="18,8 12,13 24,13" fill="white"/>
    <!-- étage milieu (plus large) -->
    <polygon points="18,11 11,16.5 25,16.5" fill="white"/>
    <!-- étage bas -->
    <polygon points="18,14 11,21 25,21" fill="white"/>
    <!-- tronc -->
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
    # même taille/ancre pour TOUT : identiques arbre/champignon
    return CustomIcon(icon_image=url, icon_size=(30, 42), icon_anchor=(15, 40))

# --- Helpers pour les marqueurs ---
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

# Ajout des points
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

# ------------------ Légende repliable ------------------
# ------------------ Légende repliable (sans JS) ------------------
def legend_pin_dataurl(name: str) -> str:
    col = colors.get(name, "green")
    if name in MUSHROOM_SET:
        return build_pin_svg(col, glyph_mushroom_white(), w=18, h=24)
    else:
        return build_pin_svg(col, glyph_tree_white(), w=18, h=24)

legend_rows = []
for name in sorted(set(CATALOG)):
    img = legend_pin_dataurl(name)
    legend_rows.append(
        f"""
        <div style="display:flex; align-items:center; gap:8px; margin:4px 0;">
          <img src="{img}" width="16" height="16" />
          <span>{name}</span>
        </div>
        """
    )
legend_body = "".join(legend_rows)

legend_html = f"""
<style>
  #legend-card summary {{
    list-style: none;
    cursor: pointer;
    font-weight: 600;
  }}
  /* masque le chevron par défaut (Chrome/Safari) */
  #legend-card summary::-webkit-details-marker {{ display: none; }}
  /* petit chevron custom */
  #legend-card summary::after {{
    content: "▸";
    margin-left: 8px;
    font-size: 12px;
    opacity: .6;
  }}
  #legend-card details[open] summary::after {{
    content: "▾";
  }}
</style>

<div id="legend-card" style="position: fixed; bottom: 24px; left: 24px; z-index: 9999;">
  <details style="
      background: #fff; border: 1px solid #ccc; border-radius: 10px;
      padding: 8px 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.15);
      max-width: 240px; font-size: 13px;">
    <summary>📖 Légende</summary>
    <div style="margin-top: 8px; max-height: 240px; overflow: auto;">
      {legend_body}
    </div>
  </details>
</div>
"""

m.get_root().html.add_child(folium.Element(legend_html))

# Affichage
st_folium(m, width=900, height=520)

# ------------------ Ajout (menu déroulant) ------------------
# ------------------ Ajouter / Supprimer un point ------------------
st.sidebar.markdown("---")
st.sidebar.subheader("➕/➖ Ajouter ou supprimer un point")

mode = st.sidebar.radio(
    "Choisir mode",
    ["Ajouter", "Supprimer"],
    index=0,
    horizontal=True,
    label_visibility="collapsed"
)


with st.sidebar.form("add_or_delete_form"):
    if mode == "Ajouter":
        new_name = st.selectbox("Catégorie", options=CATALOG, index=0)
        col_a, col_b = st.columns(2)
        with col_a:
            new_lat = st.number_input("Latitude", value=46.519100, format="%.6f")
        with col_b:
            new_lon = st.number_input("Longitude", value=6.633600, format="%.6f")
        new_seasons = st.multiselect("Saison(s)", options=all_seasons, default=["automne"])

        submitted_add = st.form_submit_button("Ajouter & enregistrer")
        if submitted_add:
            st.session_state["trees"].append({
                "name": new_name,
                "lat": float(new_lat),
                "lon": float(new_lon),
                "seasons": new_seasons or [],
            })
            # couleur par défaut si nouvelle catégorie
            if new_name not in colors:
                colors[new_name] = "green"
            save_items(st.session_state["trees"])
            st.success(f"Ajouté : {new_name} ✅ (enregistré)")

    else:  # mode == "Supprimer"
        trees = st.session_state.get("trees", [])
        if not trees:
            st.info("Aucun point à supprimer.")
            # bouton inactif pour garder un seul submit dans le form
            _ = st.form_submit_button("Supprimer définitivement", disabled=True)
        else:
            # Libellés lisibles
            options_labels = []
            for i, t in enumerate(trees):
                seasons_txt = _serialize_seasons(t.get("seasons", [])) or "—"
                options_labels.append(f"{i+1}. {t['name']} – {t['lat']:.5f}, {t['lon']:.5f} [{seasons_txt}]")

            idx_to_label = {i: label for i, label in enumerate(options_labels)}
            idx_choice = st.selectbox(
                "Choisis le point à supprimer",
                options=list(idx_to_label.keys()),
                format_func=lambda i: idx_to_label[i],
            )

            confirm = st.checkbox("Je confirme la suppression", value=False)
            submitted_del = st.form_submit_button("Supprimer définitivement", disabled=not confirm)

            if submitted_del and confirm:
                removed = st.session_state["trees"].pop(idx_choice)
                save_items(st.session_state["trees"])
                st.success(f"Supprimé : {removed['name']} ✅")



# ------------------ Stats & export ------------------
counts = Counter(t["name"] for t in filtered)
total = len(filtered)
st.markdown("**📊 Statistiques (points affichés)**")
if total == 0:
    st.write("Aucun point (vérifie les filtres).")
else:
    st.write(f"Total : **{total}**")
    st.markdown("\n".join(f"- {k} : **{counts[k]}**" for k in sorted(counts)))

st.markdown("---")
df_export = pd.DataFrame([{
    "name": t["name"],
    "lat": t["lat"],
    "lon": t["lon"],
    "seasons": _serialize_seasons(t.get("seasons", [])),
} for t in st.session_state["trees"]])

st.download_button(
    "⬇️ Télécharger tous les points (CSV)",
    data=df_export.to_csv(index=False),
    file_name="arbres_lausanne.csv",
    mime="text/csv",
)
st.caption(f"🌳 Points affichés : {len(filtered)} / {len(st.session_state['trees'])}")
