#!/usr/bin/env python3
"""
Proxy Rotator v2.0 — Advanced terminal-based rotating proxy tool.

Features over v1:
  • SQLite persistence — cached proxy pool survives across runs.
  • Geo-targeting    — filter proxies by ISO country code.
  • Multi-protocol   — HTTP, HTTPS, SOCKS4, SOCKS5 support.
  • Smart caching    — skips scraping when the DB already has enough
                       active proxies.

Author : Furkan Akkaya
License: MIT
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from colorama import Fore, Style, init as colorama_init

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).resolve().parent / "proxies.db"
VALIDATION_URL = "http://httpbin.org/ip"
DEFAULT_TIMEOUT = 4          # seconds — per-proxy validation timeout
MAX_WORKERS = 40             # concurrent threads for validation
MIN_POOL_SIZE = 20           # minimum active proxies before re-scraping
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SOURCES = {
    "free-proxy-list": "https://free-proxy-list.net/",
    "socks-proxy":     "https://www.socks-proxy.net/",
}

SUPPORTED_PROTOCOLS = ("HTTP", "HTTPS", "SOCKS4", "SOCKS5")


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proxy:
    """Immutable representation of a single proxy server."""

    ip: str
    port: str
    protocol: str = "HTTP"
    country_code: str = "--"

    @property
    def address(self) -> str:
        """Return the ``host:port`` string."""
        return f"{self.ip}:{self.port}"

    @property
    def scheme(self) -> str:
        """Return the URL scheme for this proxy's protocol."""
        mapping = {
            "HTTP":   "http",
            "HTTPS":  "http",
            "SOCKS4": "socks4",
            "SOCKS5": "socks5",
        }
        return mapping.get(self.protocol.upper(), "http")

    @property
    def url(self) -> str:
        """Return the full proxy URL with scheme."""
        return f"{self.scheme}://{self.address}"

    @property
    def as_dict(self) -> dict[str, str]:
        """Return the proxy mapping expected by *requests*."""
        return {"http": self.url, "https": self.url}

    def __str__(self) -> str:  # noqa: D105
        return f"{self.protocol:<6} {self.address} [{self.country_code}]"


# ---------------------------------------------------------------------------
# Logger — coloured terminal output
# ---------------------------------------------------------------------------


class Logger:
    """Thin wrapper around *print* that adds colour via *colorama*."""

    _PREFIXES = {
        "info":    f"{Fore.CYAN}[INFO]{Style.RESET_ALL}",
        "success": f"{Fore.GREEN}[  OK]{Style.RESET_ALL}",
        "fail":    f"{Fore.RED}[FAIL]{Style.RESET_ALL}",
        "warn":    f"{Fore.YELLOW}[WARN]{Style.RESET_ALL}",
        "db":      f"{Fore.MAGENTA}[  DB]{Style.RESET_ALL}",
    }

    @classmethod
    def info(cls, msg: str) -> None:
        print(f"  {cls._PREFIXES['info']}  {msg}")

    @classmethod
    def success(cls, msg: str) -> None:
        print(f"  {cls._PREFIXES['success']}  {Fore.GREEN}{msg}{Style.RESET_ALL}")

    @classmethod
    def fail(cls, msg: str) -> None:
        print(f"  {cls._PREFIXES['fail']}  {Fore.RED}{msg}{Style.RESET_ALL}")

    @classmethod
    def warn(cls, msg: str) -> None:
        print(f"  {cls._PREFIXES['warn']}  {Fore.YELLOW}{msg}{Style.RESET_ALL}")

    @classmethod
    def db(cls, msg: str) -> None:
        print(f"  {cls._PREFIXES['db']}  {Fore.MAGENTA}{msg}{Style.RESET_ALL}")

    @classmethod
    def banner(cls) -> None:
        art = rf"""
{Fore.CYAN}
  ██████╗ ██████╗  ██████╗ ██╗  ██╗██╗   ██╗
  ██╔══██╗██╔══██╗██╔═══██╗╚██╗██╔╝╚██╗ ██╔╝
  ██████╔╝██████╔╝██║   ██║ ╚███╔╝  ╚████╔╝
  ██╔═══╝ ██╔══██╗██║   ██║ ██╔██╗   ╚██╔╝
  ██║     ██║  ██║╚██████╔╝██╔╝ ██╗   ██║
  ╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝
{Fore.WHITE}  ██████╗  ██████╗ ████████╗ █████╗ ████████╗ ██████╗ ██████╗
  ██╔══██╗██╔═══██╗╚══██╔══╝██╔══██╗╚══██╔══╝██╔═══██╗██╔══██╗
  ██████╔╝██║   ██║   ██║   ███████║   ██║   ██║   ██║██████╔╝
  ██╔══██╗██║   ██║   ██║   ██╔══██║   ██║   ██║   ██║██╔══██╗
  ██║  ██║╚██████╔╝   ██║   ██║  ██║   ██║   ╚██████╔╝██║  ██║
  ╚═╝  ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝
{Style.RESET_ALL}
{Fore.YELLOW}  ⚡  v2.0 — SQLite · Geo-Filter · SOCKS5
  ⚡  Author: Furkan Akkaya{Style.RESET_ALL}
"""
        print(art)


# ---------------------------------------------------------------------------
# Database Layer
# ---------------------------------------------------------------------------


class ProxyDatabase:
    """SQLite persistence layer for the proxy pool.

    Stores every scraped proxy with metadata (protocol, country, latency,
    activity flag, last-checked timestamp) and provides query helpers
    for filtering.
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS proxies (
        ip           TEXT    NOT NULL,
        port         TEXT    NOT NULL,
        protocol     TEXT    NOT NULL DEFAULT 'HTTP',
        country_code TEXT    NOT NULL DEFAULT '--',
        latency      REAL             DEFAULT NULL,
        is_active    INTEGER NOT NULL DEFAULT 0,
        last_checked TEXT             DEFAULT NULL,
        PRIMARY KEY (ip, port, protocol)
    );
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(self._DDL)
        self._conn.commit()
        Logger.db(f"Database ready → {self._db_path.name}")

    # -- writes -------------------------------------------------------------

    def upsert_proxy(self, proxy: Proxy) -> None:
        """Insert or ignore a scraped proxy (initial state: inactive)."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO proxies (ip, port, protocol, country_code)
            VALUES (?, ?, ?, ?)
            """,
            (proxy.ip, proxy.port, proxy.protocol.upper(), proxy.country_code),
        )

    def mark_active(
        self,
        proxy: Proxy,
        latency_ms: float,
    ) -> None:
        """Mark *proxy* as active and record its latency."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE proxies
               SET is_active    = 1,
                   latency      = ?,
                   last_checked = ?
             WHERE ip = ? AND port = ? AND protocol = ?
            """,
            (round(latency_ms, 1), now, proxy.ip, proxy.port, proxy.protocol),
        )

    def mark_inactive(self, proxy: Proxy) -> None:
        """Mark *proxy* as inactive after a failed check."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE proxies
               SET is_active    = 0,
                   last_checked = ?
             WHERE ip = ? AND port = ? AND protocol = ?
            """,
            (now, proxy.ip, proxy.port, proxy.protocol),
        )

    def commit(self) -> None:
        """Flush pending writes."""
        self._conn.commit()

    # -- reads --------------------------------------------------------------

    def get_active(
        self,
        protocol: Optional[str] = None,
        country: Optional[str] = None,
    ) -> list[Proxy]:
        """Return active proxies, optionally filtered by protocol/country."""
        query = "SELECT ip, port, protocol, country_code FROM proxies WHERE is_active = 1"
        params: list[str] = []

        if protocol:
            query += " AND UPPER(protocol) = ?"
            params.append(protocol.upper())

        if country:
            codes = [c.strip().upper() for c in country.split(",")]
            placeholders = ", ".join("?" for _ in codes)
            query += f" AND UPPER(country_code) IN ({placeholders})"
            params.extend(codes)

        query += " ORDER BY latency ASC"

        rows = self._conn.execute(query, params).fetchall()
        return [
            Proxy(ip=r[0], port=r[1], protocol=r[2], country_code=r[3])
            for r in rows
        ]

    def count_active(self) -> int:
        """Return the total number of active proxies."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM proxies WHERE is_active = 1"
        ).fetchone()
        return row[0] if row else 0

    def get_all_unchecked_or_inactive(self) -> list[Proxy]:
        """Return proxies that need (re-)validation."""
        rows = self._conn.execute(
            """
            SELECT ip, port, protocol, country_code
              FROM proxies
             WHERE is_active = 0 OR last_checked IS NULL
            """
        ).fetchall()
        return [
            Proxy(ip=r[0], port=r[1], protocol=r[2], country_code=r[3])
            for r in rows
        ]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Proxy Scraper — multi-source
# ---------------------------------------------------------------------------


class ProxyScraper:
    """Scrape free proxy lists from multiple web sources.

    Sources:
      • free-proxy-list.net — HTTP / HTTPS proxies
      • socks-proxy.net     — SOCKS4 / SOCKS5 proxies
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})

    # -- public API ---------------------------------------------------------

    def scrape_all(self) -> list[Proxy]:
        """Scrape every configured source and return a merged list."""
        proxies: list[Proxy] = []

        proxies.extend(self._scrape_free_proxy_list())
        proxies.extend(self._scrape_socks_proxy())

        Logger.info(
            f"Total scraped: {Fore.WHITE}{len(proxies)}{Style.RESET_ALL} "
            f"proxies from {Fore.WHITE}{len(SOURCES)}{Style.RESET_ALL} sources"
        )
        return proxies

    # -- source: free-proxy-list.net ----------------------------------------

    def _scrape_free_proxy_list(self) -> list[Proxy]:
        """Parse HTTP/HTTPS proxies from free-proxy-list.net."""
        url = SOURCES["free-proxy-list"]
        Logger.info(
            f"Scraping {Fore.WHITE}{url}{Style.RESET_ALL} "
            f"[{Fore.YELLOW}HTTP/HTTPS{Style.RESET_ALL}]"
        )

        html = self._fetch(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table.table")
        if table is None:
            Logger.warn("Could not locate proxy table on free-proxy-list.net")
            return []

        proxies: list[Proxy] = []
        for row in table.select("tbody tr"):
            cols = row.find_all("td")
            if len(cols) < 7:
                continue

            ip_addr = cols[0].get_text(strip=True)
            port = cols[1].get_text(strip=True)
            country_code = cols[2].get_text(strip=True).upper() or "--"
            is_https = cols[6].get_text(strip=True).lower() == "yes"
            protocol = "HTTPS" if is_https else "HTTP"

            if ip_addr and port:
                proxies.append(Proxy(
                    ip=ip_addr,
                    port=port,
                    protocol=protocol,
                    country_code=country_code,
                ))

        Logger.info(
            f"  ↳ Parsed {Fore.WHITE}{len(proxies)}{Style.RESET_ALL} HTTP/HTTPS proxies"
        )
        return proxies

    # -- source: socks-proxy.net -------------------------------------------

    def _scrape_socks_proxy(self) -> list[Proxy]:
        """Parse SOCKS4/SOCKS5 proxies from socks-proxy.net."""
        url = SOURCES["socks-proxy"]
        Logger.info(
            f"Scraping {Fore.WHITE}{url}{Style.RESET_ALL} "
            f"[{Fore.YELLOW}SOCKS4/SOCKS5{Style.RESET_ALL}]"
        )

        html = self._fetch(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table.table")
        if table is None:
            Logger.warn("Could not locate proxy table on socks-proxy.net")
            return []

        proxies: list[Proxy] = []
        for row in table.select("tbody tr"):
            cols = row.find_all("td")
            if len(cols) < 5:
                continue

            ip_addr = cols[0].get_text(strip=True)
            port = cols[1].get_text(strip=True)
            country_code = cols[2].get_text(strip=True).upper() or "--"
            version_text = cols[4].get_text(strip=True).upper()

            if "SOCKS5" in version_text:
                protocol = "SOCKS5"
            elif "SOCKS4" in version_text:
                protocol = "SOCKS4"
            else:
                protocol = "SOCKS5"

            if ip_addr and port:
                proxies.append(Proxy(
                    ip=ip_addr,
                    port=port,
                    protocol=protocol,
                    country_code=country_code,
                ))

        Logger.info(
            f"  ↳ Parsed {Fore.WHITE}{len(proxies)}{Style.RESET_ALL} SOCKS proxies"
        )
        return proxies

    # -- helper -------------------------------------------------------------

    def _fetch(self, url: str) -> Optional[str]:
        """GET *url* and return the response body, or None on error."""
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            Logger.fail(f"Failed to reach {url}: {exc}")
            return None


# ---------------------------------------------------------------------------
# Proxy Validator — concurrent + DB-aware
# ---------------------------------------------------------------------------


class ProxyValidator:
    """Validate proxies concurrently and persist results to the database.

    A proxy is considered *valid* when:
      1. It reaches :data:`VALIDATION_URL` with HTTP 200.
      2. The returned origin IP differs from the machine's real IP.
    """

    def __init__(
        self,
        db: ProxyDatabase,
        timeout: int = DEFAULT_TIMEOUT,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        self._db = db
        self._timeout = timeout
        self._max_workers = max_workers
        self._real_ip: Optional[str] = None

    # -- public API ---------------------------------------------------------

    def validate(self, proxies: list[Proxy]) -> list[Proxy]:
        """Test *proxies* and return only those that pass validation.

        Results (active / inactive, latency) are written to the database.
        """
        self._real_ip = self._get_real_ip()
        if self._real_ip:
            Logger.info(
                f"Your real IP: {Fore.WHITE}{self._real_ip}{Style.RESET_ALL} "
                "(used to verify masking)"
            )
        else:
            Logger.warn("Could not determine real IP — skipping mask check")

        Logger.info(
            f"Validating {Fore.WHITE}{len(proxies)}{Style.RESET_ALL} proxies "
            f"with {Fore.WHITE}{self._max_workers}{Style.RESET_ALL} threads "
            f"(timeout={self._timeout}s) …"
        )

        valid: list[Proxy] = []
        start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_map = {
                pool.submit(self._check, proxy): proxy for proxy in proxies
            }
            for future in as_completed(future_map):
                proxy = future_map[future]
                try:
                    latency = future.result()
                except Exception:
                    latency = None

                if latency is not None:
                    Logger.success(
                        f"✓ {proxy} — {latency:.0f} ms"
                    )
                    self._db.mark_active(proxy, latency)
                    valid.append(proxy)
                else:
                    Logger.fail(f"✗ {proxy}")
                    self._db.mark_inactive(proxy)

        self._db.commit()

        elapsed = time.perf_counter() - start
        Logger.info(
            f"Validation done in {Fore.WHITE}{elapsed:.1f}s{Style.RESET_ALL} — "
            f"{Fore.GREEN}{len(valid)}{Style.RESET_ALL}/{len(proxies)} alive"
        )
        return valid

    # -- private helpers ----------------------------------------------------

    def _check(self, proxy: Proxy) -> Optional[float]:
        """Return latency in ms if the proxy is valid, else ``None``."""
        try:
            start = time.perf_counter()
            resp = requests.get(
                VALIDATION_URL,
                proxies=proxy.as_dict,
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout,
            )
            latency = (time.perf_counter() - start) * 1000

            if resp.status_code != 200:
                return None

            origin_ip = resp.json().get("origin", "")
            if self._real_ip and self._real_ip in origin_ip:
                return None

            return latency
        except (requests.RequestException, ValueError):
            return None

    @staticmethod
    def _get_real_ip() -> Optional[str]:
        """Resolve the machine's real public IP (best-effort)."""
        try:
            resp = requests.get(VALIDATION_URL, timeout=5)
            return resp.json().get("origin", "").split(",")[0].strip()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Proxy Rotator
# ---------------------------------------------------------------------------


@dataclass
class RotationResult:
    """Outcome of a single rotated request."""

    proxy: Proxy
    status_code: Optional[int] = None
    success: bool = False
    error: Optional[str] = None
    elapsed_ms: float = 0.0


class ProxyRotator:
    """Rotate through valid proxies and make requests to a target URL.

    Picks a proxy at random from the pool for each outgoing request.
    """

    def __init__(self, pool: list[Proxy], timeout: int = 8) -> None:
        if not pool:
            raise ValueError("Proxy pool is empty — nothing to rotate.")
        self._pool: list[Proxy] = list(pool)
        self._timeout = timeout

    # -- public API ---------------------------------------------------------

    def rotate(self, target_url: str, count: int = 1) -> list[RotationResult]:
        """Send *count* requests to *target_url* via random proxies."""
        Logger.info(
            f"Sending {Fore.WHITE}{count}{Style.RESET_ALL} request(s) → "
            f"{Fore.WHITE}{target_url}{Style.RESET_ALL}\n"
        )

        results: list[RotationResult] = []
        for idx in range(1, count + 1):
            proxy = random.choice(self._pool)
            result = self._send(proxy, target_url, idx, count)
            results.append(result)

        return results

    # -- private helpers ----------------------------------------------------

    def _send(
        self,
        proxy: Proxy,
        url: str,
        idx: int,
        total: int,
    ) -> RotationResult:
        """Perform a single GET request through *proxy*."""
        print(
            f"  {Fore.CYAN}[{idx}/{total}]{Style.RESET_ALL} "
            f"Proxy: {Fore.WHITE}{proxy}{Style.RESET_ALL}"
        )

        start = time.perf_counter()
        try:
            resp = requests.get(
                url,
                proxies=proxy.as_dict,
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout,
            )
            elapsed = (time.perf_counter() - start) * 1000
            result = RotationResult(
                proxy=proxy,
                status_code=resp.status_code,
                success=resp.status_code == 200,
                elapsed_ms=elapsed,
            )
        except requests.RequestException as exc:
            elapsed = (time.perf_counter() - start) * 1000
            result = RotationResult(
                proxy=proxy,
                success=False,
                error=str(exc),
                elapsed_ms=elapsed,
            )

        if result.success:
            Logger.success(f"HTTP {result.status_code} — {result.elapsed_ms:.0f} ms")
        else:
            detail = result.error or f"HTTP {result.status_code}"
            Logger.fail(f"{detail} — {result.elapsed_ms:.0f} ms")
        print()

        return result


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(results: list[RotationResult]) -> None:
    """Print a styled summary table of rotation results."""
    total = len(results)
    ok = sum(1 for r in results if r.success)
    fail = total - ok

    sep = f"  {Fore.CYAN}{'━' * 58}{Style.RESET_ALL}"
    print(f"\n{sep}")
    print(f"  {Fore.CYAN}  SUMMARY{Style.RESET_ALL}")
    print(sep)
    print(
        f"  {Fore.WHITE}  Total Requests :{Style.RESET_ALL} {total}  "
        f"{Fore.GREEN}✓ {ok}{Style.RESET_ALL}  "
        f"{Fore.RED}✗ {fail}{Style.RESET_ALL}"
    )

    if results:
        avg = sum(r.elapsed_ms for r in results) / total
        print(f"  {Fore.WHITE}  Avg Latency    :{Style.RESET_ALL} {avg:.0f} ms")

    protocols = {r.proxy.protocol for r in results}
    countries = {r.proxy.country_code for r in results}
    unique = {r.proxy.address for r in results}

    print(f"  {Fore.WHITE}  Unique Proxies :{Style.RESET_ALL} {len(unique)}")
    print(f"  {Fore.WHITE}  Protocols      :{Style.RESET_ALL} {', '.join(sorted(protocols))}")
    print(f"  {Fore.WHITE}  Countries      :{Style.RESET_ALL} {', '.join(sorted(countries))}")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for v2."""
    parser = argparse.ArgumentParser(
        prog="rotator_v2",
        description=(
            "Proxy Rotator v2.0 — scrape, validate, cache, and rotate "
            "free proxies (HTTP/HTTPS/SOCKS4/SOCKS5) with geo-filtering."
        ),
        epilog=(
            "Examples:\n"
            "  python rotator_v2.py -u https://example.com -c 5\n"
            "  python rotator_v2.py -u https://httpbin.org/ip -c 3 -p SOCKS5\n"
            "  python rotator_v2.py -u https://example.com -c 5 -C US,DE\n"
            "  python rotator_v2.py -u https://example.com -c 10 -p HTTP -C TR\n"
            "  python rotator_v2.py -u https://httpbin.org/ip --force-scrape"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-u", "--url",
        required=True,
        help="Target URL to send rotated requests to.",
    )
    parser.add_argument(
        "-c", "--count",
        type=int,
        default=3,
        help="Number of requests to send (default: 3).",
    )
    parser.add_argument(
        "-p", "--protocol",
        type=str,
        default=None,
        choices=["HTTP", "HTTPS", "SOCKS4", "SOCKS5"],
        help="Filter proxies by protocol (default: all).",
    )
    parser.add_argument(
        "-C", "--country",
        type=str,
        default=None,
        help="Comma-separated ISO country codes to filter (e.g. US,DE,TR).",
    )
    parser.add_argument(
        "-t", "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-proxy validation timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=MAX_WORKERS,
        help=f"Max concurrent validation threads (default: {MAX_WORKERS}).",
    )
    parser.add_argument(
        "--force-scrape",
        action="store_true",
        default=False,
        help="Force a fresh scrape even if the DB has enough active proxies.",
    )
    parser.add_argument(
        "--min-pool",
        type=int,
        default=MIN_POOL_SIZE,
        help=f"Minimum active proxies before auto-scraping (default: {MIN_POOL_SIZE}).",
    )

    return parser


# ---------------------------------------------------------------------------
# Orchestrator — ties everything together
# ---------------------------------------------------------------------------


class RotatorEngine:
    """High-level orchestrator for the scrape → validate → rotate pipeline.

    Decides whether to use cached proxies from SQLite or trigger a
    fresh scrape + validation cycle.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._db = ProxyDatabase()

    def run(self) -> None:
        """Execute the full pipeline and print results."""
        pool = self._build_pool()

        if not pool:
            Logger.fail("No usable proxies available. Exiting.")
            sys.exit(1)

        Logger.info(
            f"Rotation pool: {Fore.WHITE}{len(pool)}{Style.RESET_ALL} proxies"
        )
        print()

        rotator = ProxyRotator(pool=pool, timeout=self._args.timeout + 4)
        results = rotator.rotate(
            target_url=self._args.url,
            count=self._args.count,
        )

        print_summary(results)
        self._db.close()

    # -- private ------------------------------------------------------------

    def _build_pool(self) -> list[Proxy]:
        """Return a list of validated proxies ready for rotation.

        Uses the database cache when possible; triggers scraping and
        validation otherwise.
        """
        # 1. Check the cache first (unless --force-scrape).
        if not self._args.force_scrape:
            cached = self._db.get_active(
                protocol=self._args.protocol,
                country=self._args.country,
            )
            if len(cached) >= self._args.min_pool:
                Logger.db(
                    f"Cache hit — {Fore.WHITE}{len(cached)}{Style.RESET_ALL} "
                    f"active proxies in database (≥ {self._args.min_pool}). "
                    f"Skipping scrape."
                )
                return cached
            else:
                Logger.db(
                    f"Cache has {Fore.WHITE}{len(cached)}{Style.RESET_ALL} "
                    f"active proxies (< {self._args.min_pool}). "
                    f"Triggering scrape …"
                )
        else:
            Logger.db("--force-scrape flag set. Triggering fresh scrape …")

        print()

        # 2. Scrape from all sources.
        scraper = ProxyScraper()
        raw = scraper.scrape_all()
        if not raw:
            Logger.fail("No proxies scraped from any source.")
            return []

        # 3. Persist raw proxies to DB.
        for proxy in raw:
            self._db.upsert_proxy(proxy)
        self._db.commit()
        Logger.db(
            f"Persisted {Fore.WHITE}{len(raw)}{Style.RESET_ALL} proxies to database"
        )
        print()

        # 4. Validate (all unchecked + previously inactive).
        candidates = self._db.get_all_unchecked_or_inactive()

        # If user requested a specific protocol, only validate those.
        if self._args.protocol:
            candidates = [
                p for p in candidates
                if p.protocol.upper() == self._args.protocol.upper()
            ]

        validator = ProxyValidator(
            db=self._db,
            timeout=self._args.timeout,
            max_workers=self._args.threads,
        )
        validator.validate(candidates)
        print()

        # 5. Pull the final filtered pool from DB.
        return self._db.get_active(
            protocol=self._args.protocol,
            country=self._args.country,
        )


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    """Application entry point."""
    colorama_init(autoreset=False)
    Logger.banner()

    parser = build_parser()
    args = parser.parse_args()

    engine = RotatorEngine(args)
    engine.run()


if __name__ == "__main__":
    main()
