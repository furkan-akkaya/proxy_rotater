# 🔄 Proxy Rotator v2.0

A terminal-based **Proxy Rotator** tool written in Python with SQLite persistence, geo-filtering, and multi-protocol (HTTP/HTTPS/SOCKS4/SOCKS5) support.

![Python](https://img.shields.io/badge/Python-3.8+-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Linux%20|%20macOS%20|%20Windows-blue)
![Version](https://img.shields.io/badge/Version-2.0-orange)

---

## ✨ Features

| Feature | Description |
|---|---|
| **SQLite Cache** | Proxies are stored in a local database — no redundant scraping across runs |
| **Smart Caching** | Only scrapes when the active pool drops below a threshold (default: 20) |
| **Multi-Source** | Scrapes from `free-proxy-list.net` (HTTP/HTTPS) and `socks-proxy.net` (SOCKS4/5) |
| **Geo-Filtering** | Filter proxies by country code (`-C US,DE,TR`) |
| **Protocol Filter** | Choose HTTP, HTTPS, SOCKS4, or SOCKS5 with `-p` |
| **Concurrent Validation** | Tests proxies in parallel using `ThreadPoolExecutor` (40 threads) |
| **IP Masking Check** | Verifies the proxy actually hides your real IP |
| **Coloured Output** | Beautiful terminal output with colour-coded statuses |
| **Professional CLI** | Full `argparse` interface with examples |

## 📋 Requirements

- Python 3.8+
- pip

## ⚙️ Installation

```bash
# Clone the repository
git clone https://github.com/<your-username>/proxy_rotater.git
cd proxy_rotater

# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate   # Linux/macOS
# venv\Scripts\activate    # Windows

# Install dependencies (includes SOCKS support)
pip install -r requirements.txt
```

## 🚀 Usage

```bash
# Basic — 3 requests through random proxies
python rotator_v2.py -u https://example.com -c 3

# SOCKS5 only
python rotator_v2.py -u https://httpbin.org/ip -c 5 -p SOCKS5

# Filter by country (US and Germany only)
python rotator_v2.py -u https://example.com -c 5 -C US,DE

# Combine protocol + country filter
python rotator_v2.py -u https://example.com -c 10 -p HTTP -C TR

# Force a fresh scrape (ignore cache)
python rotator_v2.py -u https://httpbin.org/ip -c 3 --force-scrape

# Custom timeout and thread count
python rotator_v2.py -u https://example.com -c 5 -t 5 --threads 60
```

### CLI Arguments

| Argument | Short | Description | Default |
|---|---|---|---|
| `--url` | `-u` | Target URL to send requests to | *(required)* |
| `--count` | `-c` | Number of rotated requests | `3` |
| `--protocol` | `-p` | Filter: `HTTP`, `HTTPS`, `SOCKS4`, `SOCKS5` | All |
| `--country` | `-C` | Comma-separated ISO country codes | All |
| `--timeout` | `-t` | Proxy validation timeout (seconds) | `4` |
| `--threads` | | Max concurrent validation threads | `40` |
| `--force-scrape` | | Force fresh scrape, ignore DB cache | `false` |
| `--min-pool` | | Min active proxies before auto-scrape | `20` |

## 🏗️ Architecture

```
rotator_v2.py
├── Proxy              — Immutable dataclass (IP, Port, Protocol, Country)
├── Logger             — Coloured terminal output via colorama
├── ProxyDatabase      — SQLite persistence layer (CRUD + filtering)
├── ProxyScraper       — Multi-source scraper (free-proxy-list + socks-proxy)
├── ProxyValidator     — Concurrent validation with ThreadPoolExecutor
├── ProxyRotator       — Random rotation & request dispatching
├── RotatorEngine      — High-level orchestrator (cache → scrape → validate → rotate)
└── main()             — CLI entry point (argparse)
```

### Database Schema

```sql
CREATE TABLE proxies (
    ip           TEXT    NOT NULL,
    port         TEXT    NOT NULL,
    protocol     TEXT    NOT NULL DEFAULT 'HTTP',
    country_code TEXT    NOT NULL DEFAULT '--',
    latency      REAL,
    is_active    INTEGER NOT NULL DEFAULT 0,
    last_checked TEXT,
    PRIMARY KEY (ip, port, protocol)
);
```

## 📸 Example Output

```
  [  DB]  Database ready → proxies.db
  [  DB]  Cache has 4 active proxies (< 20). Triggering scrape …

  [INFO]  Scraping https://free-proxy-list.net/ [HTTP/HTTPS]
  [INFO]    ↳ Parsed 300 HTTP/HTTPS proxies
  [INFO]  Scraping https://www.socks-proxy.net/ [SOCKS4/SOCKS5]
  [INFO]    ↳ Parsed 100 SOCKS proxies
  [INFO]  Total scraped: 400 proxies from 2 sources
  [  DB]  Persisted 400 proxies to database

  [INFO]  Validating 400 proxies with 40 threads (timeout=4s) …
  [  OK]  ✓ HTTP   104.207.44.178:3128 [US] — 523 ms
  [FAIL]  ✗ SOCKS5 45.77.56.114:1080 [DE]
  ...
  [INFO]  Validation done in 14.2s — 12/400 alive

  [INFO]  Rotation pool: 12 proxies

  [1/3] Proxy: HTTP   104.207.44.178:3128 [US]
  [  OK]  HTTP 200 — 842 ms

  [2/3] Proxy: SOCKS5 185.199.229.156:1080 [DE]
  [  OK]  HTTP 200 — 1203 ms

  [3/3] Proxy: HTTP   89.40.142.236:3128 [TR]
  [  OK]  HTTP 200 — 567 ms

  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    SUMMARY
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Total Requests : 3  ✓ 3  ✗ 0
    Avg Latency    : 871 ms
    Unique Proxies : 3
    Protocols      : HTTP, SOCKS5
    Countries      : DE, TR, US
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## ⚠️ Disclaimer

This tool is intended for **educational purposes** and **authorized security testing** only. Always ensure you have explicit permission before scanning or sending automated requests to any target. The author assumes no liability for misuse.

## 📄 License

This project is licensed under the [MIT License](LICENSE).
