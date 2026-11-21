import unittest
from pathlib import Path

from kleinanzeigen_bot.action.text_variator import TextVariator
from kleinanzeigen_bot.core.eligibility import ListingFilter
from kleinanzeigen_bot.core.models import Listing
from kleinanzeigen_bot.settings import Exclusions, load_yaml

ROOT = Path(__file__).resolve().parents[2]


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.config = load_yaml(ROOT / "config/exclusions.yaml", Exclusions)
        self.gate = ListingFilter(self.config)

    def decision(self, text, title="Wohnung"):
        return self.gate.check(title, text)

    def test_applications_closed(self):
        for title in ("*ANFRAGESTOPP* Helle Wohnung", "Anfragestop", "Bewerbungsstopp"):
            with self.subTest(title=title):
                self.assertEqual(self.decision("Warmmiete: 500 €", title).outcome, "excluded")
        self.assertEqual(self.decision("Kein Anfragestopp. Warmmiete: 500 €").outcome, "allowed")

    def test_excludes_explicit_exchange(self):
        for text in (
            "Tauschwohnung",
            "Nur im Tausch",
            "Wohnungstausch",
            "Tausche meine Wohnung",
            "Nur Tausch",
            "Nur als Tausch",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.decision(text).outcome, "excluded")

    def test_negated_exchange_is_allowed(self):
        for text in (
            "Keine Tauschwohnung.",
            "Kein Wohnungstausch.",
            "Nicht zum Tausch.",
            "Keine reine Tauschwohnung.",
        ):
            with self.subTest(text=text):
                result = self.decision(text + " Warmmiete: 500 €")
                self.assertEqual(result.outcome, "allowed")

    def test_excludes_women_only_and_shared_housing(self):
        for text in (
            "Nur an Frauen.",
            "Nur für eine Frau.",
            "Nur weibliche Mieterinnen.",
            "Frauen-WG",
            "Weibliche Nachmieterin gesucht.",
            "Keine Männer.",
            "WG-Zimmer frei.",
            "Zimmer in einer WG",
            "Wohngemeinschaft",
            "Mitbewohner gesucht",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.decision(text).outcome, "excluded")

    def test_ordinary_mentions_of_women_do_not_exclude(self):
        for text in (
            "Meine Frau organisiert die Besichtigung.",
            "Ansprechpartnerin ist Frau Müller.",
            "Nicht nur Frauen dürfen sich bewerben.",
            "Die Wohnung ist WG-geeignet.",
            "Keine Wohngemeinschaft.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.decision(text + " Warmmiete: 520 €").outcome, "allowed")

    def test_over_budget_is_excluded(self):
        self.assertEqual(self.decision("Warmmiete: 520,01 €").outcome, "excluded")
        self.assertEqual(self.decision("Gesamtmiete 1.050 €").outcome, "excluded")
        self.assertEqual(self.decision("Warmmiete 500 €. Gesamtmiete 550 €.").outcome, "excluded")

    def test_exact_budget_and_repeated_same_total_are_allowed(self):
        self.assertEqual(self.decision("Warmmiete: 520 €. Also 520 € warm.").outcome, "allowed")

    def test_ambiguous_missing_or_extra_heating_goes_to_review(self):
        for text in (
            "Kaltmiete 450 €.",
            "Miete 500 €.",
            "Warmmiete ca. 500 €.",
            "Warmmiete 500 € zzgl. Heizkosten.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.decision(text).outcome, "review")

    def test_unsupported_decimal_format_cannot_be_read_as_tail_amount(self):
        for text in ("520.50 € warm", "600.99 € warm", "1,200.50 € warm"):
            with self.subTest(text=text):
                self.assertEqual(self.decision(text).outcome, "review")

    def test_cost_table_can_supply_total(self):
        result = self.gate.check("Wohnung", "Schöne Wohnung.", "Warmmiete\n510 €")
        self.assertEqual(result.outcome, "allowed")

    def test_title_exchange_not_cancelled_by_description_negation(self):
        result = self.gate.check("Tauschwohnung", "Keine Haustiere. Warmmiete 450 €")
        self.assertEqual(result.outcome, "excluded")

    def test_literal_message_preserved_exactly(self):
        template = "Guten Tag,\n\nIs the apartment still available?\n\nRegards"
        text = TextVariator(template, ["Hallo"], ["Grüße"], "").render(Listing("1", "url", "A"))
        self.assertEqual(text, template.strip())

    def test_explicit_room_in_shared_flat_remains_excluded(self):
        for title in (
            "22 m² Zimmer in Wohnung mit eigenem Balkon",
            "Einzelzimmer",
            "Zimmer in einer Wohnung",
        ):
            with self.subTest(title=title):
                self.assertEqual(self.decision("Warmmiete unbekannt", title).outcome, "excluded")
