# Tradebot

A production-grade automated **crypto** trading bot (Python, Linux VPS, Alpaca,
SQL Server). See [`SUMMARY.md`](SUMMARY.md) for the full design and
[`TODO.md`](TODO.md) for the phased build plan.

> **Paper trading only.** Live-execution code is not written until the full
> pipeline runs end-to-end on paper.

---

## Phase 1 — Foundation setup

Phase 1 delivers: the SQL Server schema, encrypted credential storage, and a
connectivity green-light test. Run everything below **on the VPS as the
dedicated non-root bot user** (not root).

### 1. System prerequisites (Linux)

```bash
# Microsoft ODBC Driver 18 + unixODBC headers (Debian/Ubuntu)
# Add Microsoft's apt repo first (see Microsoft docs), then:
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
```

### 2. Project + virtualenv

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Master key + .env

```bash
cp .env.example .env

# Generate the master key ONCE and paste it into .env as TRADEBOT_MASTER_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

chmod 600 .env
# (owned by the bot user) e.g.: chown tradebot:tradebot .env
```

**Back the master key up offline.** Losing it makes every stored credential
undecryptable. Fill in the `DB_*` connection settings in `.env` too.

### 4. Database

In SSMS:

```sql
CREATE DATABASE tradebot;
```

Then run [`db/01_schema_phase1.sql`](db/01_schema_phase1.sql) against the
`tradebot` database. It creates `api_credentials`, `app_config`,
`risk_profile`, `watchlist` (and declares `positions` / `trade_history`), and
seeds the active **medium** risk profile + base config. Add a symbol:

```sql
INSERT INTO watchlist (symbol, base_asset, quote_asset)
VALUES ('BTC/USD', 'BTC', 'USD');
```

### 5. Store Alpaca paper credentials

Create a **paper** API key/secret in the Alpaca dashboard, scoped
trading-only (no withdrawal). Then:

```bash
set -a; source .env; set +a
python -m scripts.insert_credentials      # enter provider/env/label + keys (hidden input)
```

### 6. Green-light test

```bash
python -m scripts.check_connectivity
```

All four lines must print **PASS**:

1. DB reachable
2. Decrypt Alpaca creds
3. Alpaca paper account (equity/cash)
4. Live crypto quote (bid/ask)

When all four PASS → **Phase 1 green light** → Phase 2 begins.

---

## Layout (Phase 1)

```
core/
  crypto.py    Fernet encrypt/decrypt (master key from env)
  db.py        SQLAlchemy + pyodbc engine, retry, credential fetch
db/
  01_schema_phase1.sql
scripts/
  insert_credentials.py    you run this; stores encrypted keys
  check_connectivity.py    the green-light test
```
