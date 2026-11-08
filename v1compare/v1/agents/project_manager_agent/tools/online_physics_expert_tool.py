from __future__ import annotations

import re
import time
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, TypeVar

import io

import requests
from smolagents import Tool
from openai import OpenAI, APIError, RateLimitError, APIConnectionError

from common.llm_config import DEFAULT_MODEL_ID, get_api_base_for_model, get_api_key_for_model, resolve_model_id

T = TypeVar("T")

_RETRY_EXCEPTIONS = (APIError, RateLimitError, APIConnectionError)


def _litellm_completion(**kwargs):
    import litellm

    litellm.drop_params = True
    return litellm.completion(**kwargs)

try:
    import fitz  # type: ignore[import-untyped]  # pymupdf
except Exception:  # pragma: no cover - depends on the runtime environment.
    fitz = None

_SEARCH_INSTRUCTIONS = """
You are a physics and materials science research expert with web search capability.
For EVERY query you MUST:

1. Run AT LEAST 6 targeted searches — the following are MANDATORY (not optional):
   - Search 1: main method/technique name + key process parameters
   - Search 2: alternative method or variant (different precursor, substrate, or temperature regime)
   - Search 3: REQUIRED — "state-of-the-art [topic] 2023 2024 2025" or "record low/high [key metric] [topic]" to find the MOST RECENT breakthroughs that earlier searches may miss. Recent top results often use different terminology than established methods.
   - Search 4: REQUIRED — repeat the topic with "site:arxiv.org" appended. You MUST execute this search and include any arXiv URLs found.
   - Search 5: REQUIRED — repeat the topic with "site:pmc.ncbi.nlm.nih.gov" appended. You MUST execute this search and include any PMC URLs found.
   - Search 6: deep-dive on a specific sub-parameter or emerging material/method found in earlier searches

2. For EACH distinct method found, extract ALL of the following that are available (do NOT skip):
   - Temperature (°C) — growth/reaction/annealing temperature
   - Pressure (mbar/Pa/Torr) — chamber pressure
   - Precursor species — exact chemical names and amounts (mg, sccm, mTorr partial pressure)
   - Carrier gas and flow rate (sccm)
   - Duration (min/h)
   - Substrate type
   - Grain/flake size (μm or mm)
   - Any yield or coverage (%)
   If a value is NOT found in any source, write "not reported" — NEVER use vague phrases like "optimized" or "appropriate".

3. Actively look for MULTIPLE valid methods — do not stop after finding one approach.

4. CRITICAL — include full arxiv.org URLs (https://arxiv.org/abs/XXXX.XXXXX) and pmc.ncbi.nlm.nih.gov URLs when found.
   Aim to cite AT LEAST 3 open-access papers (arXiv or PMC). If your first searches don't find them, do additional searches with "site:arxiv.org" or "site:pmc.ncbi.nlm.nih.gov" appended.

5. CRITICAL — Sources section: list every cited source as a plain bare URL on its own line:
   https://arxiv.org/abs/2403.12901
   https://pmc.ncbi.nlm.nih.gov/articles/PMC10416142/
   Do NOT use markdown link syntax [text](url) in the Sources section. Plain URLs only.

6. Structure your answer as:
   - Method overview (one line per distinct method)
   - Key Parameters per method (table with parameter | value | unit)
   - Mechanism (1-2 sentences per method)
   - Sources (plain bare URLs, one per line, arXiv/PMC preferred)

If parameters conflict across sources, explicitly note the discrepancy and cite both sources.
"""

_SYNTHESIS_SYSTEM = """
You are a materials science expert synthesizing web search results and full-text paper excerpts.

RULES — follow these strictly:

A. IDENTIFY METHODS: Determine whether sources describe ONE or MULTIPLE distinct approaches.
   - Present EACH distinct method as a separate numbered section.
   - Methods are distinct if they use different precursors, different temperature regimes (>100°C apart), or fundamentally different mechanisms.

B. FOR EACH METHOD, provide:
   1. Short title: "Approach N: <technique name> (<key condition>)"
   2. Parameter table — columns: parameter | value | unit | source
      SOURCE COLUMN RULES (critical):
      - If the value came from the web search summary → write "Web Search"
      - If the value came from a fetched paper excerpt (labeled "--- Paper: <url> ---") → write that paper's URL
      - If a value conflicts between sources → write both: "Web Search (700°C) vs PMC1234 (850°C)"
      - FORBIDDEN: "optimized", "appropriate", "controlled", "various" — these are not values.
        If a specific number is not found in any source, write "not reported".
   3. One-sentence mechanism.
   4. When to prefer this approach over others (tradeoffs).

C. END with a ranked source list (most relevant first), full URLs only.

Do NOT collapse distinct methods. Do NOT use placeholder words instead of numbers.
"""


def _strip_html(raw: str) -> str:
    """Remove HTML tags and decode entities, return plain text."""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _with_retry(fn: Callable[[], T], retries: int = 2) -> T:
    """Call fn, retrying on OpenAI API errors with exponential backoff."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except _RETRY_EXCEPTIONS as exc:
            if attempt == retries:
                raise
            wait = 2 ** attempt
            print(f"[OnlinePhysicsExpert] API error ({exc}), retrying in {wait}s…")
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Naive mode (kept for reference / fallback)
# ---------------------------------------------------------------------------

class OnlinePhysicsExpertTool(Tool):
    name = "online_physics_expert_naive"
    description = (
        "Naive single-shot web search for physics/materials science questions. "
        "Prefer online_physics_expert for higher-quality answers."
    )
    inputs = {
        "query": {"type": "string", "description": "The physics or materials science question to search and answer."},
    }
    output_type = "string"

    def __init__(
        self,
        *,
        model_id: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._model = resolve_model_id(model_id or DEFAULT_MODEL_ID)
        self._api_key = api_key or get_api_key_for_model(self._model)
        self._api_base = api_base if api_base is not None else get_api_base_for_model(self._model)
        if not self._api_key:
            raise RuntimeError("QUMUS_API_KEY not set.")
        client_kwargs: dict[str, str] = {"api_key": self._api_key}
        if self._api_base:
            client_kwargs["base_url"] = self._api_base
        self._client = OpenAI(**client_kwargs)

    def forward(self, query: str) -> str:
        resp = self._client.responses.create(
            model=self._model,
            tools=[{"type": "web_search_preview"}],
            input=[{"role": "user", "content": query}],
        )
        parts = []
        for item in resp.output:
            if hasattr(item, "content"):
                for c in item.content:
                    if hasattr(c, "text"):
                        parts.append(c.text)
        return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Enhanced mode (3-phase: search → full-text fetch → synthesis)
# ---------------------------------------------------------------------------

class EnhancedOnlinePhysicsExpertTool(Tool):
    name = "online_physics_expert"
    description = (
        "Multi-phase web search for physics/materials science questions. "
        "Performs multi-angle searches, fetches full text from open-access papers "
        "(arXiv, PMC), and synthesizes a precise answer with quantitative parameters and citations."
    )
    inputs = {
        "query": {"type": "string", "description": "The physics or materials science question to search and answer."},
    }
    output_type = "string"

    # arXiv: full URL or bare "arXiv:XXXX.XXXXX" citation
    _ARXIV_RE    = re.compile(r"https?://arxiv\.org/(?:abs|html|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)")
    _ARXIV_ID_RE = re.compile(r"arXiv:(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE)
    _PMC_RE      = re.compile(r"https?://(?:www\.)?pmc\.ncbi\.nlm\.nih\.gov/articles/(PMC\d+)")
    # Extract ANY https URL from text — bare, inside markdown [text](url), or footnotes
    _ANY_URL_RE  = re.compile(r"https?://[^\s\)\"'<>\]]+")
    _FULL_TEXT_CHARS  = 200000  # chars fed to per-paper gpt-4o-mini summarizer (~50k tokens)
    _FETCH_TIMEOUT    = 8       # seconds per URL (requests)

    def __init__(
        self,
        *,
        model_id: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._model = resolve_model_id(model_id or DEFAULT_MODEL_ID)
        self._api_key = api_key or get_api_key_for_model(self._model)
        self._api_base = api_base if api_base is not None else get_api_base_for_model(self._model)
        if not self._api_key:
            raise RuntimeError("QUMUS_API_KEY not set.")
        client_kwargs: dict[str, str] = {"api_key": self._api_key}
        if self._api_base:
            client_kwargs["base_url"] = self._api_base
        self._client = OpenAI(**client_kwargs)

    def _completion_text(self, *, temperature: float, messages: list[dict[str, str]]) -> str:
        kwargs: dict[str, object] = {
            "model": self._model,
            "api_key": self._api_key,
            "messages": messages,
            "temperature": temperature,
        }
        if self._api_base:
            kwargs["api_base"] = self._api_base
        resp = _litellm_completion(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    # -- public ---------------------------------------------------------------

    def forward(self, query: str) -> str:
        # Phase 1: enhanced multi-angle web search (OpenAI handles multi-round internally)
        print("[OnlinePhysicsExpert] Phase 1: web search…")
        resp = _with_retry(lambda: self._client.responses.create(
            model=self._model,
            tools=[{"type": "web_search_preview"}],
            instructions=_SEARCH_INSTRUCTIONS,
            input=[{"role": "user", "content": query}],
        ))
        search_text = self._extract_text(resp)

        # Phase 2: parallel full-text fetch for open-access papers
        urls = self._find_open_access_urls(search_text)

        # If no reliable open-access URLs from Phase 1, query arXiv API directly
        if not self._has_open_access(urls):
            print("[OnlinePhysicsExpert] Phase 2: no arXiv/PMC found, querying arXiv API…")
            arxiv_urls = self._arxiv_fallback_search(query)
            urls = list(dict.fromkeys(arxiv_urls + urls))

        if not urls:
            print("[OnlinePhysicsExpert] No URLs to fetch, returning search result.")
            return search_text

        print(f"[OnlinePhysicsExpert] Phase 2: fetching {len(urls)} paper(s) in parallel…")
        raw_texts = self._fetch_parallel(urls)
        if not raw_texts:
            return search_text

        # Phase 2b: per-paper gpt-4o-mini extraction (parallel) — reads up to 200k chars each
        print(f"[OnlinePhysicsExpert] Phase 2b: extracting from {len(raw_texts)} paper(s) with gpt-4o-mini…")
        full_texts = self._summarize_parallel(query, raw_texts)
        if not full_texts:
            return search_text

        # Phase 3: synthesize with gpt-4o (context is now compact summaries, not raw text)
        print(f"[OnlinePhysicsExpert] Phase 3: synthesizing {len(full_texts)} paper(s)…")
        return _with_retry(lambda: self._synthesize(query, search_text, full_texts))

    # -- private --------------------------------------------------------------

    def _extract_text(self, resp) -> str:
        parts = []
        for item in resp.output:
            if hasattr(item, "content"):
                for c in item.content:
                    if hasattr(c, "text"):
                        parts.append(c.text)
        return "".join(parts).strip()

    def _find_open_access_urls(self, text: str) -> list[str]:
        # Priority 1: arXiv HTML (always open access, convert abs→html for full text)
        arxiv_ids = list(dict.fromkeys(
            self._ARXIV_RE.findall(text) + self._ARXIV_ID_RE.findall(text)
        ))
        urls = [f"https://arxiv.org/html/{aid}" for aid in arxiv_ids]

        # Priority 2: PMC (always open access)
        pmc_ids = list(dict.fromkeys(self._PMC_RE.findall(text)))
        urls += [f"https://pmc.ncbi.nlm.nih.gov/articles/{pid}/" for pid in pmc_ids]

        # Priority 3: any other URL found anywhere in text — skip PDFs and noise domains
        all_urls = list(dict.fromkeys(self._ANY_URL_RE.findall(text)))
        already = set(urls)
        extras = [u for u in all_urls if u not in already
                  and not any(skip in u for skip in ("openai", "utm_source", "google", "bing"))
                  and not u.endswith(".pdf")]
        urls += extras

        return urls

    _PAYWALL_SIGNALS = ("sign in", "log in", "subscribe", "access denied", "purchase access", "institutional access")
    # These domains are always open access — skip paywall check
    _OPEN_ACCESS_DOMAINS = ("arxiv.org", "pmc.ncbi.nlm.nih.gov", "biorxiv.org", "medrxiv.org")

    def _has_open_access(self, urls: list[str]) -> bool:
        """Return True if any URL is from a reliably open-access domain."""
        return any(any(d in u for d in self._OPEN_ACCESS_DOMAINS) for u in urls)

    _PMC_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

    def _arxiv_fallback_search(self, query: str) -> list[str]:
        """Query PubMed Central API for open-access experimental papers; return PMC URLs."""
        # Keep first 8 content words + add experimental filter to avoid review-only papers
        short_query = " ".join(query.split()[:8]) + " experimental"
        try:
            r = requests.get(
                self._PMC_SEARCH_URL,
                params={"db": "pmc", "term": short_query, "retmax": 8,
                        "retmode": "json", "sort": "relevance"},
                timeout=12,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            ids = r.json().get("esearchresult", {}).get("idlist", [])
            urls = [f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pid}/" for pid in ids]
            print(f"[OnlinePhysicsExpert] PMC search returned {len(urls)} paper(s): {ids}")
            return urls
        except Exception as exc:
            print(f"[OnlinePhysicsExpert] PMC search failed: {exc}")
            return []

    def _fetch_arxiv_pdf(self, pdf_url: str) -> str | None:
        """Download an arXiv PDF and extract plain text via pymupdf. Returns None on failure."""
        if fitz is None:
            print("[OnlinePhysicsExpert] PyMuPDF is unavailable; skipping arXiv PDF parsing.")
            return None
        try:
            r = requests.get(pdf_url, timeout=self._FETCH_TIMEOUT * 2,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            doc = fitz.open(stream=io.BytesIO(r.content), filetype="pdf")
            pages = [page.get_text() for page in doc]
            doc.close()
            text = "\n".join(pages).strip()
            if len(text) < 200:
                return None
            _PDF_CHARS = 80000
            print(f"[OnlinePhysicsExpert] PDF parsed: {len(text)} chars from {pdf_url} (capped at {_PDF_CHARS})")
            return text[:_PDF_CHARS]
        except Exception as exc:
            print(f"[OnlinePhysicsExpert] PDF fetch/parse failed for {pdf_url}: {exc}")
            return None

    def _fetch_one(self, url: str) -> tuple[str, str] | None:
        """Fetch a single URL and return (url, truncated_plain_text) or None on failure/paywall."""
        try:
            r = requests.get(url, timeout=self._FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            # arXiv HTML pages don't exist for all papers — try PDF first, then abs
            if r.status_code == 404 and "arxiv.org/html/" in url:
                arxiv_id = url.split("arxiv.org/html/")[-1]
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
                print(f"[OnlinePhysicsExpert] arXiv HTML 404, trying PDF: {pdf_url}")
                pdf_text = self._fetch_arxiv_pdf(pdf_url)
                if pdf_text:
                    return pdf_url, pdf_text[: self._FULL_TEXT_CHARS]
                # PDF also failed — fall back to abs
                abs_url = f"https://arxiv.org/abs/{arxiv_id}"
                print(f"[OnlinePhysicsExpert] PDF failed, trying abs: {abs_url}")
                r = requests.get(abs_url, timeout=self._FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
                url = abs_url
            r.raise_for_status()
            text = _strip_html(r.text)
            is_open_access = any(d in url for d in self._OPEN_ACCESS_DOMAINS)
            # For paywalled domains: discard if short or contains login signals
            if not is_open_access and (
                len(text) < 1500 or any(sig in text[:3000].lower() for sig in self._PAYWALL_SIGNALS)
            ):
                print(f"[OnlinePhysicsExpert] Skipping (paywall/short): {url}")
                return None
            return url, text[: self._FULL_TEXT_CHARS]
        except Exception as exc:
            print(f"[OnlinePhysicsExpert] Fetch failed for {url}: {exc}")
            return None

    _PHASE2_TIMEOUT = 25  # total wall-clock seconds for all parallel fetches

    def _fetch_parallel(self, urls: list[str]) -> list[tuple[str, str]]:
        results = []
        pool = ThreadPoolExecutor(max_workers=min(len(urls), 8))
        futures = {pool.submit(self._fetch_one, u): u for u in urls}
        try:
            for fut in as_completed(futures, timeout=self._PHASE2_TIMEOUT):
                result = fut.result()
                if result:
                    results.append(result)
        except TimeoutError:
            print("[OnlinePhysicsExpert] Phase 2 fetch timed out, using partial results.")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return results

    _SUMMARIZE_TIMEOUT = 60  # total wall-clock seconds for all parallel per-paper LLM calls

    def _build_extract_prompt(self, query: str) -> str:
        return (
            f"You are a materials science expert extracting information from a research paper "
            f"to help answer this specific query:\n\n"
            f"  QUERY: {query}\n\n"
            f"Instructions:\n"
            f"1. Read the paper and identify sections directly relevant to the query.\n"
            f"2. Extract ALL specific numerical values relevant to the query with their units — "
            f"focus on whatever dimensions the query is asking about "
            f"(e.g. if the query is about growth methods: temperatures, pressures, precursor species "
            f"and amounts, carrier gas flow rates, durations, substrate types, grain/flake sizes, yields; "
            f"if about device properties: mobility, on/off ratio, gate voltage, contact resistance, etc.; "
            f"if about optical properties: wavelength, quantum yield, linewidth, etc.). "
            f"Adapt the extracted parameters to match what the query is actually asking.\n"
            f"3. For each extracted parameter, state exactly what it refers to and its value.\n"
            f"4. If a parameter type that is clearly relevant to the query is NOT mentioned in the paper, "
            f"explicitly write it as: '[parameter name]: not reported in this paper'.\n"
            f"5. NEVER invent, estimate, or infer values that are not explicitly stated in the paper.\n"
            f"6. Output as a structured list, NOT a flowing paragraph.\n"
            f"7. If the paper has NO relevant content for this query at all, "
            f"reply exactly: NOT RELEVANT"
        )

    def _summarize_one(self, query: str, url: str, text: str) -> tuple[str, str] | None:
        """Ask gpt-4o-mini to extract query-relevant parameters from a single paper's full text."""
        try:
            summary = self._completion_text(
                temperature=0.0,
                messages=[
                    {"role": "system", "content": self._build_extract_prompt(query)},
                    {"role": "user", "content": f"Paper URL: {url}\n\nFull text:\n{text}"},
                ],
            )
            if summary.upper().startswith("NOT RELEVANT"):
                print(f"[OnlinePhysicsExpert] Paper not relevant, skipping: {url}")
                return None
            return url, summary
        except Exception as exc:
            print(f"[OnlinePhysicsExpert] Summarization failed for {url}: {exc}")
            return None

    def _summarize_parallel(self, query: str, raw_texts: list[tuple[str, str]]) -> list[tuple[str, str]]:
        results = []
        pool = ThreadPoolExecutor(max_workers=min(len(raw_texts), 8))
        futures = {pool.submit(self._summarize_one, query, url, text): url
                   for url, text in raw_texts}
        try:
            for fut in as_completed(futures, timeout=self._SUMMARIZE_TIMEOUT):
                result = fut.result()
                if result:
                    results.append(result)
        except TimeoutError:
            print("[OnlinePhysicsExpert] Phase 2b timed out, using partial results.")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return results

    def _synthesize(self, query: str, search_text: str, full_texts: list[tuple[str, str]]) -> str:
        papers_block = "\n\n".join(
            f"--- Paper: {url} ---\n{text}" for url, text in full_texts
        )
        user_content = (
            f"Question: {query}\n\n"
            f"=== Web Search Summary ===\n{search_text}\n\n"
            f"=== Extracted Parameters from Papers ({len(full_texts)} papers, "
            f"extracted by a secondary LLM from full text) ===\n{papers_block}"
        )
        return self._completion_text(
            temperature=0.1,
            messages=[
                {"role": "system", "content": _SYNTHESIS_SYSTEM},
                {"role": "user", "content": user_content},
            ],
        )
