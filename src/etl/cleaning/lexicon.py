"""Static word lists the name rules read: function words per language,
legal-form tokens, placeholder strings and the notice-language codes
that select a list.

Data only — no logic lives here, so a list can be extended without
touching a rule. Every list is deliberately small: the sentence rule
needs a *ratio* of function words, not a dictionary, and a short list
of unambiguous articles / prepositions / conjunctions is enough to tell
"Gara aggiudicata come da determina n. 543 del 2013 pubblicata sul
sito ..." from "Visualforma - Tecnologias de Informação, S.A.".
"""
from __future__ import annotations

# Function words per language (casefolded). Articles, prepositions,
# conjunctions, the commonest auxiliaries and pronouns. Nothing that is
# also a plausible company-name token ("sa", "da" in PT is a preposition
# but also a legal-form fragment — kept, since the legal-form check
# exempts real names before the ratio is read).
STOPWORDS: dict[str, frozenset[str]] = {
    "it": frozenset("""
        il lo la i gli le un uno una di a da in con su per tra fra e ed o
        ma che non si del della dello dei degli delle al allo alla ai agli
        alle dal dalla dallo dai dagli dalle nel nella nello nei negli nelle
        sul sulla sullo sui sugli sulle come è sono ha hanno stato stata
        essere questo questa quale cui anche ove
    """.split()),
    "pt": frozenset("""
        o a os as um uma uns umas de do da dos das em no na nos nas por
        para com sem e ou que não nao se ao à aos às pelo pela pelos pelas
        como é são sao foi ser este esta isto qual onde
    """.split()),
    "es": frozenset("""
        el la los las un una unos unas de del a al en con por para sin y
        o u que no se como es son fue ser este esta esto cual donde
    """.split()),
    "fr": frozenset("""
        le la les l un une des du de d à au aux en dans par pour sur avec
        sans et ou que qui ne pas ce cette ces est sont été être dont où
    """.split()),
    "de": frozenset("""
        der die das des dem den ein eine einer eines einem einen und oder
        von vom zu zum zur im in am an auf für mit ohne bei nach über
        unter ist sind wurde wird nicht gemäß laut durch
    """.split()),
    "en": frozenset("""
        the a an of in on at to for with by from and or not is are was
        were be been this that these those as it its per under
    """.split()),
    "nl": frozenset("""
        de het een van in op te voor met door en of aan bij is zijn werd
        wordt niet dat die deze dit volgens
    """.split()),
    "pl": frozenset("""
        i w z na do o po od za przez dla nie jest są oraz lub a to ten ta
        te tego tej tym się we ze
    """.split()),
    "ro": frozenset("""
        și si sau de la în in cu pe pentru din prin fără fara nu este sunt
        a al ai ale un o unui unei ce care conform
    """.split()),
}

# eForms BT-702 three-letter codes and legacy TED two-letter LG codes,
# both as the parser hands them over verbatim, to a STOPWORDS key. A
# notice in any other language falls back to the union of all lists.
NOTICE_LANGUAGE_TO_LIST: dict[str, str] = {
    "ITA": "it", "IT": "it",
    "POR": "pt", "PT": "pt",
    "SPA": "es", "ES": "es",
    "FRA": "fr", "FR": "fr",
    "DEU": "de", "DE": "de",
    "ENG": "en", "EN": "en",
    "NLD": "nl", "NL": "nl",
    "POL": "pl", "PL": "pl",
    "RON": "ro", "RO": "ro",
}

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
