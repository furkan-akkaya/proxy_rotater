#!/usr/bin/env python3
"""
Proxy Rotator — Terminal-based rotating proxy tool.

Scrapes free proxies from the web, validates them concurrently,
and rotates through working proxies to make requests to a target URL.

Author : Furkan Akkaya
License: MIT
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
from colorama import Fore, Style, init as colorama_init

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROXY_SOURCE_URL = "https://free-proxy-list.net/"
VALIDATION_URL = "http://httpbin.org/ip"
DEFAULT_TIMEOUT = 4          # seconds — per-proxy validation timeout
MAX_WORKERS = 40             # concurrent threads for validation
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proxy:
    """Immutable representation of a single proxy server."""

    ip: str
    port: str

    @property
    def address(self) -> str:
        """Return the ``host:port`` string."""
        return f"{self.ip}:{self.port}"

    @property
    def as_dict(self) -> dict[str, str]:
        """Return the proxy mapping expected by *requests*."""
        url = f"http://{self.address}"
        return {"http": url, "https": url}

    def __str__(self) -> str:  # noqa: D105
        return self.address


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
        "banner":  f"{Fore.MAGENTA}",
    }

    @classmethod
    def info(cls, msg: str) -> None:
        """Print an informational message (cyan)."""
        print(f"  {cls._PREFIXES['info']}  {msg}")

    @classmethod
    def success(cls, msg: str) -> None:
        """Print a success message (green)."""
        print(f"  {cls._PREFIXES['success']}  {Fore.GREEN}{msg}{Style.RESET_ALL}")

    @classmethod
    def fail(cls, msg: str) -> None:
        """Print a failure message (red)."""
        print(f"  {cls._PREFIXES['fail']}  {Fore.RED}{msg}{Style.RESET_ALL}")

    @classmethod
    def warn(cls, msg: str) -> None:
        """Print a warning message (yellow)."""
        print(f"  {cls._PREFIXES['warn']}  {Fore.YELLOW}{msg}{Style.RESET_ALL}")

    @classmethod
    def banner(cls) -> None:
        """Print the application banner."""
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
{Fore.YELLOW}  ⚡  Free Proxy Scraper, Validator & Rotator
  ⚡  Author: Furkan Akkaya{Style.RESET_ALL}
"""
        print(art)


# ---------------------------------------------------------------------------
# Proxy Scraper
# ---------------------------------------------------------------------------


class ProxyScraper:
    """Scrape free proxy lists from the web.

    Currently supports *free-proxy-list.net*.  The class can be extended
    with additional sources by adding new ``_scrape_*`` methods.
    """

    def __init__(self, url: str = PROXY_SOURCE_URL) -> None:
        self._url = url

    # -- public API ---------------------------------------------------------

    def scrape(self) -> list[Proxy]:
        """Fetch and return a list of :class:`Proxy` objects."""
        Logger.info(f"Scraping proxies from {Fore.WHITE}{self._url}{Style.RESET_ALL}")

        try:
            response = requests.get(
                self._url,
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            Logger.fail(f"Failed to reach proxy source: {exc}")
            return []

        proxies = self._parse(response.text)
        Logger.info(
            f"Scraped {Fore.WHITE}{len(proxies)}{Style.RESET_ALL} raw proxies"
        )
        return proxies

    # -- private helpers ----------------------------------------------------

    @staticmethod
    def _parse(html: str) -> list[Proxy]:
        """Parse the HTML table and return :class:`Proxy` instances."""
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table.table")
        if table is None:
            Logger.warn("Could not locate proxy table in page HTML")
            return []

        proxies: list[Proxy] = []
        rows = table.select("tbody tr")
        for row in rows:
            cols = row.find_all("td")
            if len(cols) >= 2:
                ip_addr = cols[0].get_text(strip=True)
                port = cols[1].get_text(strip=True)
                if ip_addr and port:
                    proxies.append(Proxy(ip=ip_addr, port=port))
        return proxies


# ---------------------------------------------------------------------------
# Proxy Validator
# ---------------------------------------------------------------------------


class ProxyValidator:
    """Validate proxies concurrently using a thread pool.

    A proxy is considered *valid* when:
    1. It successfully reaches :data:`VALIDATION_URL` with HTTP 200.
    2. The returned IP differs from the machine's real public IP
       (i.e., the proxy actually masks the user).
    """

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        self._timeout = timeout
        self._max_workers = max_workers
        self._real_ip: Optional[str] = None

    # -- public API ---------------------------------------------------------

    def validate(self, proxies: list[Proxy]) -> list[Proxy]:
        """Return only the proxies that pass the validation check."""
        self._real_ip = self._get_real_ip()
        if self._real_ip:
            Logger.info(
                f"Your real IP: {Fore.WHITE}{self._real_ip}{Style.RESET_ALL} "
                "(will be used to verify masking)"
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
                    result = future.result()
                except Exception:
                    result = False

                if result:
                    Logger.success(f"✓ {proxy.address}")
                    valid.append(proxy)
                else:
                    Logger.fail(f"✗ {proxy.address}")

        elapsed = time.perf_counter() - start
        Logger.info(
            f"Validation complete in {Fore.WHITE}{elapsed:.1f}s{Style.RESET_ALL} — "
            f"{Fore.GREEN}{len(valid)}{Style.RESET_ALL}/{len(proxies)} proxies alive"
        )
        return valid

    # -- private helpers ----------------------------------------------------

    def _check(self, proxy: Proxy) -> bool:
        """Return *True* if *proxy* responds with 200 and masks the IP."""
        try:
            resp = requests.get(
                VALIDATION_URL,
                proxies=proxy.as_dict,
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return False

            origin_ip = resp.json().get("origin", "")
            # Proxy must NOT expose our real IP.
            if self._real_ip and self._real_ip in origin_ip:
                return False

            return True
        except (requests.RequestException, ValueError):
            return False

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

    The rotator picks a proxy at random from the validated pool for
    each outgoing request.
    """

    def __init__(self, pool: list[Proxy], timeout: int = 8) -> None:
        if not pool:
            raise ValueError("Proxy pool is empty — nothing to rotate.")
        self._pool: list[Proxy] = list(pool)
        self._timeout = timeout

    # -- public API ---------------------------------------------------------

    def rotate(self, target_url: str, count: int = 1) -> list[RotationResult]:
        """Send *count* requests to *target_url* through random proxies."""
        Logger.info(
            f"Sending {Fore.WHITE}{count}{Style.RESET_ALL} request(s) → "
            f"{Fore.WHITE}{target_url}{Style.RESET_ALL}"
        )
        print()

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
        header = (
            f"  {Fore.CYAN}[{idx}/{total}]{Style.RESET_ALL} "
            f"Proxy: {Fore.WHITE}{proxy.address}{Style.RESET_ALL}"
        )
        print(header)

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
            Logger.success(
                f"HTTP {result.status_code} — {result.elapsed_ms:.0f} ms"
            )
        else:
            detail = result.error or f"HTTP {result.status_code}"
            Logger.fail(f"{detail} — {result.elapsed_ms:.0f} ms")

        print()
        return result


# ---------------------------------------------------------------------------
# Summary Printer
# ---------------------------------------------------------------------------


def print_summary(results: list[RotationResult]) -> None:
    """Print a styled summary table of rotation results."""
    total = len(results)
    ok = sum(1 for r in results if r.success)
    fail = total - ok

    print(f"\n  {Fore.CYAN}{'━' * 56}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}  SUMMARY{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{'━' * 56}{Style.RESET_ALL}")
    print(
        f"  {Fore.WHITE}  Total Requests :{Style.RESET_ALL} {total}  "
        f"{Fore.GREEN}✓ {ok}{Style.RESET_ALL}  "
        f"{Fore.RED}✗ {fail}{Style.RESET_ALL}"
    )

    if results:
        avg_ms = sum(r.elapsed_ms for r in results) / len(results)
        print(
            f"  {Fore.WHITE}  Avg Latency    :{Style.RESET_ALL} "
            f"{avg_ms:.0f} ms"
        )

    proxies_used = {r.proxy.address for r in results}
    print(
        f"  {Fore.WHITE}  Unique Proxies :{Style.RESET_ALL} "
        f"{len(proxies_used)}"
    )
    print(f"  {Fore.CYAN}{'━' * 56}{Style.RESET_ALL}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="rotator",
        description=(
            "Proxy Rotator — scrape, validate, and rotate free proxies "
            "to make anonymous requests to any target URL."
        ),
        epilog=(
            "Examples:\n"
            "  python rotator.py -u https://example.com -c 5\n"
            "  python rotator.py --url https://httpbin.org/ip --count 3 --timeout 5\n"
            "  python rotator.py --url https://example.com --count 10 --threads 60"
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
        "-t", "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-proxy validation timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=MAX_WORKERS,
        help=f"Max concurrent threads for validation (default: {MAX_WORKERS}).",
    )

    return parser


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    """Application entry point."""
    colorama_init(autoreset=False)

    parser = build_parser()
    args = parser.parse_args()

    Logger.banner()

    # 1. Scrape
    scraper = ProxyScraper()
    raw_proxies = scraper.scrape()
    if not raw_proxies:
        Logger.fail("No proxies scraped. Exiting.")
        sys.exit(1)

    print()

    # 2. Validate
    validator = ProxyValidator(timeout=args.timeout, max_workers=args.threads)
    valid_proxies = validator.validate(raw_proxies)
    if not valid_proxies:
        Logger.fail("No working proxies found. Try again later.")
        sys.exit(1)

    print()

    # 3. Rotate
    rotator = ProxyRotator(pool=valid_proxies)
    results = rotator.rotate(target_url=args.url, count=args.count)

    # 4. Summary
    print_summary(results)


if __name__ == "__main__":
    main()
