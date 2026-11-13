"""
Codex Translation API
=====================
FastAPI service that sits between the CLI (or any HTTP client) and the
Neo4j-backed translation engine.

All business logic lives in codex/.  This file only:
  - Defines request / response shapes (Pydantic models)
  - Routes HTTP calls to the right backend function
  - Formats errors consistently

Endpoints
---------
  GET  /health              Liveness check — API + Neo4j
  POST /translate           Translate a drug term
  GET  /audit/{term}        Quality audit (missing translations / brands)
  POST /packs/load          Upload and load a pack file (server pack, or an old-style language pack)
  GET  /languages           List language codes that have data in Neo4j
  GET  /sync/status         Connection to the Codex server, assignment, installed packs
  POST /sync/run            Sync with the Codex server now
  PUT  /sync/assignment     Set this box's country and languages
  GET  /suggestions         Translator suggestions from this box and their review status
  POST /suggestions         Queue a translator suggestion (sent at the next sync)

Drug data comes from the Codex server (gb_server) as packs. Importing sources (DrugBank, RxNorm, ...)
happens on the server, not here.
"""

from __future__ import annotations

import os
import sys
import json
import tempfile
import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from contextlib import asynccontextmanager
from pathlib import Path
import ast
from pydantic import BaseModel
from dotenv import load_dotenv

from ocr.ocrmatching import router as ocr_router


# ── Load .env before importing codex (which reads env vars at module level) ──
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Import backend package ───────────────────────────────────────────────────
try:
    from codex.services.translation_service import (
        translate,
        load_language_pack,
        load_demo_data,
    )
    from codex.neo4j_driver import (
        driver,
        language_exists,
        find_missing_translations,
        find_missing_brands,
        get_equivalent_brands,
        search_database,
        resolve_to_base_term,
        get_translation_data,
        get_countries_for_term,
        get_languages_for_term,
        get_countries_for_brand,
        get_languages_for_brand,
    )
    from codex.services import packs as pack_store
    from codex.services import sync
except Exception as exc:
    logging.critical("Failed to import codex backend: %s", exc)
    raise exc

# ── App setup ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("codex.api")

BASE_DIR = Path(__file__).resolve().parent
DEMO_PACKS_DIR = BASE_DIR / "demo_language_packs"
# Off by default: data now comes from the Codex server. Turn on for a demo without a server.
LOAD_DEMO_DATA = os.getenv("LOAD_DEMO_DATA", "false").lower() in ("true", "1", "yes")

@asynccontextmanager
async def lifespan(app: FastAPI):
    if LOAD_DEMO_DATA:
        log.info("Scanning demo language packs directory: %s", DEMO_PACKS_DIR)
        if DEMO_PACKS_DIR.exists() and DEMO_PACKS_DIR.is_dir():
            load_demo_data(str(DEMO_PACKS_DIR))
        else:
            log.warning("Demo language packs directory '%s' does not exist.", DEMO_PACKS_DIR)
    else:
        log.info("LOAD_DEMO_DATA is false; skipping automatic demo pack loading.")

    # Use the fallback language chains from the installed core pack, then keep in sync with the Codex server
    # in the background (does nothing until CODEX_SERVER_URL / CODEX_INSTANCE_KEY are set).
    try:
        pack_store.restore_fallbacks()
    except Exception as exc:
        log.warning("Could not read fallback chains from the core pack: %s", exc)
    sync.start()
    yield
    sync.stop()

app = FastAPI(
    title="Codex Medical Translation API",
    description="Translate drug names across languages and countries using Neo4j.",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:9000",
        "http://localhost:9001",
        "http://localhost:9002",
        "http://localhost:9003",
        "http://localhost:9004",
        "http://localhost:9005",
        "http://localhost:9006",
        "http://localhost:9007",
        "http://localhost:9008",
        "http://localhost:9009",
        "http://localhost:9010",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ocr_router)

# ── Request / response models ────────────────────────────────────────────────

class SearchResponse(BaseModel):
    source_id: str
    source_name: str
    name: str
    brand: Optional[str]
    type: str
    country: str
    language: str
    uploaded_at: str

class TranslateRequest(BaseModel):
    term: str
    lang: str
    country: str

    model_config = {"json_schema_extra": {"example": {
        "term": "ibuprofen",
        "lang": "es",
        "country": "MX",
    }}}

class TranslationResult(BaseModel):
    translation: str
    language: str
    brand: Optional[str]
    country: Optional[str]


class TranslateResponse(BaseModel):
    canonical: str
    results: list[TranslationResult]


class AuditEntry(BaseModel):
    country: str
    country_name: str
    reason: Optional[str] = None


class BrandEntry(BaseModel):
    brand: str
    country: str
    country_name: str


class AuditResponse(BaseModel):
    term: str
    canonical: str
    missing_translations: list[AuditEntry]
    missing_brands: list[AuditEntry]
    equivalent_brands: list[BrandEntry]


COUNTRY_NAMES = {
    "US": "United States",
    "GB": "United Kingdom",
    "CA": "Canada",
    "FR": "France",
    "BE": "Belgium",
    "ES": "Spain",
    "MX": "Mexico",
    "RU": "Russia",
    "UA": "Ukraine",
    "PL": "Poland",
    "IN": "India",
    "NG": "Nigeria",
    "BR": "Brazil",
    "DE": "Germany",
    "IT": "Italy",
}

class CountryOption(BaseModel):
    code: str
    label: str

class LanguagesResponse(BaseModel):
    languages: list[str]
    countries: dict[str, list[CountryOption]] = {}


class MessageResponse(BaseModel):
    message: str


class HealthResponse(BaseModel):
    status: str
    neo4j: bool
    api_version: str


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness check",
    tags=["meta"],
)
def health():
    """Returns API status and whether Neo4j is reachable."""
    neo4j_ok = False
    try:
        with driver.session() as session:
            result = session.run("RETURN 1 AS ok").single()
            neo4j_ok = result and result["ok"] == 1
    except Exception as exc:
        log.warning("Neo4j health check failed: %s", exc)

    return HealthResponse(
        status="ok" if neo4j_ok else "degraded",
        neo4j=neo4j_ok,
        api_version=app.version,
    )

@app.post(
    "/search",
    response_model=List[SearchResponse],
    tags=["translation"],
)
def search(term: str):
    search_results = []
    try:
        with driver.session() as session:
            results = search_database(session, term)
            if not results:
                return []
            for result in results:
                canonical = result[0]
                brand = result[1]
                if not brand:
                    countries = get_countries_for_term(session, canonical)
                    languages = get_languages_for_term(session, canonical)
                    search_results.append(SearchResponse(
                        source_id="0",
                        source_name="",
                        name=canonical,
                        # Include brand name even if generic drug is searched
                        brand=brand,
                        type="drug",
                        country=countries,
                        language=languages,
                        uploaded_at=""
                    ))
                else:
                    countries = get_countries_for_brand(session, brand)
                    languages = get_languages_for_brand(session, brand)
                    search_results.append(SearchResponse(
                        source_id="0",
                        source_name="",
                        name=canonical,
                        brand=brand,
                        type="brand name drug",
                        country=countries,
                        language=languages,
                        uploaded_at=""
                    ))
    except Exception as exc:
        log.exception("search raised an unexpected error")
        raise HTTPException(status_code=500, detail=str(exc))
    
    return search_results

@app.post(
    "/translate",
    response_model=TranslateResponse,
    summary="Translate a drug term",
    tags=["translation"],
)
def translate_term(body: TranslateRequest):
    """
    Translate a medical term into the requested language.

    - Resolves brand names and fuzzy input to a canonical term first.
    - Falls back through configured language chains if no direct match.

    JSON SETUP
      incoming
      { "term": "ibuprofen", "source_lang": "en", "target_lang": "es", "country": "MX" }
      
      outgoing
      {
      "canonical": "ibuprofen",
      "results": [
        { "translation": "ibuprofeno", "language": "Spanish", "brand": "Advil", "country": "MX" }
      ]
      }
    """
    log.info("Translate  term=%r  lang=%s  country=%s", body.term, body.lang, body.country)

    try:
        raw_data = translate(term=body.term, lang=body.lang, country=body.country)
        if isinstance(raw_data, str):
            raw = ast.literal_eval(raw_data)
        else:
            raw = raw_data
    except Exception as exc:
        log.exception("translate() raised an unexpected error")
        raise HTTPException(status_code=500, detail=str(exc))

    raw_results = raw.get("results", []) if isinstance(raw, dict) else []
    target_lang_str = (body.lang or "").strip().lower()
    # Build dynamic language map
    LANGUAGE_MAP = {
        "fr": "french",
        "es": "spanish",
        "en": "english",
        "ru": "russian",
        "uk": "ukrainian",
    }
    mapped_lang_name = LANGUAGE_MAP.get(target_lang_str, target_lang_str)

    results = [
        TranslationResult(
            translation=r.get("translation", ""),
            language=r.get("language", ""),
            brand=r.get("brand"),
            country=r.get("country"),
        )
        for r in raw_results
        if (
            not body.lang 
            or str(r.get("language", "")).lower() == target_lang_str
            or str(r.get("lang_code", "")).lower() == target_lang_str
            or str(r.get("language_code", "")).lower() == target_lang_str
            or str(r.get("language", "")).lower() == mapped_lang_name
        )
        and (
            not body.country 
            or str(r.get("country", "")).lower() == body.country.lower()
        )
    ]

    resolved_canonical = raw.get("canonical") if isinstance(raw, dict) and raw.get("canonical") else body.term

    return TranslateResponse(
        canonical=resolved_canonical,
        results=results,
    )


@app.get(
    "/audit/{term}",
    response_model=AuditResponse,
    summary="Quality audit for a term",
    tags=["translation"],
)
def audit_term(term: str):
    """
    Returns:
    - Countries where this term has no translation
    - Countries where this term has no brand name
    - All equivalent brand names across countries
    """
    log.info("Audit  term=%r", term)

    try:
        with driver.session() as session:
            canonical = resolve_to_base_term(session, term) or term
            missing_tx   = find_missing_translations(session, canonical)
            missing_br   = find_missing_brands(session, canonical)
            equivalents  = get_equivalent_brands(session, canonical)
    except Exception as exc:
        log.exception("audit raised an unexpected error")
        raise HTTPException(status_code=500, detail=str(exc))

    return AuditResponse(
        term=term,
        canonical=canonical,
        missing_translations=[AuditEntry(**m) for m in missing_tx],
        missing_brands=[AuditEntry(**m) for m in missing_br],
        equivalent_brands=[
            BrandEntry(brand=e["brand"], country=e["country"], country_name=e["country_name"])
            for e in equivalents
        ],
    )

@app.post(
    "/packs/load",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and load a language pack",
    tags=["data"],
)
async def load_pack(file: UploadFile = File(...)):
    """
    Upload a language pack JSON file and load it into Neo4j.

    The file must follow the standard pack format:
    ```json
    {
      "language": {"code": "pt", "name": "Portuguese"},
      "terms": [
        {
          "canonical": "Ibuprofen",
          "entries": [
            {"translation": "ibuprofeno", "country": "BR", "brand": "Advil"}
          ]
        }
      ]
    }
    ```
    """
    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="File must be a .json language pack")

    log.info("Loading language pack: %s", file.filename)

    # A pack file from the Codex server (brought in by hand, e.g. on a USB stick) has a manifest:
    # install it like a synced pack, so its version is recorded and it can be removed later.
    try:
        content = await file.read()
        try:
            parsed = json.loads(content.decode("utf-8"))
        except ValueError:
            raise HTTPException(status_code=400, detail="The file is not valid JSON")
        installed_pack = pack_store.install_file(parsed)
        if installed_pack:
            return MessageResponse(message=f"Installed {installed_pack['id']} version {installed_pack['version']}")

        # Old-style language pack without a manifest: load it the original way.
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        message = load_language_pack(tmp_path)
        return MessageResponse(message=message)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.exception("load_pack failed")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if "tmp_path" in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.get(
    "/languages",
    response_model=LanguagesResponse,
    summary="List languages and their available countries with data in Neo4j",
    tags=["meta"],
)
def list_languages():
    """Returns every language code and its associated countries currently loaded in Neo4j."""
    try:
        with driver.session() as session:
            records = session.run("""
                MATCH (l:Language)
                OPTIONAL MATCH (tr:Translation)-[:IN_LANGUAGE]->(l)
                OPTIONAL MATCH (tr)-[:USED_IN]->(c:Country)
                WITH l.code AS lang, collect(DISTINCT c.iso2) AS country_codes
                RETURN lang, [code IN country_codes WHERE code IS NOT NULL] AS countries
                ORDER BY lang
            """)
            languages = []
            countries_map = {}
            for r in records:
                lang = r["lang"]
                if not lang:
                    continue
                languages.append(lang)
                codes = sorted(r["countries"])
                countries_map[lang] = [
                    CountryOption(code=c, label=COUNTRY_NAMES.get(c.upper(), c.upper()))
                    for c in codes if c
                ]
        return LanguagesResponse(languages=languages, countries=countries_map)
    except Exception as exc:
        log.exception("list_languages failed")
        raise HTTPException(status_code=500, detail=str(exc))

# ── Sync with the Codex server ───────────────────────────────────────────────

class AssignmentRequest(BaseModel):
    country: str
    languages: List[str]


class SuggestionRequest(BaseModel):
    canonical: str
    language: str
    text: str
    country: Optional[str] = None
    type: str = "generic"
    note: Optional[str] = None
    submitted_by: Optional[str] = None


@app.get("/sync/status", summary="Connection to the Codex server and installed packs", tags=["sync"])
def sync_status():
    return sync.status()


@app.post("/sync/run", summary="Sync with the Codex server now", tags=["sync"])
def sync_run():
    # FastAPI runs plain (non-async) handlers in a worker thread, so this does not block other requests.
    return sync.sync_now("manual")


@app.put("/sync/assignment", summary="Set this box's country and languages", tags=["sync"])
def sync_assignment(body: AssignmentRequest):
    try:
        assignment = sync.set_assignment(body.country, body.languages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if sync.configured():
        sync.run_in_background("assignment")
    return {"assignment": assignment}


@app.get("/suggestions", summary="Translator suggestions from this box", tags=["sync"])
def suggestions_list():
    return {"suggestions": sync.list_suggestions()}


@app.post("/suggestions", status_code=status.HTTP_201_CREATED, summary="Suggest a better name", tags=["sync"])
def suggestions_add(body: SuggestionRequest):
    try:
        item = sync.add_suggestion(body.canonical, body.language, body.text, country=body.country,
                                   term_type=body.type, note=body.note, submitted_by=body.submitted_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if sync.configured():
        sync.run_in_background("suggestion")
    return {"suggestion": item}


# ── Entry point (for running directly) ──────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("API_PORT", 8000))
    log.info("Starting Codex API on port %d", port)
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=True)
