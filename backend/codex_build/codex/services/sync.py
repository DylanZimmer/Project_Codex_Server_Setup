"""Keeps this Codex box in step with the Codex server (gb_server).

One sync:
  1. sends translator suggestions queued on this box (the outbox)
  2. checks in with the installed pack versions and this box's assignment (country + languages)
  3. downloads each pack the server lists, checks its SHA-256, and installs it (codex.services.packs)
  4. removes packs the server says this box no longer needs
  5. fetches the review results of earlier suggestions

It runs in a background thread at startup and every CODEX_SYNC_INTERVAL_MINUTES, and on demand
(POST /sync/run). When the server cannot be reached the box carries on with what it has.

Settings (environment):
  CODEX_SERVER_URL              e.g. https://codex.example.org
  CODEX_INSTANCE_KEY            this box's key, created on the server's admin page
  CODEX_COUNTRY, CODEX_LANGUAGES default assignment, e.g. UA and "uk" (can be changed in the app)
  CODEX_SYNC_INTERVAL_MINUTES   default 360
  CODEX_SYNC_ENABLED            default true
"""

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone

import httpx

from codex.neo4j_driver import driver
from codex.services import packs

log = logging.getLogger("codex.sync")

_lock = threading.Lock()
_stop = threading.Event()
_thread = None

SUGGESTION_STATUSES = ("queued", "submitted", "approved", "rejected", "failed")


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def settings():
    return {
        "server_url": os.getenv("CODEX_SERVER_URL", "").strip().rstrip("/"),
        "instance_key": os.getenv("CODEX_INSTANCE_KEY", "").strip(),
        "interval_minutes": max(5, int(os.getenv("CODEX_SYNC_INTERVAL_MINUTES", "360") or 360)),
        "enabled": os.getenv("CODEX_SYNC_ENABLED", "true").lower() in ("1", "true", "yes"),
    }


def configured():
    s = settings()
    return bool(s["server_url"] and s["instance_key"])


# ── Local state (kept in Neo4j next to the data it describes) ────────────────

def _state():
    with driver.session() as session:
        rec = session.run("MATCH (s:SyncState {id: 'sync'}) RETURN properties(s) AS p").single()
        return dict(rec["p"]) if rec else {}


def _save_state(**fields):
    """Updates the stored sync state. None leaves a field as it was; "" clears it."""
    clean = {k: (json.dumps(v) if isinstance(v, dict) else v) for k, v in fields.items() if v is not None}
    with driver.session() as session:
        session.run("MERGE (s:SyncState {id: 'sync'}) SET s += $fields", fields=clean)


def _json(value, default=None):
    try:
        return json.loads(value) if isinstance(value, str) else (value if value is not None else default)
    except ValueError:
        return default


def get_assignment():
    """The assignment chosen on this box, else the one from the environment. None when neither is set."""
    state = _state()
    country = state.get("country") or os.getenv("CODEX_COUNTRY", "").strip()
    languages = state.get("languages") or [l.strip() for l in os.getenv("CODEX_LANGUAGES", "").split(",") if l.strip()]
    if not country or not languages:
        return None
    return {"country": country.upper(), "languages": [l.lower() for l in languages]}


def set_assignment(country, languages):
    country = str(country or "").strip().upper()
    languages = sorted({str(l).strip().lower() for l in (languages or []) if str(l).strip()})
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country must be a 2-letter code like UA")
    if not languages or not all(re.fullmatch(r"[a-z]{2,3}", l) for l in languages):
        raise ValueError("languages must be codes like uk")
    _save_state(country=country, languages=languages)
    return get_assignment()


# ── Translator outbox ────────────────────────────────────────────────────────

def add_suggestion(canonical, language, text, country=None, term_type="generic", note=None, submitted_by=None):
    canonical, text = str(canonical or "").strip(), str(text or "").strip()
    language = str(language or "").strip().lower()
    country = str(country).strip().upper() if country else None
    if not canonical or not text:
        raise ValueError("canonical and text are required")
    if not re.fullmatch(r"[a-z]{2,3}", language):
        raise ValueError("language must be a code like uk")
    if country is not None and not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country must be a 2-letter code like UA")
    if term_type not in ("generic", "brand"):
        raise ValueError('type must be "generic" or "brand"')
    if term_type == "brand" and not country:
        raise ValueError("a brand suggestion needs a country")
    item = {
        "client_id": str(uuid.uuid4()), "canonical": canonical, "language": language, "country": country,
        "type": term_type, "text": text[:300], "note": (note or "")[:1000] or None,
        "submitted_by": (submitted_by or "")[:200] or None, "status": "queued", "created_at": _utc_now(),
    }
    with driver.session() as session:
        session.run("CREATE (s:Suggestion) SET s = $item", item={k: v for k, v in item.items() if v is not None})
    return item


def list_suggestions(limit=200):
    with driver.session() as session:
        rows = session.run("MATCH (s:Suggestion) RETURN properties(s) AS p ORDER BY s.created_at DESC LIMIT $limit", limit=int(limit))
        return [dict(r["p"]) for r in rows]


def _update_suggestion(client_id, **fields):
    with driver.session() as session:
        session.run("MATCH (s:Suggestion {client_id: $id}) SET s += $fields", id=client_id,
                    fields={k: v for k, v in fields.items() if v is not None})


def _send_outbox(client):
    queued = [s for s in list_suggestions(1000) if s.get("status") == "queued"]
    if not queued:
        return 0
    payload = [{k: s.get(k) for k in ("client_id", "canonical", "language", "country", "type", "text", "note", "submitted_by")
                if s.get(k) is not None} for s in queued]
    r = client.post("/v1/submissions", json={"submissions": payload})
    r.raise_for_status()
    sent = 0
    for res in r.json().get("results", []):
        if res.get("result") in ("accepted", "duplicate"):
            _update_suggestion(res["client_id"], status="submitted", server_term_id=res.get("term_id"), sent_at=_utc_now())
            sent += 1
        else:
            _update_suggestion(res.get("client_id"), status="failed", error=res.get("reason"))
    return sent


def _refresh_suggestions(client):
    if not any(s.get("status") == "submitted" for s in list_suggestions(1000)):
        return
    r = client.get("/v1/submissions", params={"limit": 1000})
    r.raise_for_status()
    for item in r.json().get("submissions", []):
        if item.get("client_id") and item.get("status") in ("approved", "rejected"):
            _update_suggestion(item["client_id"], status=item["status"], review_note=item.get("review_note"))


# ── Sync ─────────────────────────────────────────────────────────────────────

def _client():
    s = settings()
    return httpx.Client(base_url=s["server_url"], headers={"Authorization": f"Bearer {s['instance_key']}"},
                        timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True)


def _download(client, item):
    """Language packs come in the Codex format; core and country packs in the server's own format."""
    if item.get("kind") == "language" and item.get("codex_url"):
        path, expected = item["codex_url"], item.get("codex_sha256")
    else:
        path, expected = item["url"], item.get("sha256")
    r = client.get(path)
    r.raise_for_status()
    body = r.content
    actual = hashlib.sha256(body).hexdigest()
    if expected and actual != expected:
        raise ValueError(f"{item['id']} v{item['version']}: checksum mismatch, download discarded")
    return json.loads(body.decode("utf-8")), actual


def sync_now(trigger="manual"):
    """Runs one sync. Returns the status afterwards. Only one sync runs at a time."""
    if not _lock.acquire(blocking=False):
        return {**status(), "busy": True}
    started = _utc_now()
    result = {"trigger": trigger, "installed": [], "removed": [], "suggestions_sent": 0, "errors": []}
    try:
        if not configured():
            _save_state(last_sync_at=started, last_error="Not connected: set CODEX_SERVER_URL and CODEX_INSTANCE_KEY")
            return status()
        with _client() as client:
            result["suggestions_sent"] = _send_outbox(client)

            body = {"installed": {pid: info["version"] for pid, info in packs.installed().items()}}
            assignment = get_assignment()
            if assignment:
                body["assignment"] = assignment
            r = client.post("/v1/instances/check-in", json=body)
            r.raise_for_status()
            plan = r.json()

            # Core first, so language and country packs always sit on a core they understand.
            items = sorted(plan.get("install", []) + plan.get("update", []), key=lambda p: p["id"] != "core")
            for item in items:
                try:
                    data, sha = _download(client, item)
                    packs.install(item["id"], item["version"], data, sha)
                    result["installed"].append(f"{item['id']}@{item['version']}")
                except Exception as exc:  # one bad pack should not stop the others
                    log.exception("Installing %s failed", item.get("id"))
                    result["errors"].append(f"{item.get('id')}: {exc}")
            for pack_id in plan.get("remove", []):
                try:
                    packs.remove(pack_id)
                    result["removed"].append(pack_id)
                except Exception as exc:
                    result["errors"].append(f"remove {pack_id}: {exc}")

            _refresh_suggestions(client)

        _save_state(
            last_sync_at=started, last_success_at=_utc_now() if not result["errors"] else None,
            last_error="; ".join(result["errors"]) or None,
            server_assignment=plan.get("assignment"), unavailable=plan.get("unavailable", []),
            last_result=result, server_message=plan.get("message"),
        )
        if not result["errors"]:
            _save_state(last_error="")  # SET += ignores None, so clear explicitly
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300]
        _save_state(last_sync_at=started, last_error=f"Server answered {exc.response.status_code}: {detail}")
    except httpx.HTTPError as exc:
        _save_state(last_sync_at=started, last_error=f"Server not reachable: {exc.__class__.__name__}")
    except Exception as exc:
        log.exception("Sync failed")
        _save_state(last_sync_at=started, last_error=str(exc))
    finally:
        _lock.release()
    return status()


def status():
    s = settings()
    state = _state()
    suggestions = list_suggestions(1000)
    counts = {k: sum(1 for x in suggestions if x.get("status") == k) for k in SUGGESTION_STATUSES}
    return {
        "configured": configured(),
        "enabled": s["enabled"],
        "server_url": s["server_url"] or None,
        "interval_minutes": s["interval_minutes"],
        "running": _lock.locked(),
        "assignment": get_assignment(),
        "server_assignment": _json(state.get("server_assignment")),
        "installed": packs.installed(),
        "unavailable": state.get("unavailable") or [],
        "last_sync_at": state.get("last_sync_at"),
        "last_success_at": state.get("last_success_at"),
        "last_error": state.get("last_error") or None,
        "server_message": state.get("server_message"),
        "last_result": _json(state.get("last_result")),
        "suggestions": counts,
    }


def run_in_background(trigger="manual"):
    """Starts one sync without waiting for it."""
    threading.Thread(target=sync_now, args=(trigger,), daemon=True, name="codex-sync-once").start()


def start():
    """Background loop: sync at startup, then every interval. Does nothing until the box is connected."""
    global _thread
    s = settings()
    if not s["enabled"] or _thread is not None:
        return

    def loop():
        while not _stop.is_set():
            if configured():
                sync_now("schedule")
            _stop.wait(s["interval_minutes"] * 60)

    _thread = threading.Thread(target=loop, daemon=True, name="codex-sync")
    _thread.start()


def stop():
    _stop.set()
