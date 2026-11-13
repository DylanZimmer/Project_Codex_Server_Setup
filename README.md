Codex — Setup Guide

Prerequisites


| Tool                    | Version    | Purpose                           |
| ----------------------- | ---------- | --------------------------------- |
| Python                  | 3.10+      | API and CLI                       |
| Docker + Docker Compose | any recent | Full stack (Neo4j, API, Frontend) |
| pip                     | any        | Python dependencies               |


---

## Quick start (Docker)

```bash
docker compose up --build
```

Wait ~30 seconds for Neo4j, API, and Frontend to be ready.

- **Frontend UI:** [http://localhost:9000](http://localhost:9000)
- **API Docs (Swagger):** [http://localhost:8000/docs](http://localhost:8000/docs)
- **Neo4j Browser:** [http://localhost:7474](http://localhost:7474) (login: `neo4j` / `changeme`)

---

## Connecting to the Codex server

Codex boxes get their drug data from the Codex server (`gb_server/`) as **packs**:
- one **core** pack
- one pack per **language** (translated names)
- one pack per **country** (local brand names)

Each box downloads only the packs for where it's used. Importing the sources (DrugBank, RxNorm, ICD-11, SNOMED) happens on the server, not here.

1. On the server's admin page (`/admin/`), go to **Instances → Add instance**. It shows this box's key once.
2. In this project's `.env`, set:
   ```
   CODEX_SERVER_URL=https://<server address>        # same machine in Docker: http://host.docker.internal:3000
   CODEX_INSTANCE_KEY=<the key>
   CODEX_COUNTRY=UA
   CODEX_LANGUAGES=uk
   ```
3. Restart the API. It syncs at startup and then every `CODEX_SYNC_INTERVAL_MINUTES` (default 6 hours) whenever the server can be reached. Without a connection, the box keeps working with the packs it already has.

The **Sync** button in the app shows:
- the connection and the last successful sync
- the installed packs
- where the box is used. Changing the country or languages there installs the matching packs and removes the rest at the next sync.

**Translator suggestions:** under a translation result, "Suggest a better name" queues a proposal. It goes to the server at the next sync. A reviewer there approves or rejects it, and approved names reach every box with the next pack version. The Sync screen shows each suggestion's status.

**Boxes without internet:** download the pack files on a connected machine, using the Codex-format link of each pack (`?format=codex`). Then load them here with **Import Language** (or `POST /packs/load`). They're installed exactly like synced packs, with their versions recorded.

How packs are stored: everything a pack creates in Neo4j is tagged with the pack's id. Removing a pack deletes only what no other installed pack, and nothing loaded by hand, still uses. The code is in `backend/codex_build/codex/services/packs.py` (loading and removal) and `sync.py` (talking to the server).

---

## Manual Setup (Step by Step)

### Step 1 — Start Neo4j

```bash
docker buildx bake -f docker-compose.yml --allow=fs.read=frontend --allow=fs.read=backend/codex_build --load && docker compose up --no-build --pull always

```

This starts Neo4j 5.18 with the APOC plugin (required for fuzzy matching) and exposes two ports:


| Port | Use                                                                                          |
| ---- | -------------------------------------------------------------------------------------------- |
| 7474 | Neo4j Browser — [http://localhost:7474](http://localhost:7474) (login: `neo4j` / `changeme`) |
| 7687 | Bolt — used by the API                                                                       |


Wait ~30 seconds for Neo4j to finish initialising before starting the API. Watch progress with:

```bash
docker compose logs -f neo4j
```

---

### Step 2 — Configure credentials

```bash
cp .env.example .env
```

The defaults match `docker-compose.yml` exactly. Fill in the `CODEX_*` settings to connect to the Codex server (see [Connecting to the Codex server](#connecting-to-the-codex-server)).

#### Demo Language Packs

To try Codex without a server, set `LOAD_DEMO_DATA=true` in `.env`. The API then loads the sample packs from `backend/codex_build/demo_language_packs/` at startup. It's off by default, because data normally comes from the server.

---

### Step 3 — Install Python dependencies

```bash
pip install -r backend/codex_build/requirements.txt
```

---

### Step 4 — Start the API

```bash
cd backend/codex_build
python api.py
```

Or with auto-reload during development:

```bash
uvicorn api:app --reload
```

Swagger UI is available at [**http://localhost:8000/docs**](http://localhost:8000/docs)

---

### Step 5 — Verify

```bash
cd backend/codex_build
python cli.py
codex> health
```

Expected output:

```
  API   : ✓ running  (v2.0.0)
  Neo4j : ✓ connected
```

---

### Step 6 — Load drug data

With the `CODEX_*` settings filled in, the data arrives from the Codex server on the first sync. Use the **Sync** button in the app, or:

```bash
curl -X POST http://localhost:8000/sync/run
```

Without a server, load a pack file by hand: from the app's **Import Language** button, or `codex> load path/to/pack.json` in the CLI. Then check what's there:

```bash
codex> languages
```

---

### Step 7 — Translate a drug

```bash
codex> translate
  Drug name (generic or brand): Advil
  Source language code (e.g. en, es, fr): en
  Target language code (e.g. es, uk, fr): es
  Country code (e.g. US, MX) or Enter to search all: MX
```

Returns all matching brand names in the target language, sorted alphabetically:

```
  Brand Name      Generic Name    Original Language   Translated Language
  ──────────────────────────────────────────────────────────────────────
  Advil           Ibuprofeno      English             Spanish
  Anadvil         Ibuprofeno      English             Spanish
```

---

## CSV format

All drug data is managed via CSV files. **One row per (drug, brand, country).**

**Example — English (US):**

```csv
DrugBank ID,Generic Name,Brand Name,Country,Source Language,Language Code
DB00001,Ibuprofen,Advil,US,English,en
DB00001,Ibuprofen,Motrin,US,English,en
DB00002,Acetaminophen,Tylenol,US,English,en
```

**Example — Spanish (MX):**

```csv
DrugBank ID,Generic Name,Brand Name,Country,Source Language,Language Code
DB00001,Ibuprofeno,Advil,MX,Spanish,es
DB00001,Ibuprofeno,Anadvil,MX,Spanish,es
DB00002,Paracetamol,Tempra,MX,Spanish,es
```

The `DrugBank ID` column is the cross-language key used by the API to match terms across languages.

---

## JSON envelope format

All API responses returning tabular data use this envelope:

```json
{
  "metadata": {
    "generated_at":  "<ISO-8601 UTC timestamp>",
    "row_count":     10,
    "source":        "<filename | 'neo4j' | 'translate'>",
    "sort_order":    "generic_name_asc"
  },
  "csv": [ ... ]
}
```

---

## API reference


| Method | Path            | Description                                                |
| ------ | --------------- | ---------------------------------------------------------- |
| GET    | `/health`       | Liveness check — API + Neo4j                               |
| POST   | `/translate`    | Translate a drug name between languages                    |
| GET    | `/audit/{term}` | Quality audit (missing translations / brands)              |
| POST   | `/search`       | Find drugs and brands by name (fuzzy)                      |
| GET    | `/languages`    | List language codes (and their countries) loaded in Neo4j  |
| POST   | `/packs/load`   | Load a pack file by hand (server pack or old-style pack)   |
| POST   | `/ocrmatching/` | Extract text from image via OCR and fuzzy match drug terms |
| GET    | `/sync/status`  | Server connection, assignment, installed packs, last sync  |
| POST   | `/sync/run`     | Sync with the Codex server now                             |
| PUT    | `/sync/assignment` | Set this box's country and languages                    |
| GET    | `/suggestions`  | Translator suggestions from this box and their status      |
| POST   | `/suggestions`  | Queue a suggestion for review (sent at the next sync)      |


### POST /translate — request body

```json
{
  "term":        "Advil",
  "source_lang": "en",
  "target_lang": "es",
  "country":     "MX"
}
```

---

## Database Management &amp; Maintenance

### Changing the Neo4j password

1. Edit `.env`:
   ```
    NEO4J_PASSWORD=your_new_password
   ```
2. Update `docker-compose.yml`:
   ```yaml
    NEO4J_AUTH: "neo4j/your_new_password"
   ```
3. Restart:
   ```bash
    docker compose down && docker compose up -d
   ```

### Stopping or Resetting Neo4j

- **Stop containers (preserve data):**
  ```bash
  docker compose down
  ```
- **Reset and wipe all data:**
  ```bash
  docker compose down -v
  ```

