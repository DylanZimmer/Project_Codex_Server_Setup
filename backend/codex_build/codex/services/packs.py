"""Installing and removing packs from the Codex server in the local Neo4j.

Every node and relationship a pack creates is tagged with the pack's id in a `packs` list property.
Packs share nodes (two packs can both mention "Ibuprofen" or "UA"), so removing a pack takes its id
off each tag list and deletes only what no installed pack, and nothing loaded by hand, still uses.
Anything that existed before a pack touched it (for example demo data) gets the marker "local",
so removing a pack never deletes it.

Pack kinds handled here:
  core          concept IDs (added to existing terms) and the fallback language chains
  lang-<code>   the server's Codex-format file: {manifest, language, terms: [{canonical, concept_id, entries}]}
  country-<CC>  brand names sold in one country: {manifest, country, brands: [{concept_id, canonical, brand}]}

Installed versions are kept as (:InstalledPack {id, version}) nodes in the same database, so they
can never disagree with the data that is actually loaded.
"""

import json
import re
from datetime import datetime, timezone

from codex.neo4j_driver import driver

PACK_ID = re.compile(r"^(core|lang-[a-z]{2,3}|country-[A-Z]{2})$")
BATCH = 500
LOCAL = "local"


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _tag(var):
    """Cypher that records the current pack ($pack) on a node or relationship just MERGEd."""
    return (
        f" ON CREATE SET {var}.packs = [$pack]"
        f" ON MATCH SET {var}.packs = CASE"
        f" WHEN {var}.packs IS NULL THEN ['{LOCAL}', $pack]"
        f" WHEN $pack IN {var}.packs THEN {var}.packs"
        f" ELSE {var}.packs + $pack END"
    )


# Mirrors codex.neo4j_driver.create_translation (same labels and keys), with pack tags.
LANGUAGE_ROWS = f"""
UNWIND $rows AS r
MERGE (t:Term {{canonical: r.canonical, type: 'medication'}}){_tag('t')}
SET t.concept_id = coalesce(r.concept_id, t.concept_id)
MERGE (l:Language {{code: $code, name: $name}}){_tag('l')}
MERGE (tr:Translation {{text: r.translation, country: r.country}}){_tag('tr')}
MERGE (tr)-[a:OF_TERM]->(t){_tag('a')}
MERGE (tr)-[b:IN_LANGUAGE]->(l){_tag('b')}
MERGE (c:Country {{iso2: r.country, name: r.country}}){_tag('c')}
MERGE (tr)-[u:USED_IN]->(c){_tag('u')}
WITH t, tr, c, r WHERE r.brand IS NOT NULL
MERGE (br:Brand {{name: r.brand}}){_tag('br')}
MERGE (br)-[s:SOLD_IN]->(c){_tag('s')}
MERGE (tr)-[h:HAS_BRAND]->(br){_tag('h')}
MERGE (t)-[sa:SOLD_AS]->(br){_tag('sa')}
MERGE (br)-[co:CONTAINS]->(t){_tag('co')}
"""

COUNTRY_ROWS = f"""
UNWIND $rows AS r
MERGE (t:Term {{canonical: r.canonical, type: 'medication'}}){_tag('t')}
SET t.concept_id = coalesce(r.concept_id, t.concept_id)
MERGE (c:Country {{iso2: $code, name: $code}}){_tag('c')}
MERGE (br:Brand {{name: r.brand}}){_tag('br')}
MERGE (br)-[s:SOLD_IN]->(c){_tag('s')}
MERGE (t)-[sa:SOLD_AS]->(br){_tag('sa')}
MERGE (br)-[co:CONTAINS]->(t){_tag('co')}
WITH t, br
OPTIONAL MATCH (tr:Translation {{country: $code}})-[:OF_TERM]->(t)
FOREACH (x IN CASE WHEN tr IS NULL THEN [] ELSE [tr] END |
  MERGE (x)-[h:HAS_BRAND]->(br){_tag('h')}
)
"""

CORE_ROWS = """
UNWIND $rows AS r
MATCH (t:Term) WHERE toLower(t.canonical) = toLower(r.canonical)
SET t.concept_id = r.concept_id
"""


def _batches(rows):
    for i in range(0, len(rows), BATCH):
        yield rows[i:i + BATCH]


def _remove(tx, pack_id):
    """Takes pack_id off every tag list and deletes what nothing else uses any more."""
    tx.run("MATCH ()-[r]->() WHERE $pack IN r.packs SET r.packs = [p IN r.packs WHERE p <> $pack]", pack=pack_id)
    tx.run("MATCH ()-[r]->() WHERE r.packs = [] DELETE r")
    tx.run("MATCH (n) WHERE $pack IN n.packs SET n.packs = [p IN n.packs WHERE p <> $pack]", pack=pack_id)
    tx.run("MATCH (n) WHERE n.packs = [] AND NOT (n)--() DELETE n")
    tx.run("MATCH (p:InstalledPack {id: $pack}) DELETE p", pack=pack_id)


def _load_language(tx, pack_id, data):
    language = data.get("language") or {}
    code, name = language.get("code"), language.get("name") or language.get("code")
    if not code or not isinstance(data.get("terms"), list):
        raise ValueError("Not a language pack: needs language.code and terms")
    rows = [
        {"canonical": term["canonical"], "concept_id": term.get("concept_id"), "translation": entry["translation"],
         "country": entry["country"], "brand": entry.get("brand")}
        for term in data["terms"]
        for entry in term.get("entries", [])
        if term.get("canonical") and entry.get("translation") and entry.get("country")
    ]
    for chunk in _batches(rows):
        tx.run(LANGUAGE_ROWS, rows=chunk, code=code, name=name, pack=pack_id)
    return len(rows)


def _load_country(tx, pack_id, data):
    code = (data.get("country") or {}).get("code")
    if not code or not isinstance(data.get("brands"), list):
        raise ValueError("Not a country pack: needs country.code and brands")
    rows = [
        {"canonical": b["canonical"], "concept_id": b.get("concept_id"), "brand": b["brand"]}
        for b in data["brands"] if b.get("canonical") and b.get("brand")
    ]
    for chunk in _batches(rows):
        tx.run(COUNTRY_ROWS, rows=chunk, code=code, pack=pack_id)
    return len(rows)


def _load_core(tx, data):
    concepts = data.get("concepts")
    if not isinstance(concepts, list):
        raise ValueError("Not a core pack: needs concepts")
    rows = [{"concept_id": c["concept_id"], "canonical": c["canonical"]} for c in concepts if c.get("canonical")]
    for chunk in _batches(rows):
        tx.run(CORE_ROWS, rows=chunk)
    return len(rows)


def _apply_fallbacks(fallbacks):
    """The core pack carries the fallback language chains; translate() uses them from now on."""
    if not isinstance(fallbacks, dict):
        return
    from codex.services import translation_service  # imported here to avoid a circular import

    translation_service.FALLBACK_LANGUAGES.clear()
    translation_service.FALLBACK_LANGUAGES.update(fallbacks)


def restore_fallbacks():
    """At startup: use the fallback chains from the installed core pack, if there is one."""
    with driver.session() as session:
        rec = session.run("MATCH (p:InstalledPack {id: 'core'}) RETURN p.fallbacks AS f").single()
    if rec and rec["f"]:
        _apply_fallbacks(json.loads(rec["f"]))


def install(pack_id, version, data, sha256=None):
    """
    Installs (or replaces) one pack. Removing the old version and loading the new one happen in a
    single transaction: if loading fails, the old version is still there.
    """
    if not PACK_ID.match(pack_id or ""):
        raise ValueError(f"Not a pack id: {pack_id!r}")
    if pack_id == "core":
        kind_loader = lambda tx: _load_core(tx, data)  # noqa: E731
    elif pack_id.startswith("lang-"):
        kind_loader = lambda tx: _load_language(tx, pack_id, data)  # noqa: E731
    else:
        kind_loader = lambda tx: _load_country(tx, pack_id, data)  # noqa: E731

    with driver.session() as session:
        tx = session.begin_transaction()
        try:
            _remove(tx, pack_id)
            rows = kind_loader(tx)
            tx.run(
                "MERGE (p:InstalledPack {id: $id}) SET p.version = $version, p.sha256 = $sha, p.installed_at = $at, "
                "p.fallbacks = $fallbacks",
                id=pack_id, version=int(version), sha=sha256, at=_utc_now(),
                fallbacks=json.dumps(data.get("fallbacks")) if pack_id == "core" and data.get("fallbacks") else None,
            )
            tx.commit()
        except Exception:
            tx.rollback()
            raise
        finally:
            tx.close()

    if pack_id == "core":
        _apply_fallbacks(data.get("fallbacks"))
    return {"id": pack_id, "version": int(version), "rows": rows}


def remove(pack_id):
    with driver.session() as session:
        session.execute_write(_remove, pack_id)
    return {"id": pack_id, "removed": True}


def installed():
    """{pack_id: version} for every installed pack."""
    with driver.session() as session:
        records = session.run("MATCH (p:InstalledPack) RETURN p.id AS id, p.version AS version, p.installed_at AS at")
        return {r["id"]: {"version": r["version"], "installed_at": r["at"]} for r in records}


def install_file(data):
    """
    A pack file brought in by hand (for example on a USB stick from the server). Server-built files carry
    a manifest with the pack id and version. Returns None when the file has no manifest (an old-style pack).
    """
    manifest = data.get("manifest") if isinstance(data, dict) else None
    if not manifest or not PACK_ID.match(str(manifest.get("id", ""))):
        return None
    current = installed().get(manifest["id"])
    if current and current["version"] > int(manifest["version"]):
        raise ValueError(f"{manifest['id']} version {current['version']} is already installed (file is {manifest['version']})")
    return install(manifest["id"], manifest["version"], data)
