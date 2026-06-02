# Research: YouTube Transcript Extraction (for SecBrain ingestion)

**Date:** 2026-06-02
**Goal:** Evaluate how to programmatically obtain YouTube video transcripts so SecBrain can ingest them (a future `vault_ingest_youtube` tool, sibling to `vault_ingest_url` / `vault_ingest_file`).

---

## TL;DR / Recommendation

- **Use [`youtube-transcript-api`](https://pypi.org/project/youtube-transcript-api/) as the primary path.** It's pure-Python, no API key, no browser, and reads YouTube's existing caption tracks (manual *and* auto-generated). Current version **1.2.4** (Jan 2026). ([PyPI](https://pypi.org/project/youtube-transcript-api/), [GitHub](https://github.com/jdepoix/youtube-transcript-api))
- **Use `yt-dlp` as a fallback** when the above breaks (YouTube's HTML/player changes more often than yt-dlp lags). It downloads `.vtt`/`.srt` subtitle files. ([yt-dlp guide](https://medium.com/@jallenswrx2016/using-yt-dlp-to-download-youtube-transcript-3479fccad9ea))
- **Do NOT rely on the official YouTube Data API v3 `captions.download`** — it only works for videos *you own* (or where the owner enabled third-party contributions), costs 200 quota units/call (~50/day), and requires OAuth. It is the wrong tool for ingesting arbitrary public videos. ([Google docs](https://developers.google.com/youtube/), [analysis](https://roundproxies.com/blog/scrape-youtube-captions/))
- **⚠️ The single biggest gotcha for SecBrain:** YouTube **blocks data-center / cloud-provider IPs** (AWS, GCP, Azure, DigitalOcean, etc.). Since SecBrain commonly runs on a **VPS**, transcript fetches will hit `RequestBlocked` / `IpBlocked` there even though they work fine on a laptop. Plan for a proxy or a local-fetch model. ([GitHub readme](https://github.com/jdepoix/youtube-transcript-api), [issue #511](https://github.com/jdepoix/youtube-transcript-api/issues/511))
- **No captions at all?** Last resort is ASR (Whisper) on the audio — heavier, but more accurate than auto-captions. Out of scope for a first cut. ([RankStudio](https://rankstudio.net/articles/en/get-youtube-transcript-llm-api))

---

## Method comparison

| Method | API key | Cloud/VPS-safe | Arbitrary public videos | Effort | Notes |
|---|---|---|---|---|---|
| **youtube-transcript-api** | No | ❌ (IP-blocked; needs proxy) | ✅ (if captions exist) | Low | Best default. Pure Python, no browser. |
| **yt-dlp** (`--write-auto-sub`) | No | ❌ (same IP issue) | ✅ | Low–Med | Most resilient to YT changes; external binary. |
| **YouTube Data API v3** (`captions.download`) | OAuth | ✅ | ❌ (owner-only) | High | Wrong tool for 3rd-party videos. 200 units/call. |
| **Hosted 3rd-party APIs** (Supadata, youtube-transcript.io, Apify) | Yes (paid) | ✅ (they proxy) | ✅ | Low | Offloads IP/proxy problem; recurring cost + external dependency. |
| **Whisper ASR on audio** | No | ✅ | ✅ (works w/o captions) | High | CPU/GPU heavy; for videos with no captions. |

---

## 1. youtube-transcript-api (recommended primary)

Pure-Python library by jdepoix. Reads the caption tracks YouTube already serves to the watch page — works for **auto-generated** captions too, supports translation, and needs **no API key and no headless browser**. ([GitHub](https://github.com/jdepoix/youtube-transcript-api))

- **Version:** 1.2.4 (released 2026-01-29). Python 3.8–3.14. ([PyPI](https://pypi.org/project/youtube-transcript-api/))
- ⚠️ **Breaking API change in v1.0:** older tutorials use the static `YouTubeTranscriptApi.get_transcript(video_id)`. The 1.x API is **instance-based** (`.fetch()` / `.list()`) and returns objects, not dicts. Pin a version and code to the new API.

### Install
```bash
pip install youtube-transcript-api
```

### Basic usage (v1.x)
```python
from youtube_transcript_api import YouTubeTranscriptApi

ytt_api = YouTubeTranscriptApi()
fetched = ytt_api.fetch("VIDEO_ID", languages=["en"])  # pass the ID, NOT the URL

for snippet in fetched:
    print(snippet.text, snippet.start, snippet.duration)
```

- Returns a `FetchedTranscript` containing `FetchedTranscriptSnippet` objects with `.text`, `.start`, `.duration`.
- `languages=[...]` is a priority list (e.g. `["de", "en"]` → German, else English).
- `.list(video_id)` enumerates available transcripts and exposes `.translate(...)` and manual-vs-generated filtering. ([README](https://github.com/jdepoix/youtube-transcript-api))

### Formatting to plain text (what SecBrain wants)
```python
from youtube_transcript_api.formatters import TextFormatter
plain = TextFormatter().format_transcript(fetched)   # also: JSONFormatter, SRTFormatter, WebVTTFormatter
```

### Video-ID extraction
SecBrain's tool should accept a URL and extract the 11-char ID — handle `watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, and `&`/`?` query params.

### Exceptions to handle
`TranscriptsDisabled`, `NoTranscriptFound`, `VideoUnavailable`, and the IP-related `RequestBlocked` / `IpBlocked` (see §5).

---

## 2. yt-dlp (recommended fallback)

`yt-dlp` is the most battle-tested YouTube extractor and tends to recover fastest after YouTube changes its internals — a good fallback when `youtube-transcript-api` returns parse errors. It writes subtitle files rather than returning Python objects. ([guide](https://medium.com/@jallenswrx2016/using-yt-dlp-to-download-youtube-transcript-3479fccad9ea))

### CLI
```bash
# auto-generated captions, no video download
yt-dlp --write-auto-sub --skip-download --sub-format vtt --sub-lang en "URL"
# human-authored captions
yt-dlp --write-sub --skip-download --sub-lang en "URL"
```

### Python
```python
import yt_dlp
opts = {
    "writeautomaticsub": True,
    "writesubtitles": True,
    "subtitleslangs": ["en"],
    "subtitlesformat": "vtt",
    "skip_download": True,
    "outtmpl": "/tmp/%(id)s.%(ext)s",
}
with yt_dlp.YoutubeDL(opts) as ydl:
    ydl.download(["URL"])
# then parse the .vtt (strip timestamps/cues -> plain text)
```

Output is WebVTT; strip cue headers, timestamps, and de-duplicate the rolling-caption lines that auto-subs produce. Wrapper libs like [`yt-dlp-transcript`](https://github.com/haron/yt-dlp-transcript) do this and handle playlists/channels. Trade-off: adds an external binary dependency and a parsing step.

---

## 3. Official YouTube Data API v3 — why it's the wrong tool

- `captions.download` returns the caption track **only if the authenticated user owns the video** (or the owner enabled third-party contributions, which is rare). For arbitrary public videos you'll get an insufficient-permissions error. ([roundproxies](https://roundproxies.com/blog/scrape-youtube-captions/))
- Requires **OAuth** (not just an API key), and costs **200 quota units per download** against a default **10,000/day** budget → ~50 transcripts/day. ([analysis](https://roundproxies.com/blog/scrape-youtube-captions/))
- **Conclusion:** unsuitable for SecBrain's "ingest any video I'm researching" use case. Only relevant if a user ingests their *own* channel's videos.

---

## 4. Hosted third-party transcript APIs

Services like **Supadata**, **youtube-transcript.io**, and **Apify** scrapers expose a simple HTTP endpoint and **handle the proxy/IP-blocking problem for you** server-side. ([Supadata](https://supadata.ai/youtube-transcript-api), [youtube-transcript.io](https://www.youtube-transcript.io/api), [Apify](https://apify.com/thescrapelab/apify-youtube-transcript-scraper-2-0/api/openapi))

- **Pro:** dead simple, cloud/VPS-safe, no proxy management.
- **Con:** API key + recurring cost, rate limits, and a hard external dependency / privacy consideration (you send video IDs to a third party). Conflicts with SecBrain's self-hosted, no-vector-DB-required ethos.
- **Fit:** a good *optional* configured backend, not the default.

---

## 5. ⚠️ The cloud/VPS IP-blocking problem (most important for SecBrain)

YouTube blocks IPs belonging to cloud providers (AWS, GCP, Azure, DigitalOcean, …). It recognizes the **ASN** of the server and returns a block before serving the page, raising `RequestBlocked` / `IpBlocked`. This works on a home/office connection but **fails on a VPS** — exactly where SecBrain is often deployed. ([README](https://github.com/jdepoix/youtube-transcript-api), [issue #511](https://github.com/jdepoix/youtube-transcript-api/issues/511), [SkipTheWatch](https://skipthewatch.com/blog/youtube-transcript-api-not-working))

**Mitigations (in order of recommendation):**

1. **Fetch from the user's machine, not the VPS.** SecBrain's architecture already runs an MCP layer next to Claude Code on the *local* machine. If transcript fetching happens client-side and only the *text* is sent to the vault, the VPS never touches YouTube and the block is sidestepped entirely. **This is the cleanest fix and fits SecBrain's design.**
2. **Rotating residential proxies.** The library has first-class `WebshareProxyConfig` support. Critically, it must be the **"Residential"** (rotating) tier — *not* "Static Residential" or datacenter, which are ASN-detectable just like the VPS. ([README](https://github.com/jdepoix/youtube-transcript-api))
   ```python
   from youtube_transcript_api.proxies import WebshareProxyConfig
   ytt_api = YouTubeTranscriptApi(proxy_config=WebshareProxyConfig(
       proxy_username="...", proxy_password="..."))
   ```
   Also `GenericProxyConfig` for arbitrary HTTP/SOCKS (incl. Tor). Caveat: even proxies get banned eventually; rotation is required, and YouTube's countermeasures evolve (e.g. PoToken in 2025). ([README](https://github.com/jdepoix/youtube-transcript-api), [Tor approach](https://shekhargulati.com/2025/01/05/using-a-tor-proxy-to-bypass-ip-restrictions/))
3. **Delegate to a hosted API (§4)** which proxies on its own infra.

---

## 6. No captions available → ASR fallback

If a video has no caption track, the only option is to download the audio (yt-dlp) and transcribe with **Whisper** (or faster-whisper). Reported WER ~5–10% on clean English vs YouTube auto-captions' 15–20%, but it's CPU/GPU-heavy and slow. ([RankStudio](https://rankstudio.net/articles/en/get-youtube-transcript-llm-api)) Recommend **deferring** this — out of scope for a first implementation.

---

## 7. Legal / Terms-of-Service notes

- YouTube's ToS generally prohibits accessing content except through the official API or the YouTube interface; bulk scraping of captions sits in a gray area. The **official API ToS** also requires not infringing third-party IP rights. ([API ToS](https://developers.google.com/youtube/terms/api-services-terms-of-service))
- Captions are copyrightable content owned by the uploader. For **personal research/note-taking** (SecBrain's use case — storing into a private vault) the risk profile is low, but the tool should not be marketed for redistribution/republishing of transcripts.
- **Recommendation:** document this clearly, keep ingestion personal-use/private-vault, and make any scraping behavior an explicit opt-in.

---

## 8. Proposed SecBrain integration (`vault_ingest_youtube`)

Mirror the existing `vault_ingest_url` flow (server.py:1215) so it slots into the same storage/index/Chroma plumbing:

1. **Signature:** `vault_ingest_youtube(url, project, title="", tags="", languages="en")`.
2. **Extract video ID** from the URL (regex covering watch/youtu.be/shorts/embed).
3. **Fetch:** try `youtube-transcript-api` (`.fetch()` → `TextFormatter`); on `RequestBlocked`/parse failure, fall back to `yt-dlp` if installed.
4. **Title:** if not provided, pull video title via yt-dlp metadata or the page `<title>` (reuse the existing `<title>` regex pattern at server.py:1245).
5. **Store** exactly like `vault_ingest_url`: write `raw/<md5(url)[:12]>.md` with the standard header, append to `raw/index.md`, `_chroma_add(...)`, `_upsert_entity(...)`, and log to `_system/log.md`. Cap at 50k chars like the others (server.py:1252) — long transcripts may need chunking.
6. **Dependencies:** add `youtube-transcript-api` (and optionally `yt-dlp`) as **optional extras** — match the lazy-import pattern used for `PyPI2`/PyPDF2 in `vault_ingest_file` (server.py:1295) so the core install stays light.
7. **VPS reality:** document the IP-block limitation in the tool docstring and surface a clear error pointing users to either run locally or configure a proxy.

**Effort:** small — it's ~40 lines reusing the ingest pipeline, plus an ID-parser helper and dependency handling. The real design decision is **where the fetch runs** (local vs VPS) given the IP-blocking constraint.

### Open questions for the maintainer
- Default backend: bundle `youtube-transcript-api` as a hard dep, or keep it an optional extra (lazy import) like PDF support?
- Where should the fetch execute given the VPS IP-block issue — push it client-side, or accept that proxies/hosted APIs are needed server-side?
- Chunking strategy for long transcripts vs. the current 50k-char cap?

---

## Sources

- [youtube-transcript-api — PyPI (v1.2.4)](https://pypi.org/project/youtube-transcript-api/)
- [youtube-transcript-api — GitHub (readme, API, proxies)](https://github.com/jdepoix/youtube-transcript-api)
- [Issue #511 — "YouTube is blocking requests from your IP" (even with Webshare)](https://github.com/jdepoix/youtube-transcript-api/issues/511)
- [Fixing the RequestBlocked error (Medium)](https://medium.com/@lhc1990/fixing-youtube-transcript-api-requestblocked-error-a-developers-guide-83c77c061e7b)
- [YouTube Transcript API Not Working: Fix Every Error (SkipTheWatch)](https://skipthewatch.com/blog/youtube-transcript-api-not-working)
- [Using yt-dlp to download YouTube transcripts (Medium)](https://medium.com/@jallenswrx2016/using-yt-dlp-to-download-youtube-transcript-3479fccad9ea)
- [yt-dlp-transcript wrapper (GitHub)](https://github.com/haron/yt-dlp-transcript)
- [How to Scrape Captions from YouTube — official API limits (roundproxies)](https://roundproxies.com/blog/scrape-youtube-captions/)
- [YouTube API Services Terms of Service (Google)](https://developers.google.com/youtube/terms/api-services-terms-of-service)
- [YouTube Transcript Guide: API, Python & ASR for LLMs (RankStudio)](https://rankstudio.net/articles/en/get-youtube-transcript-llm-api)
- [Using a Tor proxy to bypass IP restrictions (Shekhar Gulati)](https://shekhargulati.com/2025/01/05/using-a-tor-proxy-to-bypass-ip-restrictions/)
- [Supadata YouTube Transcript API](https://supadata.ai/youtube-transcript-api) · [youtube-transcript.io API](https://www.youtube-transcript.io/api) · [Apify YouTube Transcript Scraper](https://apify.com/thescrapelab/apify-youtube-transcript-scraper-2-0/api/openapi)
