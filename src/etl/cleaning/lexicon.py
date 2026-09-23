"""The phrases the name rules look for, and the legal-form tokens that
vouch for a real name.

Data only — no logic lives here, so a family can be extended without
touching a rule. Every phrase is one a buyer really published in the
supplier-name field, with the country and the prod match count beside
it: these are individual clerks deciding to write an instruction where
a company belongs, so they are enumerated by name rather than detected
by shape.
"""
from __future__ import annotations

# ── Named junk families ─────────────────────────────────────────────
#
# Not a heuristic. Each phrase below is one a buyer really typed into
# the supplier-name field, found by probing the prod name index on
# 2026-09-23; the count and countries are what it matched there. The
# generic "looks like a sentence" test these replace withheld 5.2% of
# all names longer than 80 characters — UK trusts ("SETTLEMENT FOR THE
# BENEFIT OF THE CHILDREN OF ..."), Italian pension funds, French
# associations — because a long name full of function words is what a
# real institution is called, not what junk looks like.

# "the names are somewhere else, go and look": the buyer points at an
# annex, a list, a spreadsheet or a web page instead of naming anyone.
SEE_THE_ANNEX_PHRASES: tuple[str, ...] = (
    "see attached",          # 7, IRL/GBR — "See attached excel file"
    "see annex",
    "see the attached",
    "voir annexe",           # 2, FRA
    "voir liste",            # 20, FRA/BEL — "Lot 2 - voir liste section VI"
    "voir le bloc",          # FRA — "voir le bloc 6 'Autres informations'"
    "zie bijlage",           # 40, NLD — the largest single family
    "se bilaga",             # 2, SWE
    "se bilag",              # DNK/NOR
    "ver anexo",             # 10, ESP — "Ver anexo publicado en el perfil"
    "vedi allegato",         # 3, ITA
    "siehe anlage",          # DEU/AUT
    "siehe anhang",
    "patrz zalacznik",       # POL
    "patrz załącznik",
    "viz priloha",           # CZE/SVK
    "viz příloha",
    "katso liite",           # FIN
    "lasd melleklet",        # HUN
    "lásd melléklet",
)

# "there were several winners and I am not listing them": the buyer
# names the plurality instead of the operators.
SEVERAL_OPERATORS_PHRASES: tuple[str, ...] = (
    "various suppliers",         # 4, GBR
    "multiple suppliers",        # 13, GBR/IRL — "Lot 1 - Multiple Suppliers"
    "several suppliers",
    "multiple awards",           # IRL
    "plusieurs attributaires",   # 5, BEL/FRA
    "mehrere auftragnehmer",     # 1, DEU
    "meerdere ondernemingen",    # NLD — "Meerdere ondernemingen, zie bijlage A"
    "diverse leveranciers",      # 1, NLD
    "vari operatori",            # 2, ITA
    "diversi operatori",         # 1, ITA
    "varie ditte",               # ITA — "Varie ditte (vedi allegato A.2)"
    "rozni wykonawcy",           # POL
    "różni wykonawcy",
    "ruzni dodavatele",          # CZE
    "flera leverantorer",        # SWE
)

# "this field does not apply to me": a refusal, sometimes with the
# statute the buyer is refusing under.
NOT_APPLICABLE_PHRASES: tuple[str, ...] = (
    "not applicable",        # 18, GBR/NLD/SRB/BEL/MKD
    "niet van toepassing",   # 8, NLD
    "nie dotyczy",           # 3, POL
    "nu este cazul",         # ROU
    "keine angabe",          # 26, AUT/DEU — "Keine Angabe aus Gründen des
                             #   Wettbewerbs", "... gemäß § 61 Abs. 4 BVergG"
    "no procede",            # ESP
    "sans objet",            # FRA
    "non comunicato",        # ITA
)

# Values that mean "no supplier named" rather than a name. Compared after
# trim + casefold, exactly — a placeholder with extra decoration ("N/A.")
# is left to the other rules rather than fuzzily matched here.
PLACEHOLDER_NAMES: frozenset[str] = frozenset({
    "n/a", "na", "não aplicável", "nao aplicavel", "vários", "varios",
    "diversi", "-", ".", "1", "0", "x", "xxx",
})

# Legal-form tokens that mark a real company name. Compared on tokens
# with dots/slashes removed and casefolded, so "S.p.A.", "SpA" and
# "S.P.A." are one token "spa". These are long or distinctive enough to
# match anywhere in the name.
LEGAL_FORM_TOKENS: frozenset[str] = frozenset("""
    spa srl srls snc sas sasu sarl sàrl eurl scarl scrl sca
    gmbh mbh kgaa ohg ggmbh
    lda ltda unipessoal
    slu sau sll sccl
    ltd limited llp llc plc inc corp incorporated
    vof bvba sprl cvba
    zrt nyrt kft kkt
    oyj aps asa
    uab sia
    eood ood ead
    sro doo dooel
    ike epe ape
""".split())

# Two-letter legal forms are ambiguous as bare words ("SE" is a French
# and Spanish function word, "AS" an English one). They count only when
# written with dots or a slash ("S.A.", "N.V.", "A/S") anywhere in the
# name, or as the LAST token of the name in upper case ("Vattenfall AB").
LEGAL_FORM_SHORT_TOKENS: frozenset[str] = frozenset("""
    sa ag ab as se nv bv cv kg ks hb kb ad dd kd sl sc ae oe ee oy bt
    ug ev eg ou oü
""".split())

# Multi-token forms, matched as phrases over the normalised token
# sequence (dots removed, casefolded, single-spaced).
LEGAL_FORM_PHRASES: tuple[str, ...] = (
    r"\bsp z o ?o\b",        # Sp. z o.o.
    r"\bspol s r ?o\b",      # spol. s r.o.
    r"\bs[àa] r ?l\b",       # S.à r.l.
    r"\bsp j\b",             # Sp. j.
    r"\bsp k\b",             # Sp. k.
    r"\bco kg\b",            # GmbH & Co. KG
)
