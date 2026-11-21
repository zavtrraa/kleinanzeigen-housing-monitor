import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EligibilityDecision:
    outcome: str
    reason: str


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    value = value.translate(str.maketrans({"–": "-", "—": "-", "‑": "-", "\u00a0": " "}))
    return re.sub(r"\s+", " ", value).strip()


# Scope negation to the phrase immediately preceding the match, not the whole listing.
NEGATED_PREFIX = re.compile(
    r"\b(?:kein(?:e[rmns]?)?|nicht|ohne)\s+"
    r"(?:(?:eine?|als|nur|reine?|auch|für|an)\s+){0,3}$",
    re.IGNORECASE,
)
AMOUNT = r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)"
WARM_PATTERNS = [
    re.compile(
        r"\b(?:bruttowarmmiete|warmmiete|gesamtmiete|miete\s+warm)\s*"
        r"(?::|=|beträgt|von|ist)?\s*" + AMOUNT + r"\s*(?:€|eur(?:o)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\d.,])\b" + AMOUNT + r"\s*(?:€|eur(?:o)?)\s*(?:pro\s+monat\s+)?"
        r"(?:warm|warmmiete|gesamtmiete)\b",
        re.IGNORECASE,
    ),
]


def warm_amounts(text: str) -> set[Decimal]:
    values = set()
    for pattern in WARM_PATTERNS:
        for match in pattern.finditer(normalize(text)):
            values.add(Decimal(match[1].replace(".", "").replace(",", ".")))
    return values


class ListingFilter:
    """Explainable local heuristics; no inference of people attributes."""

    def __init__(self, config):
        self.rules = [
            (rule.label, [re.compile(pattern, re.IGNORECASE) for pattern in rule.patterns])
            for rule in config.rules
        ]
        self.max_warm_eur = config.max_warm_eur

    def check(self, title: str, description: str, cost_details: str = "") -> EligibilityDecision:
        # Keep segments separate so a negation in a title cannot affect the description.
        for label, patterns in self.rules:
            for segment in (title, description):
                text = normalize(segment)
                for pattern in patterns:
                    for match in pattern.finditer(text):
                        prefix = text[max(0, match.start() - 60) : match.start()]
                        if not NEGATED_PREFIX.search(prefix):
                            return EligibilityDecision("excluded", label)
        if self.max_warm_eur is not None:
            amounts = warm_amounts(description + "\n" + cost_details)
            if any(value > self.max_warm_eur for value in amounts):
                amount = max(amounts)
                return EligibilityDecision(
                    "excluded", f"Warmmiete {amount} € > {self.max_warm_eur} €"
                )
            if len(amounts) != 1:
                return EligibilityDecision("review", "Warmmiete is not specified reliably")
            amount = next(iter(amounts))
            if amount > self.max_warm_eur:
                return EligibilityDecision(
                    "excluded", f"Warmmiete {amount} € > {self.max_warm_eur} €"
                )
            # A labelled total with explicit excluded heating is not a verified warm total.
            text = normalize(description + " " + cost_details)
            if re.search(
                r"\b(?:zzgl\.?|zuzüglich|ohne|exkl\.?|exklusive)\s+"
                r"(?:(?:der|separate[rmn]?)\s+)?(?:heizkosten|heizung)\b",
                text,
                re.I,
            ):
                return EligibilityDecision("review", "Heating is charged separately")
        return EligibilityDecision("allowed", "")
