"""
Text normalizer: cleans leetspeak, Unicode obfuscation, zero-width chars,
and performs Hinglish phonetic normalization before detection.
"""
import re
import unicodedata


# ── Leetspeak substitution map ──────────────────────────────────────────────
LEET_MAP = {
    "0": "o", "1": "i", "3": "e", "4": "a",
    "5": "s", "7": "t", "8": "b", "@": "a",
    "$": "s", "!": "i", "+": "t", "|": "i",
}

# ── Hinglish phonetic normalization: common romanized slur variants ──────────
# Maps normalized forms -> canonical bad-word root for matching
HINGLISH_PHONETIC_MAP = {
    # Sexual slurs
    r"ch[ou]?[dt][iy][ay]?": "chutiya",
    r"ch[ou]t": "chut",
    r"m[ao][dn]+": "madn",
    r"rand[iy]?": "randi",
    r"r[ae]ndi": "randi",
    r"b[aho]+s[dr][iy]?k[eai]?": "bosadike",
    r"g[ao]nd[uU]?": "gandu",
    r"l[aeo]+[nd]": "land",
    r"l[ao][dn][dD][uU]?": "landu",
    r"maa\s*k[iy]?\s*[aeiou]*": "maaki",
    r"bh[ao][sod]+d": "bhosdike",
    r"bh[ae]nchod": "bhen_chod",
    r"b[ae]nchod": "bhen_chod",
    r"b[aei]ncho[dt]": "bhen_chod",
    r"m[ao]d[ae]r[ck][h]?[ou]d": "mader_chod",
    r"mc\b": "madar_chod",
    r"bkl\b": "bhosdika_lauda",
    r"b[ck]l\b": "bhosdika_lauda",
    # Self-harm / suicide
    r"mar\s*ja": "mar_ja",
    r"maro?\s*khud\s*ko": "self_harm",
    r"sui[cs][ei]?d[ae]?": "suicide",
    r"kha\s*le\s*zeher": "self_harm",
    # Drugs — common Hindi/Hinglish variants
    r"bh[au][kn][jg][ae]?": "bhang",
    r"g[ao][lnd][ij][ay]?": "ganja",
    r"sm[aeo]{1,2}k\s*[ck][ae]r": "drug_smoke",
    r"n[e3][e3]dl[e3]": "needle",
    r"char[sa][ie]?": "charas",
    r"chi[t]+[a4]": "chitta",
    r"af[ei]+m": "afeem",
    r"na[s5]h[ae]": "nasha",
    r"m[ao]{1,2}[l1][^a-z]": "maal",
    r"d[a4]r[uo]{1,2}": "daru",
    r"sh[a4]r[a4]b": "sharab",
    # Weapons — Hindi/Hinglish romanized
    r"b[a4]nd[o0]{1,2}[qk]": "bandook",
    r"[t7]am[a4]nch[a4]": "tamancha",
    r"k[a4][t7]{1,2}[a4]": "katta",
    r"h[a4][t7]h[iy][ya4]r": "hathiyar",
    r"[a4]sl[a4][a4]h": "aslaah",
    r"ch[a4]k[o0]?[uo]?": "chaku",
    r"[t7][a4]lw[a4]r": "talwar",
}

# ── Zero-width and invisible character pattern ───────────────────────────────
ZERO_WIDTH_PATTERN = re.compile(
    r"[\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e\u2028\u2029]+"
)

# ── Repeated character collapser (e.g. "sluuut" -> "slut") ──────────────────
REPEAT_COLLAPSE = re.compile(r"(.)\1{2,}")


def normalize_unicode(text: str) -> str:
    """Decompose unicode and keep ASCII-compatible characters."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii", "ignore")


def remove_zero_width(text: str) -> str:
    return ZERO_WIDTH_PATTERN.sub("", text)


def apply_leet(text: str) -> str:
    """Replace common leet characters."""
    result = []
    for ch in text:
        result.append(LEET_MAP.get(ch, ch))
    return "".join(result)


def collapse_repeats(text: str) -> str:
    """Collapse 3+ repeated chars to 2 (e.g. 'fuckkkk' -> 'fuckk')."""
    return REPEAT_COLLAPSE.sub(r"\1\1", text)


def normalize_text(text: str) -> str:
    """Full normalization pipeline for a text string."""
    text = remove_zero_width(text)
    text = text.lower().strip()
    text = apply_leet(text)
    text = collapse_repeats(text)
    # Remove common separator tricks: ch*t, ch.t, ch-t
    text = re.sub(r"[*.\-_|\\/'\"]{1,3}", "", text)
    return text


def hinglish_normalize(text: str) -> str:
    """
    Apply phonetic substitution for Hinglish slurs, returning a canonical
    space-separated string of detected normalized tokens for the detector.
    """
    normalized = normalize_text(text)
    tokens = []
    for pattern, canonical in HINGLISH_PHONETIC_MAP.items():
        if re.search(pattern, normalized, re.IGNORECASE):
            tokens.append(canonical)
    return normalized + " " + " ".join(tokens) if tokens else normalized


def detect_language_hint(text: str) -> str:
    """
    Very fast language hint based on character set analysis.
    Returns: 'en', 'hi', 'hinglish', 'ar', 'ru', 'mixed', or 'unknown'.
    """
    arabic = len(re.findall(r"[\u0600-\u06FF]", text))
    cyrillic = len(re.findall(r"[\u0400-\u04FF]", text))
    devanagari = len(re.findall(r"[\u0900-\u097F]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))

    if devanagari > 5:
        return "hi"
    if arabic > 5:
        return "ar"
    if cyrillic > 5:
        return "ru"
    if latin > 3 and devanagari > 0:
        return "hinglish"

    # Heuristic: check for common Hinglish words in Latin script
    hinglish_markers = re.findall(
        r"\b(bhai|yaar|kya|nahi|hai|hain|tha|mera|teri|aur|koi|kuch|abhi|agar|kaisa|kyun|mujhe|tumhe|ladki|ladka|dost|sach|jhooth)\b",
        text.lower()
    )
    if len(hinglish_markers) >= 2:
        return "hinglish"
    if latin > 3:
        return "en"
    return "unknown"
