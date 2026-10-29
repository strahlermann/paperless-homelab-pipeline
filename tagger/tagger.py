#!/usr/bin/env python3
"""Paperless-ngx Inbox tagger using an LLM (local Ollama or any OpenAI-compatible API).

A run has two steps:
1. Split: multi-page Inbox scans that contain several separate documents are split
   with Paperless' own split operation. The original is deleted (to the trash) only
   after all parts have been consumed, which a later run checks.
2. Tag: documents tagged "Inbox" are sent to the LLM for metadata
   (structured JSON output), and the results are PATCHed back.
Owners and permissions are never touched; a Paperless workflow handles sharing.
Configuration: .env (PAPERLESS_URL, PAPERLESS_TOKEN) and household.json, both kept out of git.
"""

import argparse
import collections
import datetime as dt
import io
import json
import logging
import logging.handlers
import math
import os
import re
import sys
import time

import requests
from pypdf import PdfReader

ENV_FILE = os.environ.get("TAGGER_ENV_FILE", "/etc/paperless-tagger.env")
LOG_FILE = os.environ.get("TAGGER_LOG_FILE", "/var/log/paperless-tagger.log")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = "qwen3:30b-a3b-instruct-2507-q4_K_M"
# LLM backend, set from the command line in main():
#   ollama  native Ollama API (/api/chat) at --llm-url; the production default
#   openai  any OpenAI-compatible /chat/completions endpoint at --llm-url (OpenRouter, Ollama's /v1),
#           API key from the environment variable named by --api-key-env
LLM = {"backend": "ollama", "url": OLLAMA_URL, "api_key": None, "extra": {}}
# Per-call usage (tokens, seconds, cost if the endpoint reports it); written to TAGGER_LLM_LOG.
LLM_USAGE = []
NUM_CTX = 16384

INBOX_TAG = "Inbox"
REVIEW_TAG = "Prüfen"
CONFIDENCE_THRESHOLD = 0.8
MAIL_TAG = "mail"
# Set on documents with an open deadline (custom field "Frist" today or later). A Paperless
# workflow sends an ntfy reminder 3 days before. A deadline alone no longer means "Prüfen".
FRIST_TAG = "Frist"
# Workflow tags are managed by this script or by Paperless mail rules, never chosen by the model.
WORKFLOW_TAGS = {INBOX_TAG.lower(), REVIEW_TAG.lower(), MAIL_TAG, FRIST_TAG.lower()}
# Who lives here comes from household.json next to this script (TAGGER_HOUSEHOLD_FILE overrides);
# household.example.json shows the format. It stays out of git, like .env.
HOUSEHOLD_FILE = os.environ.get("TAGGER_HOUSEHOLD_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "household.json")
HOUSEHOLD_EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "household.example.json")
_household_path = HOUSEHOLD_FILE if os.path.exists(HOUSEHOLD_FILE) else HOUSEHOLD_EXAMPLE
with open(_household_path, encoding="utf-8") as _f:
    _HH = json.load(_f)
# Members of the household. They are the recipient of nearly every document (and the sender of
# our own letters), so they are never a correspondent: never offered, accepted or suggested, and
# a household correspondent that Paperless' auto-matching set on consumption gets replaced.
# A name counts as household if it contains one of the surnames, or consists of first names only.
HOUSEHOLD = list(_HH["household"])
# Unknown correspondents are created automatically when the model is at least this sure, else
# only proposed in the note (+ "Prüfen"). None turns auto-creation off. In the benchmark every
# suggested sender at 0.7 or more was the right one (22 of 22); below that it is proposed only.
AUTO_CREATE_CORRESPONDENT_MIN_CONFIDENCE = 0.7

# One-line definition per topic tag; the model may only pick a tag whose definition fits the main
# subject of the document. Tags without a definition here are not offered to the model.
TAG_DEFINITIONS = {
    "Beruf": "Arbeit und Job: Arbeitsvertrag, Gehaltsabrechnung, Arbeitgeber, Bewerbung, Arbeitsagentur, Weiterbildung.",
    "Fahrzeug": "Auto, Motorrad oder Fahrrad: Kauf, Werkstatt, TÜV, Ersatzteile, Kfz-Versicherung, Kfz-Steuer, Leasing, Ladekarte fürs E-Auto.",
    "Finanzen": "Bank und Geldanlage: Girokonto, Kontoauszug, Kreditkarte, Depot, Kredit, Sparen. Nicht jede Rechnung.",
    "Gesundheit": "Arzt, Zahnarzt, Krankenhaus, Apotheke, Therapie, Befunde, Krankenkasse.",
    "Mobilfunk & Internet": "Handy- oder Festnetzvertrag, Internetanschluss, Mobilfunkanbieter.",
    "Reise": "Urlaub und Reisen: Flug, Bahnreise, Hotel, Ferienwohnung, Mietwagen, Reiseversicherung.",
    "Schule": "Schule, Hort, Kita oder Kindergarten: Anmeldung, Zeugnisse, Elternbriefe, Betreuungsgebühren.",
    "Steuer": "Finanzamt, Steuererklärung, Steuerbescheid, Lohnsteuerbescheinigung, Spendenquittung.",
    "Versicherung": "Versicherungen (Haftpflicht, Hausrat, Kfz, Leben, Rechtsschutz, Reise, Kranken): Police, Beitrag, Schadensfall.",
    "Wohnen": "Unser Zuhause: Miete, Nebenkosten, Strom, Gas, Wasser, Hausverwaltung, Handwerker, Wohnungssuche, Umzug.",
    "Genossenschaft": "Unsere Mitgliedschaft in einer Genossenschaft (eG), z. B. Wohnungsgenossenschaft: Beitrittserklärung, Geschäftsanteile, Einzahlung, Dividende, Mitglieder- oder Generalversammlung, Kündigung der Mitgliedschaft. Nicht normale Rechnungen oder Preisänderungen einer eG (z. B. Strom).",
}
MAX_TOPIC_TAGS = 2
# A tag the model picks is only kept if one of these word stems occurs in the text. Stems match
# at a word start ("schul" fits "Schulwechsel"); words found on nearly every invoice (IBAN, Bank,
# Umsatzsteuer, Internet address) are left out on purpose. Tags without keywords: no check.
TAG_KEYWORDS = {
    "Beruf": ["arbeitgeber", "arbeitnehmer", "arbeitsvertrag", "gehalt", "lohn", "entgeltabrechnung",
              "bewerbung", "arbeitsagentur", "agentur für arbeit", "weiterbildung", "personalabteilung"],
    "Fahrzeug": ["kfz", "pkw", "fahrzeug", "autohaus", "e-auto", "motorrad", "fahrrad", "e-bike", "werkstatt",
                 "tüv", "hauptuntersuchung", "kennzeichen", "ladekarte", "wallbox", "reifen", "leasing"],
    "Finanzen": ["girokonto", "konto", "kontoauszug", "depot", "kredit", "darlehen", "kreditkarte",
                 "sparkonto", "tagesgeld", "wertpapier", "bausparen", "geldanlage"],
    "Gesundheit": ["arzt", "ärzt", "zahnarzt", "zahnärzt", "praxis", "klinik", "krankenhaus", "apotheke",
                   "therapie", "behandlung", "patient", "krankenkasse", "krankenversicherung", "befund"],
    "Mobilfunk & Internet": ["mobilfunk", "handy", "smartphone", "sim-karte", "simkarte", "dsl", "glasfaser",
                             "router", "festnetz", "rufnummer", "datenvolumen", "internetanschluss"],
    "Reise": ["reise", "flug", "hotel", "urlaub", "ferienwohnung", "mietwagen", "unterkunft", "check-in"],
    "Schule": ["schul", "hort", "kita", "kindertages", "kindergarten", "krippe", "klasse", "lehrer",
               "zeugnis", "ferienbetreuung"],
    "Steuer": ["finanzamt", "lohnsteuer", "einkommensteuer", "steuererklärung", "steuerbescheid",
               "steuer-id", "steueridentifikationsnummer", "spendenquittung", "zuwendungsbestätigung"],
    "Versicherung": ["versicher", "police", "krankenkasse"],
    "Wohnen": ["miete", "mieter", "vermieter", "nebenkosten", "strom", "gas", "wasser", "heizung",
               "hausverwaltung", "wohnung", "handwerker", "umzug", "lieferstelle", "zähler"],
    "Genossenschaft": ["genossenschaft", "geschäftsanteil", "genossenschaftsanteil", "beitritt", "mitgliedschaft",
                       "dividende", "generalversammlung", "mitgliederversammlung", "vertreterversammlung"],
}

# Documents with less OCR text than this are not sent to the model.
MIN_TEXT_CHARS = 20
# Word 5-gram Jaccard similarity above which two documents count as possible duplicates.
# Identical rescans score ~1.0; the same form letter for two different children ~0.8.
DUPLICATE_SIMILARITY = 0.9

# Learning from the archive: the model sees how the most similar documents we already filed
# (no Inbox, no Prüfen tag, so a human is done with them) were filed, the way Paperless' own
# classifier learns from them. Similarity = TF-IDF cosine over the words of the OCR text.
EXAMPLES_MAX = 3
EXAMPLES_MIN_SIMILARITY = 0.3   # below this the nearest document usually has another sender
EXAMPLE_SNIPPET_CHARS = 250
# A correspondent or tag that our filing backs counts as evidenced even if its name or keyword is
# not in the text (e.g. a renamed correspondent): carried by one example from this similarity on,
# or by two examples from LEARNED_MIN_SIMILARITY on.
LEARNED_STRONG_SIMILARITY = 0.8
LEARNED_MIN_SIMILARITY = 0.5

# Family tags, shared with others by a Paperless workflow. They come from dedicated answer
# fields with their own rules, never from the free tag list. They must exist; never created.
CHILD_TAG = "Kinder"
CHILDREN = tuple(_HH.get("children") or ())  # first names; each one is also a tag
# Lease documents get the housing topic tag. Since 2026-10-25 "Wohnung" is merged into "Wohnen"
# and the whole topic is shared with the partner, so it stays a normal topic in the free tag list too.
LEASE_TAG = "Wohnen"
# Documents addressed to the partner get PARTNER_TAG; a Paperless workflow makes the partner's
# user their owner. No "partner" entry in household.json: no such tag.
_PARTNER = _HH.get("partner") or {}
PARTNER_TAG = _PARTNER.get("tag")
PARTNER_NAME = (_PARTNER["first_name"], _PARTNER["last_name"]) if PARTNER_TAG else ()
SHARED_TAGS = ({CHILD_TAG.casefold()} | ({PARTNER_TAG.casefold()} if PARTNER_TAG else set())
               | {c.casefold() for c in CHILDREN})
BACKFILL_PLAN = os.environ.get("TAGGER_BACKFILL_PLAN", "/var/lib/tagger/backfill-shared-plan.json")

# Split step. The prompt holds the text of every page, so it gets a larger context.
SPLIT_CONFIDENCE = 0.85
SPLIT_NUM_CTX = 32768
SPLIT_PAGE_CHARS = 1200    # per page; letterheads are at the top, "Seite x von y" often at the bottom
SPLIT_TOTAL_CHARS = 48000  # shrinks the per-page budget for long scans
# A requested split is not waited for: Paperless on the Pi often needs 20-45 min to consume the
# parts. The request goes into PENDING_SPLITS_FILE and every later run checks it; the original is
# moved to the trash only once all its parts exist, and flagged "Prüfen" if they are still
# missing after SPLIT_PENDING_MAX_S.
SPLIT_PENDING_MAX_S = 24 * 3600
PENDING_SPLITS_FILE = os.environ.get("TAGGER_PENDING_FILE") or os.path.join(
    os.path.dirname(LOG_FILE), "pending-splits.json")
SPLIT_TITLE_RE = re.compile(r"\(split \d+\)$")
# Long scans are checked in overlapping windows, so every page keeps a useful amount of text.
# A scan longer than LONG_SCAN_PAGES never passes as "one document" silently: it gets "Prüfen".
SPLIT_WINDOW_PAGES = 20
LONG_SCAN_PAGES = 20

# OCR text limits (characters). Invoices often carry the totals and due dates
# at the end, so long documents keep both the beginning and the end.
MAX_CHARS = 12000
HEAD_CHARS = 9000
TAIL_CHARS = 3000

CF_BETRAG = "Betrag"
CF_FRIST = "Frist"
CF_RECHNUNGSNUMMER = "Rechnungsnummer"
NOTE_PREFIX = "[paperless-tagger]"

log = logging.getLogger("paperless-tagger")


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            env[key] = value.strip().strip("'\"")
    for key in ("PAPERLESS_URL", "PAPERLESS_TOKEN"):
        if not env.get(key):
            raise SystemExit(f"{key} missing in {path}")
    return env


def setup_logging(verbose):
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        log.addHandler(fh)
    except OSError as e:
        print(f"WARNING: cannot write {LOG_FILE}: {e}", file=sys.stderr)
    # Progress goes to stderr; stdout is reserved for proposals and the summary.
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(sh)


class Paperless:
    def __init__(self, url, token):
        self.base = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Token {token}",
            "Accept": "application/json; version=9",
        })

    def _req(self, method, url, raw=False, **kw):
        if not url.startswith("http"):
            url = f"{self.base}/api/{url.lstrip('/')}"
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=120 if raw else 60, **kw)
                if r.status_code < 500:
                    if not r.ok:
                        raise RuntimeError(f"{method} {url}: HTTP {r.status_code}: {r.text[:500]}")
                    if raw:
                        return r.content
                    return r.json() if r.content else None
                err = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                err = str(e)
            log.warning("%s %s failed (%s), retry %d/3", method, url, err, attempt + 1)
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"{method} {url} failed after retries: {err}")

    def get_all(self, endpoint, params=None):
        params = dict(params or {}, page_size=100)
        data = self._req("GET", endpoint, params=params)
        results = list(data["results"])
        while data.get("next"):
            data = self._req("GET", data["next"])
            results.extend(data["results"])
        return results

    def get(self, endpoint, params=None):
        return self._req("GET", endpoint, params=params)

    def patch(self, endpoint, payload):
        return self._req("PATCH", endpoint, json=payload)

    def post(self, endpoint, payload):
        return self._req("POST", endpoint, json=payload)

    def delete(self, endpoint):
        return self._req("DELETE", endpoint)

    def download(self, doc_id):
        # The archived (OCRed) PDF if there is one, else the original.
        return self._req("GET", f"documents/{doc_id}/download/", raw=True)


def by_name(items):
    return {i["name"].casefold(): i for i in items}


def truncate(text):
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= MAX_CHARS:
        return text, False
    return (text[:HEAD_CHARS] + "\n\n[... gekürzt ...]\n\n" + text[-TAIL_CHARS:]), True


def enum_or_empty(names):
    # The empty string means "nothing fits"; the model then fills the suggestion field.
    return {"type": "string", "enum": [""] + sorted(set(names))}


def build_schema(correspondents, doc_types, tags):
    # One yes/no per tag, after a one-sentence main topic: a free "pick up to N" list made the
    # model fill every slot (e.g. "Fahrzeug" on a child's bank account).
    themen = {"type": "object", "properties": {t: {"type": "boolean"} for t in sorted(set(tags))},
              "required": sorted(set(tags))}
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "absender": {"type": "string"},
            "von_uns": {"type": "boolean"},
            "gegenpartei": {"type": "string"},
            "correspondent": enum_or_empty(correspondents),
            "correspondent_vorschlag": {"type": "string"},
            "document_type": enum_or_empty(doc_types),
            "document_type_vorschlag": {"type": "string"},
            "hauptthema": {"type": "string"},
            "themen": themen,
            "tags_vorschlag": {"type": "array", "items": {"type": "string"}},
            "created": {"type": "string"},
            "betrag": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "waehrung": {"type": "string"},
            "frist": {"type": "string"},
            "rechnungsnummer": {"type": "string"},
            **SHARED_PROPERTIES,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["title", "absender", "von_uns", "gegenpartei", "correspondent",
                     "correspondent_vorschlag", "document_type",
                     "document_type_vorschlag", "hauptthema", "themen", "tags_vorschlag", "created", "betrag",
                     "waehrung", "frist", "rechnungsnummer", *SHARED_PROPERTIES, "confidence"],
    }


SHARED_PROPERTIES = {
    "kinder_bezug": {"type": "boolean"},
    "kinder": {"type": "array", "items": {"type": "string", "enum": list(CHILDREN)}},
    "mietvertrag": {"type": "boolean"},
    **({"an_partner": {"type": "boolean"}} if PARTNER_TAG else {}),
}

SHARED_RULES = f"""- kinder_bezug: true, wenn das Dokument eines unserer Kinder betrifft, z. B. Schule, Hort, Kita, Kinderarzt, Impfungen, Kindergeld, Versicherung oder Konto eines Kindes, Vereine, Ausweisdokumente eines Kindes. Sonst false.
- kinder: welche Kinder betroffen sind ({", ".join(CHILDREN)}), NUR wenn das Kind im Dokument namentlich genannt oder eindeutig erkennbar ist. Sonst leere Liste – nicht raten.
- mietvertrag: true NUR für den Mietvertrag unserer Wohnung und Dokumente, die ihn ändern oder ergänzen: Zusatzvereinbarungen, Nachträge, Mieterhöhungen, Staffel- oder Indexmiet-Anpassungen, Untermieterlaubnis, Übergabeprotokolle der Wohnung. Alles andere rund um die Wohnung (Nebenkostenabrechnung, Stadtwerke/Strom/Gas, Handwerker, Schreiben der Hausverwaltung, Wohnungssuche) ist false und bekommt stattdessen den Tag "Wohnen". Mietverträge für Autos, Büros, Geräte usw. sind ebenfalls false.""" + (f"""
- an_partner: true, wenn das Dokument an {" ".join(PARTNER_NAME)} gerichtet ist: Sie steht im Anschriftenfeld (allein oder zusammen mit anderen) oder wird als Empfängerin direkt angesprochen. False, wenn sie nur erwähnt wird, z. B. als Mutter, Erziehungsberechtigte oder Vertragspartnerin in einem Schreiben an jemand anderen, und bei allem, was wir selbst geschrieben haben.""" if PARTNER_TAG else "")

SHARED_SCHEMA = {
    "type": "object",
    "properties": {**SHARED_PROPERTIES, "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
    "required": [*SHARED_PROPERTIES, "confidence"],
}

SHARED_SYSTEM_PROMPT = f"""Du bist ein sorgfältiger Assistent für die Ablage von Dokumenten in Paperless-ngx.
Du erhältst den OCR-Text eines Dokuments und beantwortest nur wenige Fragen dazu. Antworte ausschließlich mit JSON gemäß dem vorgegebenen Schema.

Regeln:
{SHARED_RULES}
- confidence: deine Sicherheit zwischen 0 und 1, dass die Angaben korrekt sind. Sei ehrlich.
Erfinde nichts."""


SYSTEM_PROMPT = f"""Du bist ein sorgfältiger Assistent für die Ablage von Dokumenten in Paperless-ngx.
Du erhältst den OCR-Text eines Dokuments und extrahierst Metadaten. Antworte ausschließlich mit JSON gemäß dem vorgegebenen Schema.

Regeln:
- title: kurzer, aussagekräftiger deutscher Titel (max. ca. 80 Zeichen), z. B. "Rechnung Onlineshop Bestellung 12345" oder "Kündigungsbestätigung Fitnessstudio". Keine Präfixe wie "AW:" oder "WG:".
- Unser Haushalt (siehe Liste in der Anfrage) steht meist als EMPFÄNGER im Adressfeld. Ein Mitglied unseres Haushalts ist NIEMALS Absender-Firma, gegenpartei oder Korrespondent.
- absender: wer das Schreiben verfasst hat, genau so, wie es im Dokument steht. Nimm ihn aus dem Briefkopf bzw. Logo, aus der kleinen Absenderzeile über dem Adressfeld (z. B. "Firma · Straße · PLZ Ort"), aus Grußformel und Unterschrift oder aus der Fußzeile (Impressum, "eine Marke der ..."). NICHT aus dem Adressfeld: dort steht der Empfänger. Unklar: "".
- von_uns: true, wenn ein Mitglied unseres Haushalts das Schreiben verfasst hat (sein Name steht in Briefkopf oder Absenderzeile oder es unterschreibt), z. B. unser Brief an eine Behörde. Sonst false.
- gegenpartei: die Firma, Behörde oder Person auf der anderen Seite. Bei Post an uns ist das der absender, bei von_uns = true der Empfänger aus dem Adressfeld (z. B. die Behörde, an die wir schreiben). Nur der Name (bei Behörden ggf. mit Abteilung, bei Privatpersonen die vollen Namen), OHNE Straße, PLZ oder Ort. Unklar: "".
- correspondent: Wähle einen Namen aus der Liste der vorhandenen Korrespondenten NUR, wenn er eindeutig dieselbe Firma bzw. Person ist wie gegenpartei. Ähnliche Branche, dieselbe Stadt oder ein Name, der nur nebenbei im Text vorkommt (z. B. "Deutsche Post" im Frankiervermerk), reicht NICHT. Wähle nie den "ähnlichsten" Namen. Im Zweifel setze "" – ein falscher Korrespondent ist schlimmer als keiner. Bei "" trage gegenpartei in correspondent_vorschlag ein (sonst "").
- document_type: Dokumenttyp. Wähle NUR aus der Liste der vorhandenen Dokumenttypen. Passt keiner, setze "" und trage in document_type_vorschlag einen Vorschlag ein (z. B. "Rechnung", "Bestellbestätigung", "Vertrag", "Mahnung"), sonst "".
- hauptthema: ein kurzer Satz, worum es im Kern geht (z. B. "Kündigung des Handyvertrags zum Jahresende").
- themen: für jeden Tag aus "Vorhandene Tags" true NUR, wenn seine Definition auf das hauptthema zutrifft; sonst false. Meist trifft genau ein Tag zu, manchmal keiner, selten zwei. Ein Begriff, der nur nebenbei vorkommt, reicht nicht: Ein Schulwechsel, der in einem Brief über eine Wohnung erwähnt wird, ist kein Grund für "Schule"; ein Bankschreiben ist nicht "Wohnen", nur weil unsere Adresse darauf steht; ein Girokonto für ein Kind ist weder "Schule" noch "Fahrzeug". Weitere sinnvolle Tags, die es noch nicht gibt, gehören in tags_vorschlag.
- created: Datum des Schreibens (Briefdatum) im Format JJJJ-MM-TT. Bevorzugt: eine Zeile "Ort, den TT.MM.JJJJ" bzw. "Ort, TT.MM.JJJJ", ein Datum bei "Datum" oder "Rechnungsdatum", sonst das Datum im Briefkopf über dem Betreff. NIEMALS: Geburtsdatum, Zeitraum oder Abrechnungszeitraum, Frist oder Fälligkeit, Leistungs- oder Behandlungsdaten, das Datum eines früheren Schreibens ("Ihr Schreiben vom", "Bescheid vom"), Scan- oder Druckdaten aus dem Dateinamen, das heutige Datum. Hat die Texterkennung die Jahreszahl abgeschnitten (z. B. "05.10.2"), ergänze das plausible Jahr. Kein Briefdatum erkennbar: "".
- betrag: Gesamt- bzw. Rechnungsbetrag (brutto) als Zahl mit Punkt als Dezimaltrennzeichen, z. B. 49.90. Kein Betrag: null. waehrung: ISO-Code, z. B. "EUR" ("" wenn kein Betrag).
- frist: Datum (JJJJ-MM-TT), bis zu dem der Empfänger handeln muss: Zahlungsziel, Kündigungs-, Widerspruchs-, Einspruchs- oder Antwortfrist. Ist die Frist relativ angegeben (z. B. "innerhalb von 14 Tagen ab Rechnungsdatum"), berechne das Datum ausschließlich ab dem im Dokument genannten Datum (Rechnungs-/Ausstellungsdatum), NIEMALS ab dem heutigen Datum. Gibt es kein solches Datum im Dokument, setze "". Bereits bezahlte Rechnungen, reine Bestell-/Versandbestätigungen, Rücksendescheine ohne konkretes Datum und allgemeine Widerrufsbelehrungen (z. B. "Sie haben das Recht, binnen vierzehn Tagen ... zu widerrufen") haben KEINE Frist. Keine Frist: "".
- rechnungsnummer: NUR eine Nummer, die ausdrücklich als Rechnungsnummer bezeichnet ist (z. B. "Rechnungsnummer", "Rechnungs-Nr.", "Rechnung Nr.", "Abrechnungsnummer", "Invoice No."). KEINE Aktenzeichen, Geschäftszeichen ("Unser Zeichen"), Kunden-, Vertrags-, Bestell-, Versicherten- oder Kassenzeichen-Nummern. Sonst "".
{SHARED_RULES}
- Vorschläge der Paperless-eigenen Zuordnung (falls aufgeführt): ein zweiter Hinweis aus unserer Ablage. Übernimm sie, wenn sie zum Dokument passen; sie können sich irren.
- Ähnliche abgelegte Dokumente (falls in der Anfrage aufgeführt): So haben wir vergleichbare Dokumente selbst abgelegt und korrigiert. Stammt das neue Dokument erkennbar vom selben Absender bzw. ist es dieselbe Art Schreiben, übernimm deren Korrespondent, Dokumenttyp und Themen-Tags: unsere Ablage hat Vorrang vor deiner eigenen Einschätzung, auch wenn der Korrespondent dort anders geschrieben ist als im Text. Ist es ein anderer Absender oder eine andere Art Schreiben (z. B. nur dasselbe Musterformular), ignoriere die Beispiele. Titel, Datum, Betrag, Frist und Rechnungsnummer kommen immer nur aus dem neuen Dokument.
- confidence: deine Gesamtsicherheit zwischen 0 und 1, dass alle Angaben korrekt sind. Sei ehrlich: unklarer, unvollständiger oder schlecht lesbarer Text bedeutet eine niedrige Sicherheit.
Erfinde nichts. Was nicht im Text steht, bleibt leer."""


def build_user_prompt(doc, text, truncated, correspondents, doc_types, tags, today, examples=""):
    def lst(names):
        return "\n".join(f"- {n}" for n in sorted(names)) or "(keine vorhanden)"

    tag_lines = "\n".join(f"- {n}: {TAG_DEFINITIONS[n]}" for n in sorted(tags)) or "(keine vorhanden)"
    return f"""Heutiges Datum: {today.isoformat()}

Unser Haushalt (Empfänger, niemals Korrespondent): {", ".join(HOUSEHOLD)}

Vorhandene Korrespondenten:
{lst(correspondents)}

Vorhandene Dokumenttypen:
{lst(doc_types)}

Vorhandene Tags (Name: Definition):
{tag_lines}

{examples}Bisheriger Titel: {doc.get('title') or ''}
Originaldateiname: {doc.get('original_file_name') or ''}
{"Hinweis: Der Text ist lang und wurde in der Mitte gekürzt." if truncated else ""}

OCR-Text des Dokuments:
<<<
{text}
>>>"""


def build_shared_prompt(doc, text, truncated):
    return f"""Titel: {doc.get('title') or ''}
Originaldateiname: {doc.get('original_file_name') or ''}
{"Hinweis: Der Text ist lang und wurde in der Mitte gekürzt." if truncated else ""}

OCR-Text des Dokuments:
<<<
{text}
>>>"""


def ask_llm(model, schema, system, user, num_ctx=NUM_CTX, followup=()):
    """`followup`: further chat messages, e.g. the model's invalid answer and a correction."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}, *followup]
    t0 = time.time()
    if LLM["backend"] == "openai":
        result, stats = _ask_openai(model, schema, messages)
    else:
        result, stats = _ask_ollama(model, schema, messages, num_ctx)
    stats["wall_s"] = time.time() - t0
    LLM_USAGE.append({"model": model, **stats})
    if os.environ.get("TAGGER_LLM_LOG"):
        with open(os.environ["TAGGER_LLM_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"),
                                "backend": LLM["backend"], "model": model, **stats}) + "\n")
    return result, stats


def _ask_ollama(model, schema, messages, num_ctx):
    payload = {
        "model": model,
        "messages": messages,
        "format": schema,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }
    r = requests.post(f"{LLM['url']}/api/chat", json=payload, timeout=900)
    r.raise_for_status()
    data = r.json()
    stats = {
        "prompt_tokens": data.get("prompt_eval_count", 0),
        "prompt_s": data.get("prompt_eval_duration", 0) / 1e9,
        "gen_tokens": data.get("eval_count", 0),
        "gen_s": data.get("eval_duration", 0) / 1e9,
    }
    return json.loads(data["message"]["content"]), stats


def strict_schema(schema):
    """Copy of a JSON schema in the subset strict structured outputs accept: every object closed
    (additionalProperties false), no numeric or length bounds. The script checks ranges itself."""
    if isinstance(schema, list):
        return [strict_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: strict_schema(v) for k, v in schema.items()
           if k not in ("minimum", "maximum", "minItems", "maxItems")}
    if out.get("type") == "object":
        out["additionalProperties"] = False
    return out


def _ask_openai(model, schema, messages):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "result", "strict": True,
                                            "schema": strict_schema(schema)}},
        **LLM["extra"],
    }
    # --llm-extra '{"temperature": null}' drops a field a model does not accept.
    payload = {k: v for k, v in payload.items() if v is not None}
    headers = {"Authorization": f"Bearer {LLM['api_key']}"} if LLM["api_key"] else {}
    for attempt in range(6):
        r = requests.post(f"{LLM['url']}/chat/completions", json=payload, headers=headers, timeout=900)
        if r.status_code < 500 and r.status_code != 429:
            break
        log.warning("LLM call failed (HTTP %s), retry %d/6", r.status_code, attempt + 1)
        time.sleep(min(60, 5 * 2 ** attempt))  # upstream rate limits clear within a minute or so
    if not r.ok:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    if "choices" not in data:
        raise RuntimeError(f"LLM error: {json.dumps(data)[:500]}")
    usage = data.get("usage") or {}
    stats = {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "prompt_s": 0.0,
        "gen_tokens": usage.get("completion_tokens", 0),
        "gen_s": 0.0,
        "cost": usage.get("cost"),
        "provider": data.get("provider"),
    }
    content = data["choices"][0]["message"]["content"] or ""
    # Some models wrap the JSON in a code fence despite structured output.
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    return json.loads(content), stats


def wait_for_ollama(timeout=180):
    """Waits for the LLM endpoint (name kept from when only Ollama was supported)."""
    if LLM["backend"] == "openai":
        return
    deadline = time.time() + timeout
    while True:
        try:
            requests.get(f"{LLM['url']}/api/version", timeout=5).raise_for_status()
            return
        except requests.RequestException:
            if time.time() > deadline:
                raise SystemExit(f"Ollama not reachable at {LLM['url']}")
            time.sleep(3)


def parse_date(value):
    try:
        d = dt.date.fromisoformat((value or "").strip())
    except ValueError:
        return None
    # Reject obviously hallucinated dates.
    return d if 1990 <= d.year <= dt.date.today().year + 10 else None


def fmt_money(amount, currency):
    currency = (currency or "EUR").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        currency = "EUR"
    return f"{currency}{amount:.2f}"


STOPWORDS = {"gmbh", "group", "team", "the", "und", "und", "der", "die", "das", "von", "from",
             "info", "noreply", "no-reply", "service", "kundenservice", "backoffice", "reg",
             "prof", "dr", "www", "com", "de", "net", "org", "mail", "account", "ag", "kg", "se"}


def name_tokens(name):
    return [t for t in re.split(r"[^\wäöüß]+", name.casefold()) if len(t) >= 3 and t not in STOPWORDS]


HOUSEHOLD_SURNAMES = {n.split()[-1].casefold() for n in HOUSEHOLD}
HOUSEHOLD_FIRST_NAMES = {t for n in HOUSEHOLD for t in name_tokens(n)} - HOUSEHOLD_SURNAMES


def is_household(name):
    tokens = set(name_tokens(name or ""))
    return bool(tokens & HOUSEHOLD_SURNAMES) or (bool(tokens) and tokens <= HOUSEHOLD_FIRST_NAMES)


def same_party(name, party):
    """True if an existing correspondent name and the model's counterparty name the same company:
    all significant words of one are in the other. "comdirect" fits "comdirect - eine Marke der
    Commerzbank AG"; "Deutsche Post" (franking line) does not fit "comdirect"."""
    a, b = set(name_tokens(name)), set(name_tokens(party))
    return bool(a) and bool(b) and (a <= b or b <= a)


def evidenced(name, doc, result):
    """True if `name`, or every significant word of it, occurs in the document or the model's
    sender field. Requiring all words keeps "Deutsche Post" from matching a "Deutsche Bahn" ticket.
    Household names never count: they are in every document as the recipient."""
    if is_household(name):
        return False
    haystack = " ".join([doc.get("content") or "", doc.get("title") or "",
                         doc.get("original_file_name") or "", result.get("absender") or ""]).casefold()
    if name.casefold() in haystack:
        return True
    tokens = name_tokens(name)
    return bool(tokens) and all(re.search(rf"(?<![\wäöüß]){re.escape(t)}(?![\wäöüß])", haystack)
                                for t in tokens)


MONTHS = ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august",
          "september", "oktober", "november", "dezember"]
NUM_WORDS = {"sieben": 7, "zehn": 10, "vierzehn": 14, "einundzwanzig": 21, "achtundzwanzig": 28,
             "dreißig": 30, "dreissig": 30, "zwei": 2, "drei": 3, "vier": 4, "sechs": 6, "acht": 8}


def date_in_text(d, text):
    """True if date d is written literally in the text, in a common German or ISO format."""
    pats = [rf"0?{d.day}\.\s?0?{d.month}\.\s?(?:{d.year}|{d.year % 100:02d})(?!\d)",
            rf"0?{d.day}/0?{d.month}/(?:{d.year}|{d.year % 100:02d})(?!\d)",
            rf"{d.year}-{d.month:02d}-{d.day:02d}",
            rf"0?{d.day}\.?\s+{MONTHS[d.month - 1]}\s+{d.year}"]
    return any(re.search(rf"(?<!\d){p}", text) for p in pats)


def relative_days(text):
    """Day counts of phrases like "14 Tage", "vierzehn Tagen", "2 Wochen" in the text."""
    days = set()
    for num, unit in re.findall(r"(\d{1,3}|[a-zß]+)\s+(?:kalender|werk|bank(?:arbeits)?)?(tag|woche)",
                                text):
        n = int(num) if num.isdigit() else NUM_WORDS.get(num)
        if n:
            days.add(n * 7 if unit == "woche" else n)
    return days


def frist_evidenced(frist, doc, result):
    """A deadline must be written in the document, or be document date + a period named in it.
    Guards against the model counting "14 Tage" from today."""
    text = (doc.get("content") or "").casefold()
    if date_in_text(frist, text):
        return True
    bases = {b for b in (parse_date(result.get("created")),
                         parse_date(doc.get("created_date") or doc.get("created"))) if b}
    return any((frist - b).days in relative_days(text) for b in bases)


# --- Document date ---

INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})(?!\d)")
# Dates after these words are never the letter date.
NOT_LETTER_DATE = re.compile(r"geburts|geb\.|geboren|zeitraum|frist|spätestens|fällig|stichtag", re.I)
DATE_LABEL = re.compile(r"(?<!geburts)(?<!liefer)(?<!leistungs)datum", re.I)
PLACE_COMMA = re.compile(r"(?:^|\n)[ \t]*[A-ZÄÖÜ][\wäöüß.\- ]{1,40},[ \t]*(?:den[ \t]+)?$")


def clean_text(text):
    # Google Docs exports put zero-width spaces around every line.
    return (text or "").translate(INVISIBLE)


def numeric_dates(text):
    """[(date, context)] for every TT.MM.JJ(JJ) date; context is the text before it, back to the
    previous date. The second date of a range ("01.01.2025 - 31.12.2025") shares the first's."""
    out, prev_end, prev_ctx = [], 0, ""
    for m in DATE_RE.finditer(text):
        day, month, year = (int(g) for g in m.groups())
        between = text[prev_end:m.start()]
        ctx = prev_ctx if prev_end and re.fullmatch(r"\s*(?:-|–|bis)\s*", between) else between[-40:]
        try:
            out.append((dt.date(year + 2000 if year < 100 else year, month, day), ctx))
        except ValueError:
            pass
        prev_end, prev_ctx = m.end(), ctx
    return out


def letter_date(text, labelled=True):
    """The date of an "Ort, den TT.MM.JJJJ" line, else (if `labelled`) the first date labelled "Datum"."""
    dates = [(d, ctx) for d, ctx in numeric_dates(text) if not NOT_LETTER_DATE.search(ctx)]
    for d, ctx in dates:
        if PLACE_COMMA.search(ctx):
            return d
    for d, ctx in dates:
        if labelled and DATE_LABEL.search(ctx):
            return d
    return None


def created_evidenced(d, text, doc):
    """The model's date must be in the text and not be a birth date, period or deadline. A date
    whose year the OCR cut off ("05.10.2") counts if the year fits the scan."""
    found = [ctx for x, ctx in numeric_dates(text) if x == d]
    if found:
        return any(not NOT_LETTER_DATE.search(ctx) for ctx in found)
    if date_in_text(d, text.casefold()):
        return True
    added = parse_date((doc.get("added") or "")[:10])
    return bool(added and added.year - 1 <= d.year <= added.year and re.search(
        rf"(?<![\d.])0?{d.day}\.\s?0?{d.month}\.\s?(?:2|20|202)?(?![\d.])", text))


def cut_off_date(text, doc):
    """A letterhead date whose year the OCR cut off at the line end ("05.10.2"), completed with
    the year that puts it on or before the scan."""
    added = parse_date((doc.get("added") or "")[:10])
    if not added:
        return None
    for m in re.finditer(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.\s?(?:2|20|202)?[ \t]*$", text[:2000], re.M):
        before = text[max(0, m.start() - 40):m.start()]
        if NOT_LETTER_DATE.search(before) or re.search(r"\b(?:vom|bis|ab)\s*$", before, re.I):
            continue
        for year in (added.year, added.year - 1):
            try:
                d = dt.date(year, int(m.group(2)), int(m.group(1)))
            except ValueError:
                break
            if d <= added:
                return d
    return None


def filename_date(doc):
    """Scan date from names like "20261012-094950.pdf" or Brother's "..._10172026_182449_...". """
    name = doc.get("original_file_name") or ""
    for m in re.finditer(r"(?<!\d)(20\d\d)(\d\d)(\d\d)(?!\d)|(?<!\d)(\d\d)(\d\d)(20\d\d)(?!\d)", name):
        y, mo, d = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(6), m.group(4), m.group(5))
        try:
            date = dt.date(int(y), int(mo), int(d))
        except ValueError:
            continue
        if date <= dt.date.today():
            return date
    return None


def plan_created(doc, result, text, reasons):
    """The letter date: from an "Ort, den" or "Datum" line, else the model's date if it is in the
    text. Falls back to the file name's scan date only if the current date is no letter date
    (e.g. Paperless took a birth date on consumption)."""
    raw = doc.get("created_date") or doc.get("created") or ""
    try:
        current = dt.date.fromisoformat(raw[:10])
    except ValueError:
        current = None
    model = parse_date(result.get("created"))
    # An "Ort, den" line wins; then the model's date if the text backs it up; only then the first date
    # next to a "...datum" label, which can belong to an enclosure (e.g. "Rechnungsdatum" of an invoice
    # sent along with an insurer's letter).
    chosen = letter_date(text, labelled=False)
    if not chosen and model:
        if created_evidenced(model, text, doc):
            chosen = model
        else:
            log.info("Document %s: rejecting date %s (not a letter date in the text)", doc["id"], model)
    if not chosen:
        chosen = letter_date(text)
    if not chosen:
        chosen = cut_off_date(text, doc)
        if chosen:
            reasons.append(f"Briefdatum nur unvollständig lesbar, Jahr ergänzt: {chosen:%d.%m.%Y}")
    if not chosen and current:
        contexts = [ctx for d, ctx in numeric_dates(text) if d == current]
        if current.year < 1990 or (contexts and all(NOT_LETTER_DATE.search(c) for c in contexts)):
            chosen = filename_date(doc)
            reasons.append(f"Briefdatum nicht erkannt; das bisherige Datum {current:%d.%m.%Y} ist kein "
                           f"Briefdatum (z. B. Geburtsdatum)"
                           + (f", ersetzt durch das Scandatum {chosen:%d.%m.%Y} aus dem Dateinamen"
                              if chosen else ""))
    return chosen if chosen and chosen != current else None


# --- Invoice number ---

INVOICE_LABEL = re.compile(r"rechnung(?:s)?[-\s]*(?:nr|nummer|no)|rg\.?[-\s]*nr|invoice\s*(?:no|number|#)"
                           r"|abrechnungs[-\s]*(?:nr|nummer)|belegnummer", re.I)
OTHER_NUMBER_LABEL = re.compile(r"aktenzeichen|\baz\b|zeichen|kunden[-\s]*(?:nr|nummer)|vertrag\w*[-\s]*nr"
                                r"|vertragsnummer|versicherten|bestell|kassenzeichen|mandat", re.I)


def invoice_number_ok(number, text):
    """True if the number appears in the text right after an invoice-number label, and not after
    an Aktenzeichen, customer number or similar."""
    chars = [re.escape(c) for c in re.sub(r"\s+", "", number)]
    if len(chars) < 3:
        return False
    for m in re.finditer(r"\s*".join(chars), text):
        before = text[max(0, m.start() - 120):m.start()]
        inv = [x.end() for x in INVOICE_LABEL.finditer(before)]
        other = [x.end() for x in OTHER_NUMBER_LABEL.finditer(before)]
        if inv and (not other or inv[-1] > other[-1]):
            return True
    return False


def norm_invoice(number):
    n = re.sub(r"[^0-9a-z]", "", (number or "").casefold())
    return n if len(n) >= 4 and any(c.isdigit() for c in n) else ""


# --- Duplicates ---

def shingles(text):
    words = re.findall(r"[\wäöüß]+", clean_text(text).casefold())
    return {" ".join(words[i:i + 5]) for i in range(len(words) - 4)}


ALL_DOC_FIELDS = "id,title,content,correspondent,document_type,tags,custom_fields"


def build_dup_index(all_docs, ctx):
    """Content fingerprints, invoice numbers and correspondents of all documents in Paperless."""
    cf_id = ctx["custom_fields"].get(CF_RECHNUNGSNUMMER)
    index = {}
    for d in all_docs:
        inv = next((c["value"] for c in d.get("custom_fields") or [] if c["field"] == cf_id), "")
        index[d["id"]] = {"shingles": shingles(d.get("content")), "invoice": norm_invoice(inv),
                          "corr": d.get("correspondent"), "content": (d.get("content") or "").casefold()}
    log.info("Duplicate check: %d documents indexed", len(index))
    return index


def find_duplicates(doc, invoice, corr_id, sender, ctx):
    """Ids of documents with near-identical text, or with the same invoice number from the same
    sender. The document is then (re)registered in the index with its new values, so later
    documents of the same run are compared against it, too."""
    index, own = ctx["dup_index"], doc["id"]
    origin = str(own).split("/")[0]  # a dry-run split part is not a duplicate of its original
    sh = shingles(doc.get("content"))
    sender_tokens = name_tokens(sender or "")
    hits = {}
    for other, e in index.items():
        if other == own or str(other).split("/")[0] == origin:
            continue
        if (len(sh) >= 20 and e["shingles"]
                and min(len(sh), len(e["shingles"])) / max(len(sh), len(e["shingles"])) >= DUPLICATE_SIMILARITY
                and len(sh & e["shingles"]) / len(sh | e["shingles"]) >= DUPLICATE_SIMILARITY):
            hits[other] = "nahezu identischer Inhalt"
        elif invoice and e["invoice"] == invoice and (
                (corr_id is not None and e["corr"] == corr_id)
                or (sender_tokens and all(t in e["content"] for t in sender_tokens))):
            hits[other] = f"gleiche Rechnungsnummer {invoice} vom selben Absender"
    index[own] = {"shingles": sh, "invoice": invoice, "corr": corr_id,
                  "content": (doc.get("content") or "").casefold()}
    return hits


# --- Examples: similar documents we already filed ---

WORD_RE = re.compile(r"[a-zäöüß][a-zäöüß0-9&\-]{2,}")


def word_counts(text):
    # Letterhead and footer (imprint) name the sender, so a long text keeps head and tail.
    t = clean_text(text).casefold()
    if len(t) > 4500:
        t = t[:3000] + " " + t[-1500:]
    return collections.Counter(WORD_RE.findall(t))


def build_example_index(all_docs, ctx):
    """TF-IDF vectors of all documents; the filed ones (no Inbox, no Prüfen) are the examples."""
    counts = {d["id"]: word_counts(d.get("content")) for d in all_docs}
    df = collections.Counter(w for c in counts.values() for w in c)
    n = len(all_docs) or 1
    ctx["idf"] = {w: math.log(n / k) for w, k in df.items() if k > 1}  # words in one doc only: noise
    workflow = {ctx["inbox_id"], ctx["review_id"]}
    ctx["examples"] = {d["id"]: dict(d, vec=tfidf(counts[d["id"]], ctx)) for d in all_docs
                       if not workflow & set(d["tags"])}
    ctx["similar"] = {}
    log.info("Examples: %d filed documents indexed", len(ctx["examples"]))


def tfidf(counts, ctx):
    idf = ctx["idf"]
    v = {w: (1 + math.log(k)) * idf[w] for w, k in counts.items() if w in idf}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {w: x / norm for w, x in v.items()}


def similar_examples(doc, ctx):
    """Up to EXAMPLES_MAX filed documents most similar to `doc`, as [(similarity, entry)]."""
    if "examples" not in ctx:
        return []
    key = str(doc["id"])
    if key not in ctx["similar"]:
        origin = key.split("/")[0]  # a dry-run split part: not its own original
        v = tfidf(word_counts(doc.get("content")), ctx)
        scored = []
        for eid, e in ctx["examples"].items():
            if str(eid) == origin:
                continue
            a, b = (v, e["vec"]) if len(v) < len(e["vec"]) else (e["vec"], v)
            s = sum(x * b.get(w, 0.0) for w, x in a.items())
            if s >= EXAMPLES_MIN_SIMILARITY:
                scored.append((s, e))
        scored.sort(key=lambda x: -x[0])
        ctx["similar"][key] = scored[:EXAMPLES_MAX]
        if scored:
            log.info("Document %s: similar filed documents %s", doc["id"],
                     ", ".join(f"#{e['id']} ({s:.2f})" for s, e in scored[:EXAMPLES_MAX]))
    return ctx["similar"][key]


def learned(examples, values_of):
    """Values (correspondent ids, tag ids) our filing of similar documents backs."""
    strong, seen = set(), collections.Counter()
    for s, e in examples:
        vals = set(values_of(e))
        if s >= LEARNED_STRONG_SIMILARITY:
            strong |= vals
        if s >= LEARNED_MIN_SIMILARITY:
            seen.update(vals)
    return strong | {v for v, k in seen.items() if k >= 2}


def examples_section(examples, ctx):
    if not examples:
        return ""
    names = lambda items, i: next((x["name"] for x in items.values() if x["id"] == i), "")
    lines = []
    for n, (s, e) in enumerate(examples, 1):
        topic = [t["name"] for t in ctx["tags"].values()
                 if t["id"] in e["tags"] and t["name"] in TAG_DEFINITIONS]
        snippet = re.sub(r"\s+", " ", clean_text(e.get("content")))[:EXAMPLE_SNIPPET_CHARS]
        lines.append(f"{n}. Ähnlichkeit {s:.2f}\n"
                     f"   Titel: {e.get('title') or ''}\n"
                     f"   Korrespondent: {names(ctx['correspondents'], e['correspondent']) or '(leer)'}\n"
                     f"   Dokumenttyp: {names(ctx['doc_types'], e['document_type']) or '(leer)'}\n"
                     f"   Themen-Tags: {', '.join(sorted(topic)) or '(keine)'}\n"
                     f"   Textanfang: {snippet}")
    return ("Ähnliche Dokumente, die wir bereits abgelegt und geprüft haben (so haben wir sie abgelegt):\n"
            + "\n".join(lines) + "\n\n")


def paperless_suggestions(pl, doc, ctx):
    """Paperless' own classifier (trained on filed documents, no AI) as a prompt hint. It only
    knows correspondents, types and tags whose matching is "Auto"."""
    if not isinstance(doc["id"], int):
        return ""  # a dry-run split part does not exist in Paperless yet
    try:
        s = pl.get(f"documents/{doc['id']}/suggestions/")
    except Exception as e:
        log.warning("Document %s: Paperless suggestions failed (%s)", doc["id"], e)
        return ""
    names = lambda items, ids: [x["name"] for x in items.values() if x["id"] in ids]
    corr = [n for n in names(ctx["correspondents"], s.get("correspondents") or []) if not is_household(n)]
    types = names(ctx["doc_types"], s.get("document_types") or [])
    tags = [n for n in names(ctx["tags"], s.get("tags") or []) if n in TAG_DEFINITIONS]
    if not (corr or types or tags):
        return ""
    log.info("Document %s: Paperless suggests corr=%s type=%s tags=%s", doc["id"], corr, types, tags)
    return ("Vorschläge der Paperless-eigenen Zuordnung (aus unserer bisherigen Ablage gelernt, kann "
            "sich irren):\n"
            f"- Korrespondent: {', '.join(corr) or '(keiner)'}\n"
            f"- Dokumenttyp: {', '.join(types) or '(keiner)'}\n"
            f"- Themen-Tags: {', '.join(tags) or '(keine)'}\n\n")


def word_in_doc(word, doc):
    haystack = " ".join([doc.get("content") or "", doc.get("title") or "",
                         doc.get("original_file_name") or ""]).casefold()
    return bool(re.search(rf"(?<![\wäöüß]){re.escape(word.casefold())}(?![\wäöüß])", haystack))


def plan_shared_tags(doc, result, ctx):
    """Tag ids for the child tags, the lease tag and the partner tag, and whether the child is unclear.
    A child is only tagged if its name is in the document; otherwise just "Kinder"."""
    names = []
    kids = []
    for kid in result.get("kinder") or []:
        if kid in CHILDREN and kid not in kids:
            if word_in_doc(kid, doc):
                kids.append(kid)
            else:
                log.info("Document %s: rejecting child %r (name not in document)", doc["id"], kid)
    child_related = bool(result.get("kinder_bezug")) or bool(kids)
    if child_related:
        names += [CHILD_TAG] + kids
    if result.get("mietvertrag"):
        names.append(LEASE_TAG)
    if PARTNER_TAG and result.get("an_partner"):
        if all(word_in_doc(w, doc) for w in PARTNER_NAME):
            names.append(PARTNER_TAG)
        else:
            log.info("Document %s: rejecting %r (name not in document)", doc["id"], PARTNER_TAG)

    ids = []
    for name in names:
        tag = ctx["tags"].get(name.casefold())
        if tag:
            ids.append(tag["id"])
        else:
            log.warning("Tag %r missing in Paperless, not created", name)
    return ids, child_related and not kids


def tag_evidenced(tag, text):
    stems = TAG_KEYWORDS.get(tag)
    return not stems or any(re.search(rf"(?<![\wäöüß]){re.escape(s)}", text, re.I) for s in stems)


def strip_address(name):
    """"Stadt Musterstadt · Bürgerbüro · Hauptstr. 1 · 12345 Musterstadt" ->
    "Stadt Musterstadt – Bürgerbüro": keep the parts before the first one with a
    house number or postcode."""
    parts = [p.strip() for p in re.split(r"\s+[·•|+]\s+|\s+-\s+|,\s*", name or "") if p.strip()]
    kept = []
    for p in parts:
        if re.search(r"\d", p):
            break
        kept.append(p)
    return " – ".join(kept) if kept else (name or "").strip()


def tame_caps(name):
    """"MUSTER BÄCKEREI" -> "Muster Bäckerei"; short acronyms such as "DKV" or "GmbH" stay."""
    return " ".join(w.capitalize() if len(w) > 4 and w.isupper() else w for w in (name or "").split(" "))


def pick_correspondent(doc, result, ctx):
    """The existing correspondent the model chose, if it holds up, and why not otherwise.
    Returns (match or None, counterparty name, rejection reason or None)."""
    party = (result.get("gegenpartei") or "").strip()
    if not party and not result.get("von_uns"):
        party = (result.get("absender") or "").strip()
    party = strip_address(party)
    if is_household(party):
        party = ""
    name = (result.get("correspondent") or "").strip()
    match = ctx["correspondents"].get(name.casefold()) if name else None
    why = None
    if match and not is_household(match["name"]) and match["id"] in learned(
            similar_examples(doc, ctx), lambda e: [e["correspondent"]] if e["correspondent"] else []):
        log.info("Document %s: correspondent %r backed by similar filed documents", doc["id"], match["name"])
        return match, party, None
    if match:
        if is_household(match["name"]):
            why = "gehört zum Haushalt (Empfänger)"
        elif not evidenced(match["name"], doc, result):
            why = "steht nicht im Dokument"
        elif party and not same_party(match["name"], party):
            why = f"passt nicht zum erkannten Absender \"{party}\""
        else:
            return match, party, None
        log.info("Document %s: rejecting correspondent %r (%s)", doc["id"], match["name"], why)
    # The model often recognises the sender but picks a wrong list entry (or none): take the one
    # existing correspondent that names the same sender, if there is exactly one.
    same = [c for c in ctx["correspondents"].values()
            if party and not is_household(c["name"]) and same_party(c["name"], party)
            and evidenced(c["name"], doc, result)]
    if len(same) == 1:
        log.info("Document %s: correspondent %r matches the sender %r", doc["id"], same[0]["name"], party)
        return same[0], party, None
    if not match:
        return None, party, None
    return None, party, f"Vom Modell gewählter Korrespondent \"{match['name']}\" verworfen: {why}"


def plan_correspondent(doc, result, ctx, patch, reasons, suggestions):
    """Sets patch["correspondent"]. A household correspondent (set by Paperless' auto-matching)
    counts as empty and is replaced, or removed if nothing fits. Returns True if the model's pick
    was overruled."""
    current = doc["correspondent"]
    current_name = next((c["name"] for c in ctx["correspondents"].values() if c["id"] == current), None)
    household_set = current is not None and is_household(current_name)
    if current is not None and not household_set:
        return False  # never overwrite a value a human (or a sensible matching rule) set
    match, party, rejected = pick_correspondent(doc, result, ctx)
    if rejected:
        reasons.append(rejected)
    if household_set:
        reasons.append(f"Bisheriger Korrespondent \"{current_name}\" gehört zum Haushalt (Empfänger) "
                       + ("und wurde ersetzt" if match else "und wurde entfernt"))
    if match:
        patch["correspondent"] = match["id"]
        return bool(rejected)
    if household_set:
        patch["correspondent"] = None
    suggestion = strip_address(result.get("correspondent_vorschlag"))
    if not suggestion or is_household(suggestion):
        suggestion = party
    suggestion = tame_caps(suggestion)
    if suggestion:
        threshold = AUTO_CREATE_CORRESPONDENT_MIN_CONFIDENCE
        if threshold is not None and not rejected and float(result.get("confidence") or 0) >= threshold:
            patch["correspondent"] = {"create": suggestion}  # created when the patch is applied
            log.info("Document %s: new correspondent %r will be created", doc["id"], suggestion)
        else:
            suggestions.append(f"Korrespondent: {suggestion}")
            reasons.append("Kein passender Korrespondent vorhanden (Vorschlag unten)")
    else:
        reasons.append("Absender nicht erkannt, Korrespondent bleibt leer")
    return bool(rejected)


def plan_changes(doc, result, ctx, extra_reasons=()):
    """Turn the model's answer into a PATCH payload, a note and a review decision.
    Every "Prüfen" comes with its reasons in the note."""
    patch, suggestions, reasons = {}, [], list(extra_reasons)
    text = clean_text(doc.get("content"))

    title = (result.get("title") or "").strip()[:128]
    if title and title != doc["title"]:
        patch["title"] = title

    overruled = plan_correspondent(doc, result, ctx, patch, reasons, suggestions)
    name = (result.get("document_type") or "").strip()
    match = ctx["doc_types"].get(name.casefold()) if name else None
    if match:
        if doc["document_type"] is None:
            patch["document_type"] = match["id"]
        elif doc["document_type"] != match["id"]:
            # Set at import by Paperless' auto-matching (the tagger only sees Inbox documents). It is
            # kept, but a disagreement gets "Prüfen": on 2026-10-25 a polluted classifier filed 20 rent
            # documents as "Paketschein", and Paperless is often right with a more specific type.
            current = next((t["name"] for t in ctx["doc_types"].values()
                            if t["id"] == doc["document_type"]), f"#{doc['document_type']}")
            reasons.append(f"Dokumenttyp unklar: Paperless hat beim Import \"{current}\" gesetzt, "
                           f"das Modell sagt \"{match['name']}\"")
    else:
        suggestion = (result.get("document_type_vorschlag") or name).strip()
        if suggestion and doc["document_type"] is None:
            suggestions.append(f"Dokumenttyp: {suggestion}")

    tag_ids = list(doc["tags"])
    picked = [name for name, yes in (result.get("themen") or {}).items() if yes is True]
    learned_tags = learned(similar_examples(doc, ctx), lambda e: e["tags"])
    added = 0
    for name in sorted(picked, key=lambda n: list(TAG_DEFINITIONS).index(n) if n in TAG_DEFINITIONS else 99):
        t = ctx["tags"].get(name.casefold())
        if not t or t["name"] not in TAG_DEFINITIONS:
            continue
        if not tag_evidenced(t["name"], text) and t["id"] not in learned_tags:
            log.info("Document %s: dropping tag %r (no keyword in text)", doc["id"], t["name"])
        elif added >= MAX_TOPIC_TAGS:
            log.info("Document %s: dropping tag %r (more than %d)", doc["id"], t["name"], MAX_TOPIC_TAGS)
        elif t["id"] not in tag_ids:
            tag_ids.append(t["id"])
            added += 1
    shared_ids, child_unclear = plan_shared_tags(doc, result, ctx)
    tag_ids += [t for t in shared_ids if t not in tag_ids]
    new_tag_suggestions = [s.strip() for s in result.get("tags_vorschlag") or []
                           if s.strip() and s.strip().casefold() not in ctx["tags"]
                           and s.strip().casefold() not in WORKFLOW_TAGS]
    if new_tag_suggestions:
        suggestions.append("Tags: " + ", ".join(new_tag_suggestions))

    created = plan_created(doc, result, text, reasons)
    if created:
        patch["created"] = created.isoformat()

    frist = parse_date(result.get("frist"))
    if frist and not frist_evidenced(frist, doc, result):
        log.info("Document %s: rejecting Frist %s (not in document, not document date + period)",
                 doc["id"], frist)
        reasons.append(f"Vom Modell genannte Frist {frist:%d.%m.%Y} steht nicht im Text und wurde verworfen")
        frist, overruled = None, True
    betrag = result.get("betrag")
    rechnungsnr = (result.get("rechnungsnummer") or "").strip()[:128]
    if rechnungsnr and not invoice_number_ok(rechnungsnr, text):
        log.info("Document %s: rejecting Rechnungsnummer %r (not labelled as invoice number)",
                 doc["id"], rechnungsnr)
        rechnungsnr = ""

    corr_id = patch.get("correspondent", doc["correspondent"])
    if not isinstance(corr_id, int):
        corr_id = None
    sender = next((c["name"] for c in ctx["correspondents"].values() if c["id"] == corr_id), None)
    duplicates = find_duplicates(doc, norm_invoice(rechnungsnr), corr_id,
                                 sender or (result.get("gegenpartei") or "").strip(), ctx)
    for other, why in sorted(duplicates.items(), key=lambda x: str(x[0])):
        reasons.append(f"Mögliches Duplikat von #{other} ({why})")

    cf = {c["field"]: c["value"] for c in doc.get("custom_fields") or []}
    cf_before = dict(cf)
    fields = ctx["custom_fields"]
    if isinstance(betrag, (int, float)) and betrag > 0 and CF_BETRAG in fields:
        cf[fields[CF_BETRAG]] = fmt_money(betrag, result.get("waehrung"))
    if frist and CF_FRIST in fields:
        cf[fields[CF_FRIST]] = frist.isoformat()
    if rechnungsnr and CF_RECHNUNGSNUMMER in fields:
        cf[fields[CF_RECHNUNGSNUMMER]] = rechnungsnr
    if cf != cf_before:
        patch["custom_fields"] = [{"field": k, "value": v} for k, v in cf.items()]

    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < CONFIDENCE_THRESHOLD:
        reasons.append(f"Modell unsicher (Sicherheit {confidence:.2f})")
    if overruled:
        # The model's self-assessment included a wrong pick, so don't trust it.
        confidence = min(confidence, CONFIDENCE_THRESHOLD - 0.01)
    frist_tag = ctx["tags"].get(FRIST_TAG.casefold())
    if frist and frist >= dt.date.today() and frist_tag and frist_tag["id"] not in tag_ids:
        tag_ids.append(frist_tag["id"])
    if child_unclear:
        reasons.append(f"Betrifft ein Kind, aber welches ist unklar: bitte "
                       f"{' und/oder '.join(CHILDREN)} ergänzen")
    needs_review = bool(reasons)

    inbox_id = ctx["inbox_id"]
    if needs_review:
        tag_ids.append("REVIEW")  # resolved to the real id once the tag exists
    elif inbox_id in tag_ids:
        tag_ids.remove(inbox_id)
    if tag_ids != list(doc["tags"]):
        patch["tags"] = tag_ids

    notes = []
    if needs_review:
        notes.append("Zur Prüfung markiert:\n- " + "\n- ".join(reasons))
    if suggestions:
        notes.append(f"Kein passender Eintrag vorhanden, Vorschläge "
                     f"(Sicherheit {confidence:.2f}):\n- " + "\n- ".join(suggestions))
    note = f"{NOTE_PREFIX} " + "\n".join(notes) if notes else None
    return patch, note, needs_review, confidence, frist, {
        "reasons": reasons, "duplicates": sorted(duplicates, key=str), "rechnungsnummer": rechnungsnr}


def plan_backfill(doc, result, ctx):
    """Backfill mode: only fill an empty correspondent/document type.
    Titles, dates, tags, custom fields and notes stay untouched."""
    patch = {}
    # A rejected correspondent is simply not applied; unlike the Inbox run there is
    # no review tag to route it to, so it shouldn't discard the other fields too.
    match, _, _ = pick_correspondent(doc, result, ctx)
    if match and doc["correspondent"] is None:
        patch["correspondent"] = match["id"]
    name = (result.get("document_type") or "").strip()
    match = ctx["doc_types"].get(name.casefold()) if name else None
    if match and doc["document_type"] is None:
        patch["document_type"] = match["id"]

    # Topic tags are left out: in the test run the model's picks were too unreliable.
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0
    # Without the "Prüfen" safety net, unsure answers are dropped, unless a human
    # reviews the document anyway.
    if confidence < CONFIDENCE_THRESHOLD and ctx["review_id"] not in doc["tags"]:
        return {}, confidence, True
    return patch, confidence, False


def needs_backfill(doc, ctx):
    return doc["correspondent"] is None or doc["document_type"] is None


def corr_label(value, ctx):
    if value is None:
        return "(leer)"
    if isinstance(value, dict):
        return f"{value['create']} (neu)"
    return next((c["name"] for c in ctx["correspondents"].values() if c["id"] == value), value)


def proposal(doc, patch, note, ctx, needs_review, confidence, frist, info):
    """The proposed end state of a document, for --json."""
    tag_names = {v["id"]: v["name"] for v in ctx["tags"].values()}
    tag_names["REVIEW"] = REVIEW_TAG
    cf_names = {v: k for k, v in ctx["custom_fields"].items()}
    cf = {cf_names.get(c["field"], c["field"]): c["value"]
          for c in patch.get("custom_fields", doc.get("custom_fields") or [])}
    corr = patch.get("correspondent", doc["correspondent"])
    dtype = patch.get("document_type", doc["document_type"])
    suggestion = next((line.split(": ", 1)[1] for line in (note or "").splitlines()
                       if line.startswith("- Korrespondent: ")), None)
    return {
        "id": doc["id"],
        "titel": patch.get("title", doc["title"]),
        "titel_bisher": doc["title"],
        "korrespondent": None if corr is None else corr_label(corr, ctx),
        "korrespondent_bisher": None if doc["correspondent"] is None else corr_label(doc["correspondent"], ctx),
        "korrespondent_vorschlag": suggestion,
        "dokumenttyp": next((t["name"] for t in ctx["doc_types"].values() if t["id"] == dtype), None),
        "datum": patch.get("created", doc.get("created_date") or doc.get("created")),
        "datum_bisher": doc.get("created_date") or doc.get("created"),
        "tags": [tag_names.get(t, t) for t in patch.get("tags", doc["tags"])],
        "betrag": cf.get(CF_BETRAG),
        "frist": cf.get(CF_FRIST),
        "rechnungsnummer": cf.get(CF_RECHNUNGSNUMMER),
        "pruefen": needs_review,
        "gruende": info.get("reasons", []),
        "duplikat_von": info.get("duplicates", []),
        "sicherheit": round(confidence, 2),
        "notiz": note,
    }


def describe(doc, patch, note, ctx, needs_review, confidence, frist):
    names = {v["id"]: v["name"] for v in ctx["tags"].values()}
    names["REVIEW"] = REVIEW_TAG
    cf_names = {v: k for k, v in ctx["custom_fields"].items()}
    lines = [f"Dokument {doc['id']}: \"{doc['title']}\"  (Sicherheit {confidence:.2f}, "
             f"{'Prüfen' if needs_review else 'automatisch erledigt'})"]
    for key, value in patch.items():
        if key == "correspondent":
            value = corr_label(value, ctx)
        elif key == "document_type":
            value = next(c["name"] for c in ctx["doc_types"].values() if c["id"] == value)
        elif key == "tags":
            before = [names.get(t, t) for t in doc["tags"]]
            value = f"{before} -> {[names.get(t, t) for t in value]}"
        elif key == "custom_fields":
            value = ", ".join(f"{cf_names.get(c['field'], c['field'])}={c['value']}" for c in value)
        lines.append(f"    {key}: {value}")
    if note:
        lines.append("    note: " + note.replace("\n", "\n          "))
    if not patch and not note:
        lines.append("    (keine Änderungen)")
    return "\n".join(lines)


def ensure_review_tag(pl, ctx, dry_run):
    if ctx["review_id"] is not None or dry_run:
        return ctx["review_id"]
    tag = pl.post("tags/", {"name": REVIEW_TAG, "matching_algorithm": 0, "color": "#e5a50a"})
    log.info("Created tag %r (id %s)", REVIEW_TAG, tag["id"])
    ctx["review_id"] = tag["id"]
    ctx["tags"][REVIEW_TAG.casefold()] = tag
    return tag["id"]


def flag_for_review(pl, ctx, doc, note):
    """Add "Prüfen" and a note; used when the split step can't finish on its own."""
    rid = ensure_review_tag(pl, ctx, False)
    if rid not in doc["tags"]:
        pl.patch(f"documents/{doc['id']}/", {"tags": list(doc["tags"]) + [rid]})
    pl.post(f"documents/{doc['id']}/notes/", {"note": note})


# --- Step 1: split combined scans -------------------------------------------------------------

SPLIT_SYSTEM_PROMPT = """Du prüfst eingescannte PDF-Dateien für Paperless-ngx. Oft werden mehrere getrennte Schriftstücke in einem Durchgang gescannt.
Du erhältst den OCR-Text jeder Seite (lange Seiten gekürzt) und entscheidest, auf welchen Seiten ein neues, eigenständiges Dokument beginnt. Antworte ausschließlich mit JSON gemäß dem vorgegebenen Schema.

Hinweise auf den Beginn eines neuen Dokuments:
- ein neuer Briefkopf oder ein neues Logo, ein anderer Absender oder Empfänger,
- ein neues Datum oder eine neue Anrede ("Sehr geehrte ..."),
- "Seite 1" bzw. "Seite 1 von N", wenn die Seite davor zu einem anderen Dokument gehörte,
- ein deutlicher Themenwechsel (z. B. erst eine Stromrechnung, dann ein Schreiben der Schule).
Hinweise, dass Seiten zusammengehören:
- fortlaufende Seitenzahlen ("Seite 2 von 3"), gleiche Kunden-, Rechnungs- oder Aktenzeichen und gleiches Datum, ein Satz, der auf der nächsten Seite weitergeht,
- Anlagen, Beiblätter, Hinweise, AGB oder Formulare, die erkennbar zu dem Schreiben davor gehören,
- leere oder fast leere Seiten (z. B. Rückseiten beim beidseitigen Scannen) gehören zum Dokument davor,
- ein Blattzähler der Versanddruckerei am Seitenrand, z. B. "18210860 0005/0044 0000853 18.05.2026": gleiche Auftrags-/Sendungsnummer und fortlaufender Zähler "x/N" bedeuten EIN Brief mit N Blättern. Alle Blätter dieser Sendung gehören zu einem Dokument, auch wenn einzelne Teile eigene Titel, eigene Fassungsdaten oder eigene Seitenzahlen haben.

Beilagen gehören zum Anschreiben: Allgemeine Geschäftsbedingungen, Sonderbedingungen, Preis- und Leistungsverzeichnisse, Broschüren, Merkblätter, Formulare, Belege und Einzelabrechnungen (z. B. die WEG- oder Heizkostenabrechnung hinter einer Nebenkostenabrechnung des Vermieters) sind KEIN eigenes Dokument, wenn sie hinter einem Schreiben liegen, das sie erwähnt oder zu dem sie thematisch gehören – auch wenn sie von einem anderen Unternehmen stammen oder eigene Überschriften haben. Ein neues Dokument beginnt erst mit einem neuen eigenständigen Schreiben bzw. Beleg, das an uns adressiert ist (eigenes Adressfeld oder eigenes Ausstellungsdatum) und nicht Beilage des vorigen ist.

Regeln:
- dokumente: Liste der Dokumente in Seitenreihenfolge, je mit erster Seite (von), letzter Seite (bis) und einer kurzen Beschreibung (z. B. "Stromabrechnung Stadtwerke März 2026"). Die Bereiche müssen lückenlos alle gezeigten Seiten von der ersten bis zur letzten abdecken, aufsteigend sortiert, und dürfen sich nicht überschneiden: Jede Seite gehört zu genau einem Bereich.
- Bei langen Scans siehst du nur einen Ausschnitt. Die erste gezeigte Seite kann die Fortsetzung eines Dokuments von früheren Seiten sein; beginne den ersten Bereich trotzdem bei der ersten gezeigten Seite. Fehlt in deinem Ausschnitt das Anschreiben, trenne Beilagen (z. B. mehrere Bedingungswerke hintereinander) trotzdem nicht voneinander.
- Ist alles ein einziges Dokument, gib genau einen Bereich von der ersten bis zur letzten gezeigten Seite zurück. Im Zweifel nicht trennen.
- Gleichartige Schreiben desselben Absenders mit unterschiedlichem Datum (z. B. Kontoauszüge oder Lohnabrechnungen verschiedener Monate, zwei Bescheide) sind getrennte Dokumente.
- begruendung: ein Satz, woran du die Grenzen erkannt hast.
- confidence: deine Sicherheit zwischen 0 und 1, dass die Aufteilung genau stimmt. Hoch (0.9 oder mehr), wenn jedes Dokument eindeutig mit eigenem Adressfeld bzw. Briefkopf und eigenem Datum beginnt, z. B. Lohnsteuerbescheinigungen verschiedener Jahre oder Schreiben verschiedener Absender hintereinander. Niedrig, wenn du Beilagen von eigenständigen Dokumenten nicht sicher unterscheiden kannst. Sei ehrlich."""


def split_schema(first, last):
    page = {"type": "integer", "minimum": first, "maximum": last}
    return {
        "type": "object",
        "properties": {
            "dokumente": {"type": "array", "minItems": 1, "items": {
                "type": "object",
                "properties": {"von": page, "bis": page, "beschreibung": {"type": "string"}},
                "required": ["von", "bis", "beschreibung"]}},
            "begruendung": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["dokumente", "begruendung", "confidence"],
    }


def page_texts(pdf_bytes):
    return [page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf_bytes)).pages]


def clip_page(text, limit):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + " [...] " + text[-(limit - head):]


def build_split_prompt(doc, pages, first=1, last=None):
    n = len(pages)
    last = last or n
    shown = last - first + 1
    limit = max(200, min(SPLIT_PAGE_CHARS, SPLIT_TOTAL_CHARS // shown))
    body = "\n\n".join(f"=== Seite {i} von {n} ===\n{clip_page(pages[i - 1], limit) or '(leer)'}"
                       for i in range(first, last + 1))
    window = (f"\nGezeigter Ausschnitt: Seiten {first} bis {last}. Deine Bereiche müssen genau "
              f"diese Seiten abdecken." if shown < n else "")
    return f"""Titel: {doc.get('title') or ''}
Originaldateiname: {doc.get('original_file_name') or ''}
Anzahl Seiten: {n}{window}

{body}"""


def check_ranges(result, first, last):
    """([(von, bis, description), ...], problems). The ranges are valid (problems == []) if they
    cover pages first..last in order, each page exactly once."""
    try:
        raw = [(int(d["von"]), int(d["bis"]), str(d.get("beschreibung") or "").strip())
               for d in result.get("dokumente") or []]
    except (KeyError, TypeError, ValueError):
        return [], ["Antwort enthält keine gültigen Seitenzahlen"]
    if not raw:
        return [], ["keine Bereiche angegeben"]
    problems = []
    for a, b, _ in raw:
        if b < a:
            problems.append(f"Bereich {a}-{b} endet vor seinem Anfang")
        if a < first or b > last:
            problems.append(f"Bereich {a}-{b} liegt außerhalb der Seiten {first}-{last}")
    covered = {}
    for a, b, _ in raw:
        for p in range(max(a, first), min(b, last) + 1):
            covered.setdefault(p, []).append(fmt_range(a, b))
    overlaps = sorted(p for p, r in covered.items() if len(r) > 1)
    gaps = [p for p in range(first, last + 1) if p not in covered]
    if overlaps:
        problems.append(f"Seite(n) {fmt_pages(overlaps)} mehrfach vergeben "
                        f"(Bereiche {', '.join(dict.fromkeys(r for p in overlaps for r in covered[p]))})")
    if gaps:
        problems.append(f"Seite(n) {fmt_pages(gaps)} keinem Dokument zugeordnet")
    return sorted(raw), problems


def fmt_range(first, last):
    return str(first) if first == last else f"{first}-{last}"


def fmt_pages(pages):
    """[1, 2, 3, 7] -> "1-3, 7"."""
    out, start = [], None
    for i, p in enumerate(pages):
        if start is None:
            start = p
        if i + 1 == len(pages) or pages[i + 1] != p + 1:
            out.append(fmt_range(start, p))
            start = None
    return ", ".join(out)


def split_proposal_lines(result, ranges):
    lines = [f"Seiten {fmt_range(a, b)}: {desc}" for a, b, desc in ranges]
    if result.get("begruendung"):
        lines.append(f"Begründung: {result['begruendung']}")
    return lines


def ask_split(args, doc, pages, first, last):
    """One split check of pages first..last, with one correction round if the ranges are
    invalid. Returns (result, ranges, problems, confidence)."""
    schema = split_schema(first, last)
    user = build_split_prompt(doc, pages, first, last)
    followup = []
    for attempt in range(2):
        result, st = ask_llm(args.model, schema, SPLIT_SYSTEM_PROMPT, user,
                             num_ctx=SPLIT_NUM_CTX, followup=followup)
        log.info("Document %s split check (pages %d-%d): %d prompt tok in %.1fs, %d gen tok in %.1fs",
                 doc["id"], first, last, st["prompt_tokens"], st["prompt_s"], st["gen_tokens"], st["gen_s"])
        log.debug("Document %s split output: %s", doc["id"], json.dumps(result, ensure_ascii=False))
        ranges, problems = check_ranges(result, first, last)
        if not problems:
            break
        log.info("Document %s: invalid split ranges (%s)%s", doc["id"], "; ".join(problems),
                 ", asking again" if attempt == 0 else "")
        followup = [{"role": "assistant", "content": json.dumps(result, ensure_ascii=False)},
                    {"role": "user", "content": (
                        f"Diese Aufteilung ist ungültig: {'; '.join(problems)}. Jede Seite von "
                        f"{first} bis {last} muss zu genau einem Bereich gehören, die Bereiche "
                        f"aufsteigend und ohne Überschneidung. Gib die korrigierte Aufteilung zurück.")}]
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0
    return result, ranges, problems, confidence


# Sheet counter printed by mail-processing printers (e.g. on bank mailings):
# "<job> <sheet>/<sheets> <recipient> <date> <page serial>", e.g. "18210860 0005/0044 0000853 18.05.2026 0074967".
MAILING_RE = re.compile(r"\b(\d{8})\s+(\d{4})/(\d{4})\s+(\d{7})\s+\d{2}\.\d{2}\.\d{4}\b")


def single_mailing(pages):
    """True if the whole scan is one mailing by its sheet counter: the first page is sheet 1, every
    counter has the same job/recipient/sheet count, sheets never go backwards, the last sheet is
    reached and at most one page (its back) follows it."""
    hits = []
    for i, text in enumerate(pages):
        m = MAILING_RE.search(re.sub(r"\s+", " ", text or ""))
        if m:
            hits.append((i, m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)))
    if len(hits) < 2 or hits[0][0] != 0 or hits[0][2] != 1:
        return False
    keys = {(job, total, rcpt) for _, job, _, total, rcpt in hits}
    sheets = [sheet for _, _, sheet, _, _ in hits]
    return (len(keys) == 1 and sheets == sorted(sheets) and sheets[-1] == hits[0][3]
            and len(pages) - 1 - hits[-1][0] <= 1)


def detect_split(args, doc, pages):
    """Short scans are checked in one go; long ones in windows of SPLIT_WINDOW_PAGES that overlap
    by one page, so each page keeps enough text. A window's first page is a document start only
    if the previous window says so. Returns (result, ranges, problems, confidence)."""
    n = len(pages)
    if single_mailing(pages):
        log.info("Document %s: sheet counter shows one mailing of %d pages, no split check", doc["id"], n)
        return ({"begruendung": "Blattzähler der Versanddruckerei: eine Sendung"},
                [(1, n, "eine Sendung (Blattzähler)")], [], 1.0)
    if n <= SPLIT_WINDOW_PAGES:
        return ask_split(args, doc, pages, 1, n)
    starts, reasons, problems, confidence = {}, [], [], 1.0
    first = 1
    while True:
        last = min(n, first + SPLIT_WINDOW_PAGES - 1)
        result, ranges, window_problems, conf = ask_split(args, doc, pages, first, last)
        confidence = min(confidence, conf)
        problems += [f"Seiten {first}-{last}: {p}" for p in window_problems]
        for a, _, desc in ranges:
            if a > first or first == 1:
                starts.setdefault(a, desc)
        if result.get("begruendung"):
            reasons.append(f"Seiten {first}-{last}: {result['begruendung']}")
        if last == n:
            break
        first = last  # overlap by one page
    if problems:
        return {"dokumente": [], "begruendung": " ".join(reasons)}, [], problems, confidence
    firsts = sorted(starts)
    ranges = [(a, (firsts[i + 1] - 1) if i + 1 < len(firsts) else n, starts[a])
              for i, a in enumerate(firsts)]
    return {"begruendung": " ".join(reasons)}, ranges, [], confidence


def fetch_by_ids(pl, ids):
    return pl.get_all("documents/", {"id__in": ",".join(map(str, ids)), "ordering": "-added"})


def split_candidates(pl, ctx, ids, limit, ignore_filter=False):
    params = {"tags__id__all": ctx["inbox_id"], "ordering": "-added"}
    if ctx["review_id"] is not None:
        params["tags__id__none"] = ctx["review_id"]
    docs = [d for d in (fetch_by_ids(pl, ids) if ignore_filter else pl.get_all("documents/", params))
            if (d.get("page_count") or 0) > 1 and d.get("mime_type") == "application/pdf"
            # Parts of an earlier split are not split again.
            and not SPLIT_TITLE_RE.search(d["title"])
            and (not ids or d["id"] in ids)]
    return docs[:limit] if limit else docs


def preview_part(doc, pages, idx, first, last):
    """A stand-in for a split part, so a dry run can show the tags it would get."""
    return dict(doc, id=f"{doc['id']}/{idx}", title=f"{doc['title']} (split {idx})",
                content="\n\n".join(pages[first - 1:last]), page_count=last - first + 1,
                original_file_name=f"{doc['id']}_{first}-{last}.pdf")


def part_filename(doc, first, last):
    # Paperless names the temporary file of each part like this; it becomes the part's
    # original file name, which is how the consumed parts are found again.
    return f"{doc['id']}_{first}-{last}.pdf"


def run_split_step(pl, args, ctx):
    """Step 1. Returns stats, the ids of originals the tag step must skip, (dry run only)
    stand-in documents for the proposed parts, and {id: note} of scans the step couldn't decide;
    the tag step flags those with "Prüfen" and the note."""
    stats = {"checked": 0, "split": 0, "parts": 0, "review": 0, "failed": 0, "waiting": 0}
    skip_ids, previews, review_notes = set(), [], {}
    pending, handled = finish_pending_splits(pl, ctx, stats, args.dry_run)
    # Originals of earlier split requests (waiting, or finished just now) are neither split
    # again nor tagged.
    skip_ids |= {int(k) for k in handled}
    candidates = [d for d in split_candidates(pl, ctx, args.doc, args.limit,
                                              ignore_filter=args.dry_run and bool(args.doc))
                  if d["id"] not in skip_ids]
    log.info("Split step: %d multi-page Inbox document(s) to check", len(candidates))
    if candidates:
        wait_for_ollama()

    for doc in candidates:
        try:
            pages = page_texts(pl.download(doc["id"]))
            n = len(pages)
            if n != doc["page_count"]:
                log.warning("Document %s: PDF has %d pages, Paperless says %s; not splitting",
                            doc["id"], n, doc["page_count"])
                continue
            result, ranges, problems, confidence = detect_split(args, doc, pages)
            stats["checked"] += 1

            if not problems and len(ranges) == 1 and n <= LONG_SCAN_PAGES:
                log.info("Document %s: %d pages, one document, not split", doc["id"], n)
                continue

            lines = split_proposal_lines(result, ranges)
            if problems:
                status = "Aufteilungsvorschlag ungültig, nicht aufgeteilt, Tag \"Prüfen\" + Notiz"
                lines = [f"Ungültig: {p}" for p in problems] + lines
                note = (f"{NOTE_PREFIX} Dieser Scan enthält möglicherweise mehrere Dokumente, aber der "
                        f"Aufteilungsvorschlag des Modells ist ungültig und wurde NICHT ausgeführt. "
                        f"Bitte selbst prüfen und ggf. in Paperless aufteilen.\n- " + "\n- ".join(lines))
            elif len(ranges) == 1:
                status = "langer Scan als ein Dokument erkannt, Tag \"Prüfen\" + Notiz"
                note = (f"{NOTE_PREFIX} Langer Scan ({n} Seiten), vom Modell als ein einziges Dokument "
                        f"erkannt (Sicherheit {confidence:.2f}). Bitte prüfen, ob er mehrere Dokumente "
                        f"enthält, und ihn ggf. in Paperless aufteilen.\n- " + "\n- ".join(lines))
            else:
                status = None if confidence >= SPLIT_CONFIDENCE else "Aufteilung unsicher, Tag \"Prüfen\" + Notiz"
                note = (f"{NOTE_PREFIX} Dieser Scan enthält möglicherweise mehrere Dokumente "
                        f"(Sicherheit {confidence:.2f}). Vorgeschlagene Aufteilung:\n- " + "\n- ".join(lines))
            do_split = status is None
            desc = "\n".join(
                [f"Dokument {doc['id']}: \"{doc['title']}\" ({n} Seiten): "
                 + ("wird aufgeteilt" if do_split else status)
                 + f" (Sicherheit {confidence:.2f})"] + [f"    {line}" for line in lines])
            for line in desc.splitlines():
                log.debug(line)
            if args.dry_run:
                if args.json:
                    ctx["report"].append({"id": doc["id"], "aufteilung": {
                        "seiten": n, "wird_aufgeteilt": do_split, "status": status or "wird aufgeteilt",
                        "bereiche": [fmt_range(a, b) for a, b, _ in ranges] if not problems else [],
                        "probleme": problems, "sicherheit": round(confidence, 2),
                        "notiz": None if do_split else note}})
                else:
                    print(desc, flush=True)

            if do_split:
                skip_ids.add(doc["id"])
                if args.dry_run:
                    previews += [preview_part(doc, pages, i, a, b)
                                 for i, (a, b, _) in enumerate(ranges, 1)]
                    stats["split"] += 1
                    stats["parts"] += len(ranges)
                    continue
                spec = ",".join(fmt_range(a, b) for a, b, _ in ranges)
                pl.post("documents/bulk_edit/", {"documents": [doc["id"]], "method": "split",
                                                 "parameters": {"pages": spec,
                                                                "delete_originals": False}})
                log.info("Document %s: split into %s requested", doc["id"], spec)
                pending[str(doc["id"])] = {
                    "parts": [part_filename(doc, a, b) for a, b, _ in ranges], "lines": lines,
                    "requested": dt.datetime.now().isoformat(timespec="seconds")}
                save_pending_splits(pending)
                stats["waiting"] += 1
            else:
                stats["review"] += 1
                review_notes[doc["id"]] = note
        except Exception:
            stats["failed"] += 1
            log.exception("Document %s: split step failed", doc.get("id"))

    return stats, skip_ids, previews, review_notes


def load_pending_splits():
    try:
        with open(PENDING_SPLITS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_pending_splits(pending):
    tmp = PENDING_SPLITS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PENDING_SPLITS_FILE)


def finish_pending_splits(pl, ctx, stats, dry_run):
    """Splits requested in earlier runs: once Paperless has consumed all parts, move the original
    to the trash; if parts are still missing after SPLIT_PENDING_MAX_S, keep it and flag it.
    Returns the entries still waiting and the ids of all originals looked at (a dry run only
    reports and changes nothing)."""
    pending = load_pending_splits()
    if not pending:
        return pending, set()
    still = {}
    for oid, entry in pending.items():
        try:
            hits = pl.get("documents/", {"id__in": oid})["results"]
            if not hits:
                log.info("Document %s: original is gone (deleted by hand), split done", oid)
                continue
            doc = hits[0]
            found = {}
            for name in entry["parts"]:
                hits = pl.get("documents/", {"original_filename__iexact": name})["results"]
                if hits:
                    found[name] = hits[0]["id"]
            age = (dt.datetime.now() - dt.datetime.fromisoformat(entry["requested"])).total_seconds()
            if len(found) == len(entry["parts"]):
                ids = [found[n] for n in entry["parts"]]
                if not dry_run:
                    pl.delete(f"documents/{oid}/")
                log.info("Document %s: all %d parts exist (%s), original %s", oid, len(ids),
                         ", ".join(map(str, ids)), "would be moved to the trash" if dry_run
                         else "moved to the trash")
                stats["split"] += 1
                stats["parts"] += len(ids)
            elif age > SPLIT_PENDING_MAX_S:
                have = [str(found[n]) for n in entry["parts"] if n in found]
                log.warning("Document %s: only %d of %d parts after %.0f h, keeping original",
                            oid, len(have), len(entry["parts"]), age / 3600)
                stats["review"] += 1
                if not dry_run:
                    flag_for_review(pl, ctx, doc, (
                        f"{NOTE_PREFIX} Aufteilung gestartet, aber nach {age / 3600:.0f} Std. waren erst "
                        f"{len(have)} von {len(entry['parts'])} Teilen da"
                        + (f" (Dokumente {', '.join(have)})" if have else "")
                        + ". Das Original wurde behalten. Bitte prüfen und das Original löschen, "
                          "wenn alle Teile vollständig sind.\nAufteilung:\n- " + "\n- ".join(entry["lines"])))
            else:
                log.info("Document %s: %d of %d split parts consumed so far, waiting",
                         oid, len(found), len(entry["parts"]))
                stats["waiting"] += 1
                still[oid] = entry
        except Exception:
            stats["failed"] += 1
            log.exception("Document %s: finishing split failed, will retry", oid)
            still[oid] = entry
    if not dry_run:
        save_pending_splits(still)
    return still, set(pending)


# --- Step 2: tag ------------------------------------------------------------------------------

def fresh_copy(doc, ctx):
    """--fresh (dry run only): the document as it was at consumption, before any tagger run:
    only Inbox/mail tags, no document type or custom fields, the file name as title, and only a
    correspondent that Paperless' own auto-matching assigns."""
    corr = next((c for c in ctx["correspondents"].values() if c["id"] == doc["correspondent"]), None)
    keep = {ctx["inbox_id"]} | {t["id"] for t in ctx["tags"].values() if t["name"].casefold() == MAIL_TAG}
    name = doc.get("original_file_name") or doc["title"]
    return dict(doc, tags=[t for t in doc["tags"] if t in keep] or [ctx["inbox_id"]], document_type=None,
                custom_fields=[], title=re.sub(r"\.pdf$", "", name, flags=re.I),
                correspondent=doc["correspondent"] if corr and corr.get("matching_algorithm") else None)


def plan_empty(doc, ctx, extra_reasons=()):
    """No OCR text: nothing for the model to read, so only flag the document."""
    reasons = [*extra_reasons, "Kein Text erkannt – Original prüfen"]
    patch = {"tags": list(doc["tags"]) + ["REVIEW"]}
    note = f"{NOTE_PREFIX} " + ("Kein Text erkannt – Original prüfen" if len(reasons) == 1
                                else "Zur Prüfung markiert:\n- " + "\n- ".join(reasons))
    return patch, note, True, 0.0, None, {"reasons": reasons, "duplicates": []}


def apply_correspondent(pl, ctx, patch):
    """Creates a correspondent proposed with AUTO_CREATE_CORRESPONDENT_MIN_CONFIDENCE. Matching
    stays off, so Paperless never assigns it on its own."""
    value = patch.get("correspondent")
    if not isinstance(value, dict):
        return
    existing = ctx["correspondents"].get(value["create"].casefold())
    if not existing:
        existing = pl.post("correspondents/", {"name": value["create"], "matching_algorithm": 0})
        ctx["correspondents"][existing["name"].casefold()] = existing
        log.info("Created correspondent %r (id %s)", existing["name"], existing["id"])
    patch["correspondent"] = existing["id"]


def tag_document(pl, args, ctx, schema, names, doc, split_note=None):
    """Ask the model about one document and apply (or, in a dry run, print) the result.
    `split_note`: the split step couldn't decide; the document gets "Prüfen" and that note too.
    Returns (needs_review, frist, title)."""
    corr_names, type_names, choosable_tags = names
    extra = ["Möglicherweise mehrere Dokumente in einem Scan (siehe Notiz zur Aufteilung)"] if split_note else []
    text, truncated = truncate(doc.get("content"))
    if len(re.sub(r"\W", "", clean_text(text))) < MIN_TEXT_CHARS:
        log.warning("Document %s has no OCR text, not asking the model", doc["id"])
        patch, note, needs_review, confidence, frist, info = plan_empty(doc, ctx, extra)
    else:
        user = build_user_prompt(doc, text, truncated, corr_names, type_names, choosable_tags,
                                 dt.date.today(), examples_section(similar_examples(doc, ctx), ctx)
                                 + paperless_suggestions(pl, doc, ctx))
        result, st = ask_llm(args.model, schema, SYSTEM_PROMPT, user)
        log.info("Document %s: %d prompt tok in %.1fs, %d gen tok in %.1fs",
                 doc["id"], st["prompt_tokens"], st["prompt_s"], st["gen_tokens"], st["gen_s"])
        log.debug("Document %s model output: %s", doc["id"], json.dumps(result, ensure_ascii=False))
        patch, note, needs_review, confidence, frist, info = plan_changes(doc, result, ctx, extra)
    if not args.dry_run and "tags" in patch:
        rid = ensure_review_tag(pl, ctx, args.dry_run)
        patch["tags"] = list(dict.fromkeys(rid if t == "REVIEW" else t for t in patch["tags"]))

    desc = describe(doc, patch, note, ctx, needs_review, confidence, frist)
    for line in desc.splitlines():
        log.debug(line)
    if args.dry_run:
        if args.json:
            entry = proposal(doc, patch, note, ctx, needs_review, confidence, frist, info)
            if split_note:
                entry["notiz_aufteilung"] = split_note
            ctx["report"].append(entry)
        else:
            print(desc, flush=True)
    else:
        apply_correspondent(pl, ctx, patch)
        if patch:
            pl.patch(f"documents/{doc['id']}/", patch)
        if split_note:
            pl.post(f"documents/{doc['id']}/notes/", {"note": split_note})
        if note:
            pl.post(f"documents/{doc['id']}/notes/", {"note": note})
    return needs_review, frist, (patch.get("title") or doc["title"])


# --- One-off: family tags on all documents ----------------------------------------------------

def run_backfill_shared(pl, args, ctx, start):
    """Only adds the child, lease and partner tags to existing documents; changes nothing else.
    A dry run writes the plan to BACKFILL_PLAN, which --apply-plan applies without asking
    the model again, so exactly the confirmed changes are made."""
    tag_names = {v["id"]: v["name"] for v in ctx["tags"].values()}
    if args.apply_plan:
        with open(args.apply_plan, encoding="utf-8") as f:
            plan = json.load(f)
        log.info("Applying %d planned document(s) from %s", len(plan), args.apply_plan)
    else:
        docs = pl.get_all("documents/", {"ordering": "-added"})
        if args.doc:
            docs = [d for d in docs if d["id"] in args.doc]
        if args.limit:
            docs = docs[:args.limit]
        log.info("%d document(s) to check for family tags", len(docs))
        if docs:
            wait_for_ollama()
        plan, checked, unsure, failed = [], 0, 0, 0
        for doc in docs:
            try:
                text, truncated = truncate(doc.get("content"))
                result, st = ask_llm(args.model, SHARED_SCHEMA, SHARED_SYSTEM_PROMPT,
                                     build_shared_prompt(doc, text or "(kein Text erkannt)", truncated))
                log.info("Document %s: %d prompt tok in %.1fs, %d gen tok in %.1fs",
                         doc["id"], st["prompt_tokens"], st["prompt_s"], st["gen_tokens"], st["gen_s"])
                log.debug("Document %s model output: %s", doc["id"], json.dumps(result, ensure_ascii=False))
                checked += 1
                ids, child_unclear = plan_shared_tags(doc, result, ctx)
                add = [tag_names[t] for t in ids if t not in doc["tags"]]
                try:
                    confidence = max(0.0, min(1.0, float(result.get("confidence"))))
                except (TypeError, ValueError):
                    confidence = 0.0
                if not add:
                    continue
                line = (f"Dokument {doc['id']}: \"{doc['title']}\"  +{', +'.join(add)}  "
                        f"(Sicherheit {confidence:.2f}"
                        + (", Kind unklar" if child_unclear else "") + ")")
                if confidence < CONFIDENCE_THRESHOLD:
                    unsure += 1
                    line += "  -> unsicher, wird nicht übernommen"
                else:
                    plan.append({"id": doc["id"], "title": doc["title"], "add": add,
                                 "child_unclear": child_unclear, "confidence": confidence})
                log.debug(line)
                if args.dry_run:
                    print(line, flush=True)
            except Exception:
                failed += 1
                log.exception("Document %s failed", doc.get("id"))

    counts = {n: sum(n in e["add"] for e in plan) for n in (CHILD_TAG, *CHILDREN, LEASE_TAG, *([PARTNER_TAG] if PARTNER_TAG else []))}
    unclear = sum(e["child_unclear"] for e in plan)
    if args.dry_run:
        with open(BACKFILL_PLAN, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=1)
    else:
        by_tag = {}
        for e in plan:
            for name in e["add"]:
                by_tag.setdefault(name, []).append(e["id"])
        for name, ids in by_tag.items():
            tag = ctx["tags"].get(name.casefold())
            if not tag:
                log.warning("Tag %r missing in Paperless, not created", name)
                continue
            # add_tag only ever adds; other tags, fields, owners and permissions stay as they are.
            for i in range(0, len(ids), 100):
                pl.post("documents/bulk_edit/", {"documents": ids[i:i + 100], "method": "add_tag",
                                                 "parameters": {"tag": tag["id"]}})
            log.info("Added tag %r to %d document(s): %s", name, len(ids), ids)

    summary = (f"Paperless-Tagger Familien-Tags{' (Testlauf, nichts geschrieben)' if args.dry_run else ''}: "
               + (f"{checked} Dokumente geprüft in {(time.time() - start) / 60:.0f} Min., "
                  if not args.apply_plan else "")
               + f"{len(plan)} Dokument{'e' if len(plan) != 1 else ''} "
               + ("bekämen" if args.dry_run else "erhielten") + " Tags ("
               + ", ".join(f"{n} {c}" for n, c in counts.items()) + ")"
               + (f", davon {unclear} mit unklarem Kind (nur \"{CHILD_TAG}\")" if unclear else ""))
    if not args.apply_plan:
        summary += (f", {unsure} wegen Unsicherheit übersprungen"
                    + (f", {failed} fehlgeschlagen (siehe {LOG_FILE})" if failed else ""))
    summary += "."
    if args.dry_run:
        summary += f" Plan gespeichert in {BACKFILL_PLAN}; übernehmen mit --backfill-shared --apply-plan."
    log.info("Summary: %s", summary)
    print(summary, flush=True)
    return 0


def summarize(processed, review, failed, deadlines, dry_run, elapsed, split_stats=None):
    today = dt.date.today()
    upcoming = sorted((d, t) for d, t in deadlines if d >= today)
    prefix = "Paperless-Tagger (Testlauf, nichts geschrieben): " if dry_run else "Paperless-Tagger: "
    split_text = ""
    # Splits only waiting for their parts are not news on their own (the runner stays quiet then).
    if split_stats and (split_stats["split"] or split_stats["review"] or split_stats["failed"]):
        s = split_stats
        split_text = (f"{s['split']} Sammelscan{'s' if s['split'] != 1 else ''} in {s['parts']} "
                      f"Dokumente aufgeteilt" + (f", {s['review']} Aufteilung(en) zur Prüfung"
                                                 if s["review"] else "")
                      + (f", {s['waiting']} Aufteilung(en) in Arbeit" if s.get("waiting") else "")
                      + (f", {s['failed']} Aufteilung(en) fehlgeschlagen" if s["failed"] else "")
                      + "; ")
    if processed == 0 and failed == 0:
        return prefix + split_text + "Keine neuen Dokumente im Inbox."
    parts = [f"{processed} Dokument{'e' if processed != 1 else ''} verarbeitet "
             f"in {elapsed / 60:.0f} Min., davon {review} zur Prüfung markiert (Tag \"{REVIEW_TAG}\")"]
    if failed:
        parts.append(f"{failed} fehlgeschlagen (siehe {LOG_FILE})")
    text = prefix + split_text + "; ".join(parts) + "."
    if upcoming:
        text += " Anstehende Fristen: " + "; ".join(
            f"{d.strftime('%d.%m.%Y')} {t}" for d, t in upcoming[:10])
        if len(upcoming) > 10:
            text += f" (und {len(upcoming) - 10} weitere)"
        text += "."
    else:
        text += " Keine anstehenden Fristen."
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show proposed changes, write nothing")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N documents (per step: split check and tagging)")
    ap.add_argument("--doc", type=int, action="append", default=[], metavar="ID",
                    help="only these documents (repeatable); the tag step also takes their split parts")
    ap.add_argument("--backfill", action="store_true",
                    help="go over already processed documents and only fill empty correspondent/"
                         "document type")
    ap.add_argument("--backfill-shared", action="store_true",
                    help=f"one-off: go over ALL documents and only add the tags {CHILD_TAG}/"
                         f"{'/'.join(CHILDREN)}/{LEASE_TAG}; use with --dry-run first")
    ap.add_argument("--apply-plan", nargs="?", const=BACKFILL_PLAN, metavar="FILE",
                    help=f"with --backfill-shared: apply the plan of a dry run (default {BACKFILL_PLAN})")
    ap.add_argument("--json", action="store_true",
                    help="with --dry-run: print the proposed fields as JSON (summary goes to stderr)")
    ap.add_argument("--fresh", action="store_true",
                    help="with --dry-run: treat documents as unprocessed (only Inbox/mail tags, no type, "
                         "custom fields or tagger-set correspondent, file name as title)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--backend", choices=("ollama", "openai"), default="ollama",
                    help="ollama: native Ollama API (default); openai: OpenAI-compatible endpoint")
    ap.add_argument("--llm-url", help=f"LLM base URL (ollama default {OLLAMA_URL}; openai e.g. "
                                      "https://openrouter.ai/api/v1)")
    ap.add_argument("--api-key-env", default="LLM_API_KEY",
                    help="environment variable holding the API key (openai backend)")
    ap.add_argument("--llm-extra", default="{}", metavar="JSON",
                    help='extra request fields for the openai backend, e.g. '
                         '\'{"provider": {"zdr": true, "data_collection": "deny"}}\'')
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.apply_plan and (not args.backfill_shared or args.dry_run):
        ap.error("--apply-plan needs --backfill-shared and no --dry-run")
    if args.backfill and args.backfill_shared:
        ap.error("--backfill and --backfill-shared are separate runs")
    if (args.json or args.fresh) and not args.dry_run:
        ap.error("--json and --fresh need --dry-run")
    if _household_path != HOUSEHOLD_FILE and not args.dry_run:
        ap.error(f"{HOUSEHOLD_FILE} missing (copy household.example.json); "
                 "only a --dry-run may use the example household")

    LLM["backend"] = args.backend
    if args.llm_url:
        LLM["url"] = args.llm_url.rstrip("/")
    elif args.backend == "openai":
        ap.error("--backend openai needs --llm-url")
    LLM["api_key"] = os.environ.get(args.api_key_env) or None
    LLM["extra"] = json.loads(args.llm_extra)

    setup_logging(args.verbose)
    env = load_env(ENV_FILE)
    pl = Paperless(env["PAPERLESS_URL"], env["PAPERLESS_TOKEN"])
    start = time.time()
    log.info("Start (dry_run=%s, limit=%s, doc=%s, backfill=%s, backfill_shared=%s, backend=%s, model=%s)",
             args.dry_run, args.limit, args.doc, args.backfill, args.backfill_shared, args.backend, args.model)

    tags = by_name(pl.get_all("tags/"))
    ctx = {
        "tags": tags,
        "correspondents": by_name(pl.get_all("correspondents/")),
        "doc_types": by_name(pl.get_all("document_types/")),
        "custom_fields": {c["name"]: c["id"] for c in pl.get_all("custom_fields/")},
        "inbox_id": tags.get(INBOX_TAG.casefold(), {}).get("id"),
        "review_id": tags.get(REVIEW_TAG.casefold(), {}).get("id"),
        "report": [],
    }
    if ctx["inbox_id"] is None:
        raise SystemExit(f"Tag {INBOX_TAG!r} not found in Paperless")
    for name in (CF_BETRAG, CF_FRIST, CF_RECHNUNGSNUMMER):
        if name not in ctx["custom_fields"]:
            log.warning("Custom field %r missing in Paperless, will be skipped", name)
    for name in (CHILD_TAG, *CHILDREN, LEASE_TAG):
        if name.casefold() not in tags:
            log.warning("Tag %r missing in Paperless, will be skipped (never created)", name)

    if args.backfill_shared:
        return run_backfill_shared(pl, args, ctx, start)

    split_stats, skip_ids, previews, split_notes = None, set(), [], {}
    if not args.backfill:
        split_stats, skip_ids, previews, split_notes = run_split_step(pl, args, ctx)

    # Documents already marked "Prüfen" are waiting for a human; don't redo them weekly.
    params = {"tags__id__all": ctx["inbox_id"], "ordering": "-added"}
    if ctx["review_id"] is not None:
        params["tags__id__none"] = ctx["review_id"]
    if args.dry_run and args.doc and not args.backfill:
        # Testing given documents: whatever their tags, nothing is written anyway.
        docs = fetch_by_ids(pl, args.doc)
    elif args.backfill:
        # Everything the normal run is done with: out of Inbox, or waiting in "Prüfen".
        docs = [d for d in pl.get_all("documents/", {"ordering": "-added"})
                if (ctx["inbox_id"] not in d["tags"] or ctx["review_id"] in d["tags"])
                and needs_backfill(d, ctx)]
        if args.limit:
            docs = docs[:args.limit]
    elif args.limit and not args.doc:
        params["page_size"] = args.limit
        docs = pl.get("documents/", params)["results"][:args.limit]
    else:
        docs = pl.get_all("documents/", params)
    if args.doc and not args.backfill and not args.dry_run:
        # The requested documents, plus the parts split off them in this run.
        part_re = re.compile(rf"^({'|'.join(map(str, args.doc))})_\d+-\d+\.pdf$")
        docs = [d for d in docs if d["id"] in args.doc
                or part_re.match(d.get("original_file_name") or "")]
    # Originals that were (or in a dry run would be) split are not tagged; their parts are.
    docs = previews + [d for d in docs if d["id"] not in skip_ids]
    if args.limit and not args.backfill:
        docs = docs[:args.limit]
    log.info("%d document(s) to process", len(docs))

    if args.fresh:
        docs = [fresh_copy(d, ctx) for d in docs]
    choosable_tags = [t["name"] for t in tags.values()
                      if t["name"].casefold() not in WORKFLOW_TAGS | SHARED_TAGS
                      and t["name"] in TAG_DEFINITIONS]
    undefined = [t["name"] for t in tags.values() if t["name"].casefold() not in WORKFLOW_TAGS | SHARED_TAGS
                 and t["name"] not in TAG_DEFINITIONS]
    if undefined:
        log.warning("Tags without a definition in TAG_DEFINITIONS, not offered to the model: %s", undefined)
    corr_names = [c["name"] for c in ctx["correspondents"].values() if not is_household(c["name"])]
    type_names = [t["name"] for t in ctx["doc_types"].values()]
    schema = build_schema(corr_names, type_names, choosable_tags)

    if docs:
        wait_for_ollama()
        all_docs = pl.get_all("documents/", {"fields": ALL_DOC_FIELDS})
        build_example_index(all_docs, ctx)
        if not args.backfill:
            ctx["dup_index"] = build_dup_index(all_docs, ctx)

    processed = review = failed = changed = 0
    deadlines = []
    for doc in docs:
        try:
            if args.backfill:
                text, truncated = truncate(doc.get("content"))
                if not text:
                    log.warning("Document %s has no OCR text", doc["id"])
                user = build_user_prompt(doc, text or "(kein Text erkannt)", truncated,
                                         corr_names, type_names, choosable_tags, dt.date.today(),
                                         examples_section(similar_examples(doc, ctx), ctx))
                result, st = ask_llm(args.model, schema, SYSTEM_PROMPT, user)
                log.info("Document %s: %d prompt tok in %.1fs, %d gen tok in %.1fs",
                         doc["id"], st["prompt_tokens"], st["prompt_s"], st["gen_tokens"], st["gen_s"])
                log.debug("Document %s model output: %s", doc["id"], json.dumps(result, ensure_ascii=False))
                patch, confidence, unsure = plan_backfill(doc, result, ctx)
                desc = describe(doc, patch, None, ctx, False, confidence, None)
                if unsure:
                    desc += "\n    (unsicher, nichts übernommen)"
                for line in desc.splitlines():
                    log.debug(line)
                if args.dry_run:
                    print(desc, flush=True)
                elif patch:
                    pl.patch(f"documents/{doc['id']}/", patch)
                processed += 1
                review += unsure
                changed += bool(patch)
                continue

            needs_review, frist, title = tag_document(
                pl, args, ctx, schema, (corr_names, type_names, choosable_tags), doc,
                split_notes.pop(doc["id"], None))
            processed += 1
            review += needs_review
            if frist:
                deadlines.append((frist, title[:60]))
        except Exception:
            failed += 1
            log.exception("Document %s failed", doc.get("id"))

    # Undecided scans the tag step didn't get to (failed or beyond --limit) still get flagged.
    for doc_id, note in split_notes.items():
        if not args.dry_run:
            try:
                flag_for_review(pl, ctx, pl.get(f"documents/{doc_id}/"), note)
            except Exception:
                log.exception("Document %s: flagging the split proposal failed", doc_id)

    if args.backfill:
        summary = ((f"Paperless-Tagger Nachpflege{' (Testlauf, nichts geschrieben)' if args.dry_run else ''}: "
                    f"{processed} Dokumente geprüft in {(time.time() - start) / 60:.0f} Min., "
                    f"{changed} ergänzt, {review} wegen Unsicherheit übersprungen")
                   + (f", {failed} fehlgeschlagen (siehe {LOG_FILE})" if failed else "") + ".")
        log.info("Summary: %s", summary)
        print(summary, flush=True)
        return 1 if failed and not processed else 0

    summary = summarize(processed, review, failed, deadlines, args.dry_run, time.time() - start,
                        split_stats)
    log.info("Summary: %s", summary)
    if args.json:
        # Split check and tag step of the same document in one entry.
        merged = {}
        for entry in ctx["report"]:
            merged.setdefault(entry["id"], {}).update(entry)
        print(json.dumps(list(merged.values()), ensure_ascii=False, indent=1), flush=True)
        print(summary, file=sys.stderr, flush=True)
    else:
        print(summary, flush=True)
    return 1 if failed and not processed else 0


if __name__ == "__main__":
    sys.exit(main())
