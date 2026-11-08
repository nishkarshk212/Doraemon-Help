"""
Multilingual text moderation detector.

Uses a layered approach:
  1. Normalization & Hinglish phonetic expansion
  2. Regex/keyword lexicon matching per category
  3. Pattern-based confidence scoring with severity mapping

Each category detector returns a CategoryScore.  The detector is fully
offline — no external API required — but can be extended by plugging in
a transformer-based classifier.
"""
from __future__ import annotations
import re
from typing import List

from moderation.nsfw_engine.schemas import CategoryScore, SeverityLevel
from moderation.nsfw_engine.normalizer import normalize_text, hinglish_normalize, detect_language_hint
from moderation.nsfw_engine.logger import get_logger

logger = get_logger("text_detector")

# ─── Lexicon definitions ──────────────────────────────────────────────────────

# Each entry: (regex_pattern, base_confidence, severity)
# Patterns are matched against the normalized+hinglish-expanded text.

_CAT_PATTERNS: dict[str, list[tuple[str, float, SeverityLevel]]] = {

    "sexual_nsfw": [
        (r"\bporn(?:ography|o)?\b", 0.92, SeverityLevel.HIGH),
        (r"\bxxx\b", 0.90, SeverityLevel.HIGH),
        (r"\bnude[sd]?\b", 0.80, SeverityLevel.MEDIUM),
        (r"\bnudity\b", 0.80, SeverityLevel.MEDIUM),
        (r"\bsexual(?:ly)?\b", 0.72, SeverityLevel.MEDIUM),
        (r"\bsex\s*(?:tape|video|clip|chat)\b", 0.88, SeverityLevel.HIGH),
        (r"\bnaked\b", 0.75, SeverityLevel.MEDIUM),
        (r"\bboobs?\b|\btits?\b", 0.78, SeverityLevel.MEDIUM),
        (r"\bbreasts?\b.{0,30}(?:nude|naked|expos|bare|fondl|touch|grope)", 0.78, SeverityLevel.MEDIUM),
        (r"\bexpos(?:e|ed|ing)\b.{0,30}\bbreasts?\b", 0.78, SeverityLevel.MEDIUM),
        (r"\bd[i1]ck\b|\bcock\b|\bpenis\b", 0.80, SeverityLevel.MEDIUM),
        (r"\bpussy\b|\bvagina\b", 0.80, SeverityLevel.MEDIUM),
        (r"\bfuck(?:ing|er|s)?\b", 0.70, SeverityLevel.MEDIUM),
        (r"\bslut\b|\bwhore\b|\bho\b", 0.82, SeverityLevel.HIGH),
        (r"\bescort\b|\bprostitut", 0.85, SeverityLevel.HIGH),
        (r"\bonlyfans?\b", 0.78, SeverityLevel.MEDIUM),
        (r"\bsend\s*(?:nudes?|pics?)\b", 0.88, SeverityLevel.HIGH),
        # Hinglish canonical tokens (avoid \b with underscore words — use lookarounds)
        (r"(?<![a-z])(?:chutiya|chut|randi|landu?|gandu|bhen_chod|mader_chod|maaki|bhosdike?|bosadike?)(?![a-z])", 0.88, SeverityLevel.HIGH),
    ],

    "violence_gore": [
        (r"\bbeheading\b", 0.95, SeverityLevel.CRITICAL),
        (r"\btorture\b", 0.88, SeverityLevel.HIGH),
        (r"\bmutilat", 0.90, SeverityLevel.CRITICAL),
        (r"\bgore\b", 0.85, SeverityLevel.HIGH),
        (r"\bbloodsh[ae]d\b", 0.88, SeverityLevel.HIGH),
        (r"\bkill(?:ing|ed|s)?\s+(?:him|her|them|you|people)\b", 0.80, SeverityLevel.HIGH),
        (r"\bstab(?:bing|bed)?\b", 0.78, SeverityLevel.HIGH),
        (r"\bbeat\s+(?:him|her|them|you)\s+(?:up|to\s*death)\b", 0.78, SeverityLevel.MEDIUM),
        (r"\b(?:graphic|brutal)\s+(?:violence|fight|attack)\b", 0.82, SeverityLevel.HIGH),
        (r"\bcorpse\b|\bdead\s*body\b", 0.72, SeverityLevel.MEDIUM),
        (r"\bI\s+(?:will|gonna|going\s+to)\s+(?:kill|hurt|harm|destroy)\s+(?:you|him|her|them)\b", 0.85, SeverityLevel.HIGH),
        (r"(?<![a-z])mar_ja(?![a-z])", 0.82, SeverityLevel.HIGH),
    ],

    "weapons": [
        # ── Specific firearm model names ─────────────────────────────────────
        (r"\b(?:ak[-\s]?47|ak47|akat|ak[-\s]?74|ak74|m16|m4|ar[-\s]?15|ar15|glock|uzi|beretta|colt|mp5|fn\s*fal|desert\s*eagle)\b", 0.90, SeverityLevel.HIGH),
        # ── Generic firearm types (standalone) ──────────────────────────────
        (r"\b(?:guns?|firearms?|weapons?|pistols?|rifles?|shotguns?|revolvers?|handguns?|carbines?)\b", 0.82, SeverityLevel.HIGH),
        # ── Weapon + sale/buy context ────────────────────────────────────────
        (r"\b(?:ak|akat|ak47|ak74|m16|ar15|glock|uzi|gun|rifle|pistol|shotgun|revolver|firearm|weapon)\s*(?:for\s*sale|to\s*buy|available|deliver|supply)\b", 0.95, SeverityLevel.CRITICAL),
        (r"\bfor\s*sale\b.{0,40}(?:ak|akat|gun|rifle|pistol|weapon|ammo|bullet|katta|bandook|tamancha)", 0.92, SeverityLevel.HIGH),
        # ── Ammunition ───────────────────────────────────────────────────────
        (r"\b(?:ammunition|bullets?|ammo|cartridges?|magazine|silencer|suppressor)\b", 0.80, SeverityLevel.HIGH),
        # ── Explosives ───────────────────────────────────────────────────────
        (r"\b(?:explosives?|bombs?|bomb\s*making|grenades?|rpg|rocket\s*launcher|dynamite|c4|landmine|ied)\b", 0.95, SeverityLevel.CRITICAL),
        # ── Bladed weapons ───────────────────────────────────────────────────
        (r"\b(?:knives|knife|daggers?|swords?|machetes?|switchblade|katana|bowie)\b", 0.72, SeverityLevel.MEDIUM),
        # ── Illegal weapon context ───────────────────────────────────────────
        (r"\billegal\s*(?:weapons?|guns?|firearms?)\b", 0.92, SeverityLevel.HIGH),
        # ── Hindi/Hinglish romanized weapon words ─────────────────────────────
        (r"\b(?:bandook|bandooq|tamancha|katta|desi\s*katta|aslaah|hathiyar|chaku|talwar|goliyan|goli|tota)\b", 0.85, SeverityLevel.HIGH),
        # ── Hindi Devanagari weapon words ─────────────────────────────────────
        (r"(?:बंदूक|तमंचा|कट्टा|हथियार|पिस्तौल|राइफल|गोली|गोलियां|चाकू|तलवार|गन|वेपन|असलाह)", 0.88, SeverityLevel.HIGH),
    ],

    "drugs_alcohol": [
        # ── Standalone 'drug' or 'drugs' ─────────────────────────────────────
        (r"\bdrugs?\b", 0.82, SeverityLevel.HIGH),
        (r"\bnarcotics?\b|\bsubstance\s*abuse\b", 0.85, SeverityLevel.HIGH),
        # ── Hard drugs ───────────────────────────────────────────────────────
        (r"\b(?:cocaine|coke|crack|smack|heroin|meth|methamphetamine|crystal\s*meth|mdma|ecstasy|lsd|acid|mushrooms?|shrooms?|fentanyl|opium|morphine|ketamine|pcp|dmt)\b", 0.92, SeverityLevel.CRITICAL),
        # ── Cannabis family ───────────────────────────────────────────────────
        (r"\b(?:weed|marijuana|cannabis|ganja|bhang|charas|hash|hashish|kush|dope|joint|pot)\b", 0.85, SeverityLevel.HIGH),
        # ── Prescription drug abuse ───────────────────────────────────────────
        (r"\b(?:xanax|valium|adderall|percocet|oxycodone|oxy|codeine|lean|hydrocodone|tramadol)\b", 0.88, SeverityLevel.HIGH),
        (r"\b(?:prescription|rx)\s*(?:pills?|meds?)\s*(?:sell|buy|for\s*sale|without\s*prescription)\b", 0.90, SeverityLevel.HIGH),
        # ── Alcohol products, brands and bottles ──────────────────────────────
        (r"\b(?:corona|budweiser|heineken|kingfisher|carlsberg|tuborg|bira\s*91|bira|bacardi|smirnoff|absolut|jack\s*daniel'?s?|johnnie\s*walker|old\s*monk|blenders?\s*pride|royal\s*challenge|royal\s*stag|mcdowell'?s?|officers?\s*choice|black\s*dog|chivas\s*regal|chivas|glenfiddich|grey\s*goose|jameson|baileys|jagermeister|captain\s*morgan|tanqueray|bombay\s*sapphire|magic\s*moments|white\s*mischief|breezer|smirnoff\s*ice|guinness|stella\s*artois|asahi|peroni)\b", 0.90, SeverityLevel.HIGH),
        (r"\b(?:beer|whiskey|whisky|vodka|rum|tequila|gin|brandy|champagne|liquor|bourbon|scotch|booze)\s*(?:bottle|can|glass|brand|drink|shot|peg|pint|bar|pack|case|crate)\b", 0.90, SeverityLevel.HIGH),
        (r"\b(?:bottle|can|pint|peg|shot|crate|pack)\s*of\s*(?:beer|whiskey|whisky|vodka|rum|tequila|gin|brandy|wine|champagne|alcohol|liquor)\b", 0.90, SeverityLevel.HIGH),
        (r"\b(?:alcoholic\s*(?:drink|beverage|product)s?)\b", 0.90, SeverityLevel.HIGH),
        # ── Alcohol sale / supply context ─────────────────────────────────────
        (r"\b(?:alcohol|liquor|booze|vodka|whiskey|whisky|rum|tequila|gin|brandy|beer|wine)\s*(?:for\s*sale|to\s*buy|deliver|available|order|supply)\b", 0.90, SeverityLevel.HIGH),
        (r"\b(?:buy|sell|deliver|supply|order)\s*(?:alcohol|liquor|booze|vodka|whiskey|whisky|rum|tequila|gin|brandy|beer|wine)\b", 0.88, SeverityLevel.HIGH),
        (r"\b(?:underage|minor)\s*(?:drinking|alcohol)\b|\bdrinking\s*(?:under\s*age|and\s*driving)\b", 0.82, SeverityLevel.HIGH),
        (r"\bdrunk\s*(?:drive|driving|driver)\b|\bdrink\s*drive\b", 0.80, SeverityLevel.MEDIUM),
        (r"\balcohol(?:ic)?\s*(?:addict|abuse|dependency|problem)\b", 0.78, SeverityLevel.MEDIUM),
        # ── Drug sale/supply context ──────────────────────────────────────────
        (r"\b(?:buy|sell|order|deliver|supply)\s*(?:drugs?|weed|marijuana|cannabis|ganja|bhang|charas|hash|cocaine|heroin|meth)\b", 0.95, SeverityLevel.CRITICAL),
        (r"\b(?:drug)\s*(?:dealer|trafficking|peddling|cartel|lord|kingpin)\b", 0.92, SeverityLevel.CRITICAL),
        (r"\bwhatsapp\s*(?:me|dm|msg)\s*(?:for|to\s*buy|order)\b.{0,40}(?:drug|weed|pills?|medicine)\b", 0.88, SeverityLevel.HIGH),
        (r"\boverdose\b", 0.78, SeverityLevel.HIGH),
        # ── Hindi/Hinglish romanized drug words ───────────────────────────────
        (r"\b(?:nasha|nashe|chitta|afeem|afim|charas|ganja|bhang|maal|daru|daaru|sharab|shraab|mahua|toddy|desi\s*daru|angrezi\s*daru)\b", 0.85, SeverityLevel.HIGH),
        # ── Hindi Devanagari drug words ───────────────────────────────────────
        (r"(?:ड्रग्स|ड्रग|चरस|गांजा|शराब|दारू|अफीम|चिट्टा|नशा|कोकीन|हेरोइन|भांग|नशीला)", 0.88, SeverityLevel.HIGH),
        # ── Drug smoke / consume context ──────────────────────────────────────
        (r"\bdrug_smoke\b|\bsmok[ei]ng\s*(?:ganja|weed|charas)\b", 0.78, SeverityLevel.MEDIUM),
    ],

    "self_harm": [
        (r"\bsuicide\b", 0.88, SeverityLevel.HIGH),
        (r"(?<![a-z])mar_ja(?![a-z])|\bkill\s+yourself\b|\bkys\b", 0.92, SeverityLevel.CRITICAL),
        (r"\bself[_\s-]?harm\b|\bself[_\s-]?injur", 0.88, SeverityLevel.HIGH),
        (r"\bcut(?:ting)?\s+(?:myself|yourself|wrists?)\b", 0.85, SeverityLevel.HIGH),
        (r"(?<![a-z])self_harm(?![a-z])", 0.88, SeverityLevel.HIGH),
        (r"\bI\s+want\s+to\s+die\b|\bI\s+don[''']?t\s+want\s+to\s+live\b", 0.78, SeverityLevel.MEDIUM),
        (r"\bkill\s+myself\b|kill\s+myself\b", 0.92, SeverityLevel.CRITICAL),  # catches contracted forms
        (r"\bI\s+(?:will|gonna|going\s+to)\s+(?:kill|end)\s+myself\b", 0.92, SeverityLevel.CRITICAL),
        (r"\bkha\s*le\s*zeher\b|\bzeher\s*kha", 0.90, SeverityLevel.CRITICAL),
        (r"\bhow\s+to\s+(?:commit\s+)?suicide\b|\bsuicide\s+(?:method|guide|tip|how)\b", 0.95, SeverityLevel.CRITICAL),
    ],

    "hate_extremism": [
        # Generic hate speech / slurs — handle asterisk-masked versions (normalizer removes *)
        (r"\bn[i1*]?gg[ae]r[sz]?\b|\bngg[ae]r[sz]?\b", 0.95, SeverityLevel.CRITICAL),
        (r"\bchink\b|\bspic\b|\bkike\b|\btowelhead\b|\bcamel\s*jockey\b", 0.92, SeverityLevel.HIGH),
        (r"\b(?:all|these|those)\s+(?:muslims?|hindus?|christians?|jews?|sikhs?)\s+(?:should|must|deserve\s+to)\s+(?:die|be\s+killed|suffer)\b", 0.95, SeverityLevel.CRITICAL),
        (r"\b(?:terrorists?|jihad[is]?)\s+(?:attack|bomb|kill)\b", 0.90, SeverityLevel.HIGH),
        (r"\bISIS\b|\bISIL\b|\bal[-\s]?qaeda\b|\btaliban\b.{0,30}\b(?:join|support|follow|recruit)\b", 0.92, SeverityLevel.CRITICAL),
        (r"\b(?:ethnic|religious)\s+(?:cleansing|genocide|purge)\b", 0.95, SeverityLevel.CRITICAL),
        (r"\bwhite\s+(?:power|supremacy|supremacist)\b|\bn[a4][z2]i\b", 0.92, SeverityLevel.CRITICAL),
        (r"\b(?:death|burn)\s+(?:to\s+)?(?:jews?|muslims?|hindus?|christians?|infidels?)\b", 0.95, SeverityLevel.CRITICAL),
        (r"\b(?:gas|exterminate|eliminate)\s+the\s+(?:jews?|muslims?|blacks?|whites?|hindus?)\b", 0.97, SeverityLevel.CRITICAL),
    ],

    "harassment_abuse": [
        (r"\bI[''']?ll\s+(?:find|locate|track)\s+you\b|\bI\s+know\s+where\s+you\s+live\b", 0.88, SeverityLevel.HIGH),
        (r"\bI[''']?ll\s+(?:hurt|destroy|ruin|rape)\s+you\b|\bI[''']?m\s+going\s+to\s+(?:hurt|ruin)\s+you\b", 0.90, SeverityLevel.HIGH),
        (r"\bfuck\s+(?:you|off|your)\b", 0.70, SeverityLevel.MEDIUM),
        (r"\bkill\s+yourself\b|\bkys\b", 0.92, SeverityLevel.CRITICAL),
        (r"\bstop\s+existing\b|\bno\s+one\s+(?:likes|loves|wants)\s+you\b", 0.80, SeverityLevel.MEDIUM),
        (r"\bI\s+will\s+(?:dox|expose|leak)\s+(?:you|your)\b", 0.88, SeverityLevel.HIGH),
        (r"\bhate\s+(?:you|him|her|them)\b", 0.60, SeverityLevel.LOW),
        (r"\bdelete\s+(?:yourself|your\s+account)\b", 0.65, SeverityLevel.LOW),
        (r"\b(?:loser|pathetic|worthless|trash|garbage|idiot|moron|retard)\b", 0.60, SeverityLevel.LOW),
        # Hinglish harassment (use word-like boundaries for canonicals with underscores or variants)
        (r"(?<![a-z])(?:mad[ae]r_?chod|bhosdike?|bh[ae]n_?chod|chutiya|gandu|bkl|maaki)(?![a-z])", 0.82, SeverityLevel.HIGH),
        (r"\bmad[ae]r\b.{0,5}\bchod\b", 0.82, SeverityLevel.HIGH),  # split variant
    ],
}


def _score_category(category: str, patterns: list, normalized_text: str, threshold: float) -> CategoryScore:
    best_conf = 0.0
    best_sev = SeverityLevel.NONE
    reasons = []

    for pat, base_conf, sev in patterns:
        matches = re.findall(pat, normalized_text, re.IGNORECASE)
        if matches:
            # Boost confidence if multiple distinct matches
            boost = min(0.08 * (len(matches) - 1), 0.20)
            confidence = min(base_conf + boost, 1.0)
            if confidence > best_conf:
                best_conf = confidence
                best_sev = sev
            reasons.append(f"matched '{pat[:40]}' ({len(matches)}x)")

    triggered = best_conf >= threshold
    return CategoryScore(
        category=category,
        triggered=triggered,
        confidence=round(best_conf, 4),
        severity=best_sev if triggered else SeverityLevel.NONE,
        reason="; ".join(reasons[:3]) if triggered else "",
    )


def analyze_text(
    text: str,
    enabled_categories: dict[str, bool],
    threshold: float = 0.65,
) -> tuple[list[CategoryScore], str]:
    """
    Analyze a text string against all enabled categories.

    Returns:
        (list[CategoryScore], detected_language)
    """
    # Detect language early (before normalization removes hints)
    language = detect_language_hint(text)

    # Full normalization + Hinglish phonetic expansion
    normalized = hinglish_normalize(text)

    scores: list[CategoryScore] = []
    for category, patterns in _CAT_PATTERNS.items():
        enabled = enabled_categories.get(category, True)
        if not enabled:
            scores.append(CategoryScore(
                category=category,
                triggered=False,
                confidence=0.0,
                severity=SeverityLevel.NONE,
                reason="category disabled",
            ))
            continue
        score = _score_category(category, patterns, normalized, threshold)
        scores.append(score)

    return scores, language
