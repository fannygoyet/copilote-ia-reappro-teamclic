"""
POC — Copilote IA de réapprovisionnement TeamClic
Classification ABC/XYZ + stock de sécurité dynamique + ROI de portage, depuis les exports Odoo.
Fanny GOYET — Devoir final LSI3 "IA & Supply Chain".

Corrections post-vérification :
- XYZ calculé sur les 9 MOIS PLEINS (sept 2025 → mai 2026) ; août-25 et juin-26 exclus car partiels
  (ils gonflaient artificiellement la variabilité).
- Stock de sécurité calculé UNIQUEMENT pour les références stockées (classes A et B).
  Les classes C — et a fortiori CZ — relèvent de la gestion à la demande (pas de réappro auto).
- Références sans coût isolées dans un tableau "à fiabiliser" (ne faussent plus le classement).
Entrées : Catalogue_pieces.xlsx, Mouvements_pieces.xlsx (exports Odoo, non fournis — confidentiels)
Sorties : abc_xyz_resultats.csv, a_fiabiliser.csv, rapport_reappro_IA.xlsx
"""
import pandas as pd, numpy as np, re

# Périmètre PIÈCES de réparation (export Odoo filtré) — périmètre curé et fiabilisé du PFE.
cat = pd.read_excel("Catalogue_pieces.xlsx")
m   = pd.read_excel("Mouvements_pieces.xlsx")

def code(s):
    mm = re.match(r'\s*\[([^\]]+)\]', str(s));  return mm.group(1) if mm else None
clean = lambda s: re.sub(r'\s*\[[^\]]+\]\s*', '', str(s)).strip()
m['code'] = m['Produit'].apply(code); m['nom'] = m['Produit'].apply(clean)
cat['nom'] = cat['Nom'].astype(str).str.strip()
cat['cout_u'] = np.where(cat['Coût'].fillna(0) > 0, cat['Coût'], cat['Prix de vente'].fillna(0))
c2 = cat.dropna(subset=['Référence interne']).copy(); c2['ref'] = c2['Référence interne'].astype(str)
cost_code = dict(zip(c2['ref'], c2['cout_u'])); cost_name = cat.groupby('nom')['cout_u'].max().to_dict()
catname   = cat.groupby('nom')['Catégorie de produits'].first().to_dict()
qty_stock = cat.groupby('nom')['Quantité en stock'].sum().to_dict()
def cout(r):
    if r['code'] and str(r['code']) in cost_code and cost_code[str(r['code'])] > 0:
        return cost_code[str(r['code'])]
    return cost_name.get(r['nom'], 0)

# --- Consommation = sorties vers clients, statut Fait ---
s = m[(m['Emplacement de destination'] == "Partners/Customers") & (m['Statut'] == "Fait")].copy()
s['cout_u'] = s.apply(cout, axis=1); s['mois'] = s['Date'].dt.to_period('M').astype(str)

# --- ABC sur la valeur consommée (tout le périmètre) ---
g = s.groupby('nom').agg(qte=('Quantité', 'sum'), nb=('Quantité', 'size'),
                         cout_u=('cout_u', 'max')).reset_index()
g['cout_manquant'] = g['cout_u'] == 0
g['valeur_conso']  = g['qte'] * g['cout_u']
g = g.sort_values('valeur_conso', ascending=False).reset_index(drop=True)
tot = g['valeur_conso'].sum(); g['cum_pct'] = g['valeur_conso'].cumsum() / tot * 100
g['ABC'] = g['cum_pct'].apply(lambda p: 'A' if p <= 70 else ('B' if p <= 90 else 'C'))  # seuils Camelot/Lemaire

# --- XYZ sur les 9 mois PLEINS uniquement ---
MOIS_PLEINS = [f"2025-{mm:02d}" for mm in range(9, 13)] + [f"2026-{mm:02d}" for mm in range(1, 6)]
sp  = s[s['mois'].isin(MOIS_PLEINS)]
piv = sp.pivot_table(index='nom', columns='mois', values='Quantité', aggfunc='sum', fill_value=0)
piv = piv.reindex(columns=MOIS_PLEINS, fill_value=0)
cov = lambda r: (r.values.std(ddof=0) / r.values.mean()) if r.values.mean() > 0 else np.nan
g['CoV'] = g['nom'].map(piv.apply(cov, axis=1))
g['XYZ'] = g['CoV'].apply(lambda c: 'Z' if pd.isna(c) else ('X' if c <= 0.5 else ('Y' if c <= 1 else 'Z')))
g['classe'] = g['ABC'] + g['XYZ']
g['categorie'] = g['nom'].map(catname)
g['conso_mens_moy'] = g['nom'].map(piv.mean(axis=1)).round(2)
g['sigma_mens']     = g['nom'].map(piv.std(ddof=0, axis=1)).round(2)

# --- Stock de sécurité : UNIQUEMENT pour les références stockées (A et B) ---
k = {'X': 1.28, 'Y': 1.65, 'Z': 2.33}; LT = 7   # table Mercier ; délai fournisseur 7 j (HYPOTHÈSE à confirmer)
def ss(r):
    if r['ABC'] in ('A', 'B') and pd.notna(r['sigma_mens']):
        return float(np.ceil(k[r['XYZ']] * r['sigma_mens'] * np.sqrt(LT / 30)))
    return 0.0   # classe C / CZ : gestion à la demande, pas de stock de sécurité automatique
g['SS_reco'] = g.apply(ss, axis=1)
g['point_commande'] = np.where(g['ABC'].isin(['A', 'B']),
                               np.ceil(g['conso_mens_moy'] * (LT / 30) + g['SS_reco']), 0)
g['mode_gestion'] = np.where(g['ABC'].isin(['A', 'B']), 'Réappro piloté (SS dynamique)',
                             'Gestion à la demande (pas de réappro auto)')

# --- ROI : coût de portage évité en sortant les CZ du réappro auto ---
g['stock_actuel']   = g['nom'].map(qty_stock).fillna(0)
g['valeur_stock']   = g['stock_actuel'] * g['cout_u']
cz = g[g['classe'] == 'CZ']
portage = 0.20  # coût total annuel d'un stock > 20% de sa valeur (Mercier)
roi_cz = cz['valeur_stock'].sum() * portage

# --- Sorties fichiers ---
afiab = g[g['cout_manquant']][['nom', 'qte', 'categorie']].copy()
afiab.to_csv("a_fiabiliser.csv", index=False)
g.drop(columns=['stock_actuel']).to_csv("abc_xyz_resultats.csv", index=False)

# --- Rapport Excel pour la direction (3 onglets, plus lisible qu'un .md) ---
reco = g[g['ABC'].isin(['A', 'B'])].sort_values(['ABC', 'valeur_conso'], ascending=[True, False])
reco_out = reco[['nom', 'classe', 'conso_mens_moy', 'sigma_mens', 'SS_reco',
                 'point_commande', 'mode_gestion']].rename(columns={
    'nom': 'Référence', 'classe': 'Classe', 'conso_mens_moy': 'Conso moy/mois',
    'sigma_mens': 'Écart-type mensuel', 'SS_reco': 'Stock de sécurité',
    'point_commande': 'Point de commande', 'mode_gestion': 'Mode de gestion'})
synth = pd.DataFrame({'Indicateur': [
    'Références consommées', 'Unités sorties (clients)', 'Valeur consommée (€)',
    'Classe A — nb de références', 'Classe A — part de la valeur',
    'Références régulières (classe X)', 'Références CZ (à sortir du réappro auto)',
    'Références sans coût (à fiabiliser)', 'Stock CZ immobilisé (€)',
    'Coût de portage évité (€/an)'],
    'Valeur': [g['nom'].nunique(), int(s['Quantité'].sum()), round(tot),
               int((g.ABC == 'A').sum()), f"{g[g.ABC=='A'].valeur_conso.sum()/tot*100:.1f} %",
               int((g.XYZ == 'X').sum()), int((g.classe == 'CZ').sum()),
               int(g.cout_manquant.sum()), round(cz['valeur_stock'].sum()), round(roi_cz)]})
afiab_out = afiab.rename(columns={'nom': 'Référence', 'qte': 'Qté consommée', 'categorie': 'Catégorie'})
with pd.ExcelWriter("rapport_reappro_IA.xlsx", engine="openpyxl") as xw:
    synth.to_excel(xw, sheet_name="Synthèse", index=False)
    reco_out.to_excel(xw, sheet_name="Recommandations A-B", index=False)
    afiab_out.to_excel(xw, sheet_name="À fiabiliser", index=False)
    for ws in xw.book.worksheets:                       # entêtes en gras + largeur auto
        for cell in ws[1]:
            cell.font = cell.font.copy(bold=True)
        for col in ws.columns:
            longueur = max((len(str(c.value)) for c in col if c.value is not None), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(longueur + 2, 55)

print(f"Sorties clients: {len(s)} | unités: {int(s['Quantité'].sum())} | réf: {g['nom'].nunique()} "
      f"| XYZ sur {len(MOIS_PLEINS)} mois pleins")
print("ABC :", {c: int((g.ABC == c).sum()) for c in 'ABC'},
      f"| valeur A = {g[g.ABC=='A'].valeur_conso.sum()/tot*100:.1f}%")
print("Matrice ABC/XYZ :")
print(pd.crosstab(g['ABC'], g['XYZ']).reindex(index=['A','B','C'], columns=['X','Y','Z'], fill_value=0))
print(f"Réf classées X (régulières) : {int((g.XYZ=='X').sum())}")
print(f"Réf CZ : {int((g.classe=='CZ').sum())} | réf sans coût (à fiabiliser) : {int(g.cout_manquant.sum())}")
print(f"Valeur consommée totale : {tot:,.0f} € | Stock CZ immobilisé : {cz['valeur_stock'].sum():,.0f} € "
      f"-> coût de portage évité ≈ {roi_cz:,.0f} €/an".replace(',', ' '))
