import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial
from itertools import cycle, islice
from types import TracebackType
from typing import Dict, List, Optional, Tuple, Union

from curl_cffi import requests
from lxml import html

from .exceptions import DuckDuckGoSearchException
from .utils import (
    _calculate_distance,
    _extract_vqd,
    _is_500_in_url,
    _normalize,
    _normalize_url,
    _text_extract_json,
    json_loads,
)

logger = logging.getLogger("duckduckgo_search.AsyncDDGS")


class AsyncDDGS:
    """DuckDuckgo_search async class to get search results from duckduckgo.com."""

    _executor: Optional[ThreadPoolExecutor] = None

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        proxies: Union[Dict[str, str], str, None] = None,
        timeout: Optional[int] = 10,
    ) -> None:
        """Initialize the AsyncDDGS object.

        Args:
            headers (dict, optional): Dictionary of headers for the HTTP client. Defaults to None.
            proxies (Union[dict, str], optional): Proxies for the HTTP client (can be dict or str). Defaults to None.
            timeout (int, optional): Timeout value for the HTTP client. Defaults to 10.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        self.proxies = {"all": proxies} if isinstance(proxies, str) else proxies
        self._asession = requests.AsyncSession(
            headers=headers, proxies=self.proxies, timeout=timeout, impersonate="chrome"
        )
        self._asession.headers["Referer"] = "https://duckduckgo.com/"
        self._parser: Optional[html.HTMLParser] = None

    async def __aenter__(self) -> "AsyncDDGS":
        """A context manager method that is called when entering the 'with' statement."""
        return self

    async def __aexit__(
        self, exc_type: Optional[BaseException], exc_val: Optional[BaseException], exc_tb: Optional[TracebackType]
    ) -> None:
        """Closes the session."""
        await self._asession.close()

    def _get_parser(self) -> html.HTMLParser:
        """Get HTML parser."""
        if self._parser is None:
            self._parser = html.HTMLParser(
                remove_blank_text=True, remove_comments=True, remove_pis=True, collect_ids=False
            )
        return self._parser

    def _get_executor(self, max_workers: int = 1) -> ThreadPoolExecutor:
        """Get ThreadPoolExecutor. Default max_workers=1, because >=2 leads to a big overhead"""
        if AsyncDDGS._executor is None:
            AsyncDDGS._executor = ThreadPoolExecutor(max_workers=max_workers)
        return AsyncDDGS._executor

    async def _aget_url(
        self,
        method: str,
        url: str,
        data: Optional[Union[Dict[str, str], bytes]] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> bytes:
        try:
            resp = await self._asession.request(method, url, data=data, params=params, stream=True)
            resp.raise_for_status()
            resp_content: bytes = await resp.acontent()
            logger.debug(f"_aget_url() {resp.url} {resp.status_code} {resp.elapsed:.2f} {len(resp_content)}")
            if _is_500_in_url(resp.url) or resp.status_code == 202:
                raise DuckDuckGoSearchException("Ratelimit")
            if resp.status_code == 200:
                return resp_content
        except Exception as ex:
            raise DuckDuckGoSearchException(f"_aget_url() {url} {type(ex).__name__}: {ex}") from ex
        raise DuckDuckGoSearchException(f"_aget_url() {url} return None. {params=} {data=}")

    async def _aget_vqd(self, keywords: str) -> str:
        """Get vqd value for a search query."""
        resp_content = await self._aget_url("POST", "https://duckduckgo.com", data={"q": keywords})
        return _extract_vqd(resp_content, keywords)

    async def text(
        self,
        keywords: str,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: Optional[str] = None,
        backend: str = "api",
        max_results: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """DuckDuckGo text search generator. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query.
            region: wt-wt, us-en, uk-en, ru-ru, etc. Defaults to "wt-wt".
            safesearch: on, moderate, off. Defaults to "moderate".
            timelimit: d, w, m, y. Defaults to None.
            backend: api, html, lite. Defaults to api.
                api - collect data from https://duckduckgo.com,
                html - collect data from https://html.duckduckgo.com,
                lite - collect data from https://lite.duckduckgo.com.
            max_results: max number of results. If None, returns results only from the first response. Defaults to None.

        Returns:
            List of dictionaries with search results, or None if there was an error.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        if backend == "api":
            results = await self._text_api(keywords, region, safesearch, timelimit, max_results)
        elif backend == "html":
            results = await self._text_html(keywords, region, safesearch, timelimit, max_results)
        elif backend == "lite":
            results = await self._text_lite(keywords, region, timelimit, max_results)
        return results

    async def _text_api(
        self,
        keywords: str,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """DuckDuckGo text search generator. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query.
            region: wt-wt, us-en, uk-en, ru-ru, etc. Defaults to "wt-wt".
            safesearch: on, moderate, off. Defaults to "moderate".
            timelimit: d, w, m, y. Defaults to None.
            max_results: max number of results. If None, returns results only from the first response. Defaults to None.

        Returns:
            List of dictionaries with search results.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        vqd = await self._aget_vqd(keywords)

        payload = {
            "q": keywords,
            "kl": region,
            "l": region,
            "vqd": vqd,
            "bing_market": region,
            "a": "ftsa",  # something
        }
        safesearch = safesearch.lower()
        if safesearch == "moderate":
            payload["ex"] = "-1"
        elif safesearch == "off":
            payload["ex"] = "-2"
        elif safesearch == "on":  # strict
            payload["p"] = "1"
        if timelimit:
            payload["df"] = timelimit

        cache = set()
        results: List[Optional[Dict[str, str]]] = [None] * 1100

        async def _text_api_page(s: int, page: int) -> None:
            priority = page * 100
            payload["s"] = f"{s}"
            resp_content = await self._aget_url("GET", "https://links.duckduckgo.com/d.js", params=payload)
            page_data = _text_extract_json(resp_content, keywords)

            for row in page_data:
                href = row.get("u", None)
                if href and href not in cache and href != f"http://www.google.com/search?q={keywords}":
                    cache.add(href)
                    body = _normalize(row["a"])
                    if body:
                        priority += 1
                        result = {
                            "title": _normalize(row["t"]),
                            "href": _normalize_url(href),
                            "body": body,
                        }
                        results[priority] = result

        tasks = [_text_api_page(0, 0)]
        if max_results:
            max_results = min(max_results, 500)
            tasks.extend(_text_api_page(s, i) for i, s in enumerate(range(23, max_results, 50), start=1))
        await asyncio.gather(*tasks)

        return list(islice(filter(None, results), max_results))

    async def _text_html(
        self,
        keywords: str,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """DuckDuckGo text search generator. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query.
            region: wt-wt, us-en, uk-en, ru-ru, etc. Defaults to "wt-wt".
            safesearch: on, moderate, off. Defaults to "moderate".
            timelimit: d, w, m, y. Defaults to None.
            max_results: max number of results. If None, returns results only from the first response. Defaults to None.

        Returns:
            List of dictionaries with search results.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        self._asession.headers["Referer"] = "https://html.duckduckgo.com/"
        safesearch_base = {"on": "1", "moderate": "-1", "off": "-2"}
        payload = {
            "q": keywords,
            "kl": region,
            "p": safesearch_base[safesearch.lower()],
            "o": "json",
            "api": "d.js",
        }
        if timelimit:
            payload["df"] = timelimit
        if max_results and max_results > 20:
            vqd = await self._aget_vqd(keywords)
            payload["vqd"] = vqd

        cache = set()
        results: List[Optional[Dict[str, str]]] = [None] * 1100

        async def _text_html_page(s: int, page: int) -> None:
            priority = page * 100
            payload["s"] = f"{s}"
            resp_content = await self._aget_url("POST", "https://html.duckduckgo.com/html", data=payload)
            if b"No  results." in resp_content:
                return

            tree = await self._asession.loop.run_in_executor(
                self._get_executor(), partial(html.document_fromstring, resp_content, self._get_parser())
            )

            for e in tree.xpath("//div[h2]"):
                href = e.xpath("./a/@href")
                href = href[0] if href else None
                if (
                    href
                    and href not in cache
                    and not href.startswith(
                        ("http://www.google.com/search?q=", "https://duckduckgo.com/y.js?ad_domain")
                    )
                ):
                    cache.add(href)
                    title = e.xpath("./h2/a/text()")
                    body = e.xpath("./a//text()")

                    priority += 1
                    result = {
                        "title": _normalize(title[0]),
                        "href": _normalize_url(href),
                        "body": _normalize("".join(body)),
                    }
                    results[priority] = result

        tasks = [_text_html_page(0, 0)]
        if max_results:
            max_results = min(max_results, 500)
            tasks.extend(_text_html_page(s, i) for i, s in enumerate(range(23, max_results, 50), start=1))
        await asyncio.gather(*tasks)

        return list(islice(filter(None, results), max_results))

    async def _text_lite(
        self,
        keywords: str,
        region: str = "wt-wt",
        timelimit: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """DuckDuckGo text search generator. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query.
            region: wt-wt, us-en, uk-en, ru-ru, etc. Defaults to "wt-wt".
            timelimit: d, w, m, y. Defaults to None.
            max_results: max number of results. If None, returns results only from the first response. Defaults to None.

        Returns:
            List of dictionaries with search results.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        self._asession.headers["Referer"] = "https://lite.duckduckgo.com/"
        payload = {
            "q": keywords,
            "o": "json",
            "api": "d.js",
            "kl": region,
        }
        if timelimit:
            payload["df"] = timelimit

        cache = set()
        results: List[Optional[Dict[str, str]]] = [None] * 1100

        async def _text_lite_page(s: int, page: int) -> None:
            priority = page * 100
            payload["s"] = f"{s}"
            resp_content = await self._aget_url("POST", "https://lite.duckduckgo.com/lite/", data=payload)
            if b"No more results." in resp_content:
                return

            tree = await self._asession.loop.run_in_executor(
                self._get_executor(), partial(html.document_fromstring, resp_content, self._get_parser())
            )

            data = zip(cycle(range(1, 5)), tree.xpath("//table[last()]//tr"))
            for i, e in data:
                if i == 1:
                    href = e.xpath(".//a//@href")
                    href = href[0] if href else None
                    if (
                        href is None
                        or href in cache
                        or href.startswith(("http://www.google.com/search?q=", "https://duckduckgo.com/y.js?ad_domain"))
                    ):
                        [next(data, None) for _ in range(3)]  # skip block(i=1,2,3,4)
                    else:
                        cache.add(href)
                        title = e.xpath(".//a//text()")[0]
                elif i == 2:
                    body = e.xpath(".//td[@class='result-snippet']//text()")
                    body = "".join(body).strip()
                elif i == 3:
                    priority += 1
                    result = {
                        "title": _normalize(title),
                        "href": _normalize_url(href),
                        "body": _normalize(body),
                    }
                    results[priority] = result

        tasks = [_text_lite_page(0, 0)]
        if max_results:
            max_results = min(max_results, 500)
            tasks.extend(_text_lite_page(s, i) for i, s in enumerate(range(23, max_results, 50), start=1))
        await asyncio.gather(*tasks)

        return list(islice(filter(None, results), max_results))

    async def images(
        self,
        keywords: str,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: Optional[str] = None,
        size: Optional[str] = None,
        color: Optional[str] = None,
        type_image: Optional[str] = None,
        layout: Optional[str] = None,
        license_image: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """DuckDuckGo images search. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query.
            region: wt-wt, us-en, uk-en, ru-ru, etc. Defaults to "wt-wt".
            safesearch: on, moderate, off. Defaults to "moderate".
            timelimit: Day, Week, Month, Year. Defaults to None.
            size: Small, Medium, Large, Wallpaper. Defaults to None.
            color: color, Monochrome, Red, Orange, Yellow, Green, Blue,
                Purple, Pink, Brown, Black, Gray, Teal, White. Defaults to None.
            type_image: photo, clipart, gif, transparent, line.
                Defaults to None.
            layout: Square, Tall, Wide. Defaults to None.
            license_image: any (All Creative Commons), Public (PublicDomain),
                Share (Free to Share and Use), ShareCommercially (Free to Share and Use Commercially),
                Modify (Free to Modify, Share, and Use), ModifyCommercially (Free to Modify, Share, and
                Use Commercially). Defaults to None.
            max_results: max number of results. If None, returns results only from the first response. Defaults to None.

        Returns:
            List of dictionaries with images search results.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        vqd = await self._aget_vqd(keywords)

        safesearch_base = {"on": "1", "moderate": "1", "off": "-1"}
        timelimit = f"time:{timelimit}" if timelimit else ""
        size = f"size:{size}" if size else ""
        color = f"color:{color}" if color else ""
        type_image = f"type:{type_image}" if type_image else ""
        layout = f"layout:{layout}" if layout else ""
        license_image = f"license:{license_image}" if license_image else ""
        payload = {
            "l": region,
            "o": "json",
            "q": keywords,
            "vqd": vqd,
            "f": f"{timelimit},{size},{color},{type_image},{layout},{license_image}",
            "p": safesearch_base[safesearch.lower()],
        }

        cache = set()
        results: List[Optional[Dict[str, str]]] = [None] * 600

        async def _images_page(s: int, page: int) -> None:
            priority = page * 100
            payload["s"] = f"{s}"
            resp_content = await self._aget_url("GET", "https://duckduckgo.com/i.js", params=payload)
            resp_json = json_loads(resp_content)

            page_data = resp_json.get("results", [])

            for row in page_data:
                image_url = row.get("image")
                if image_url and image_url not in cache:
                    cache.add(image_url)
                    priority += 1
                    result = {
                        "title": row["title"],
                        "image": _normalize_url(image_url),
                        "thumbnail": _normalize_url(row["thumbnail"]),
                        "url": _normalize_url(row["url"]),
                        "height": row["height"],
                        "width": row["width"],
                        "source": row["source"],
                    }
                    results[priority] = result

        tasks = [_images_page(0, page=0)]
        if max_results:
            max_results = min(max_results, 500)
            tasks.extend(_images_page(s, i) for i, s in enumerate(range(100, max_results, 100), start=1))
        await asyncio.gather(*tasks)

        return list(islice(filter(None, results), max_results))

    async def videos(
        self,
        keywords: str,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: Optional[str] = None,
        resolution: Optional[str] = None,
        duration: Optional[str] = None,
        license_videos: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """DuckDuckGo videos search. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query.
            region: wt-wt, us-en, uk-en, ru-ru, etc. Defaults to "wt-wt".
            safesearch: on, moderate, off. Defaults to "moderate".
            timelimit: d, w, m. Defaults to None.
            resolution: high, standart. Defaults to None.
            duration: short, medium, long. Defaults to None.
            license_videos: creativeCommon, youtube. Defaults to None.
            max_results: max number of results. If None, returns results only from the first response. Defaults to None.

        Returns:
            List of dictionaries with videos search results.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        vqd = await self._aget_vqd(keywords)

        safesearch_base = {"on": "1", "moderate": "-1", "off": "-2"}
        timelimit = f"publishedAfter:{timelimit}" if timelimit else ""
        resolution = f"videoDefinition:{resolution}" if resolution else ""
        duration = f"videoDuration:{duration}" if duration else ""
        license_videos = f"videoLicense:{license_videos}" if license_videos else ""
        payload = {
            "l": region,
            "o": "json",
            "q": keywords,
            "vqd": vqd,
            "f": f"{timelimit},{resolution},{duration},{license_videos}",
            "p": safesearch_base[safesearch.lower()],
        }

        cache = set()
        results: List[Optional[Dict[str, str]]] = [None] * 700

        async def _videos_page(s: int, page: int) -> None:
            priority = page * 100
            payload["s"] = f"{s}"
            resp_content = await self._aget_url("GET", "https://duckduckgo.com/v.js", params=payload)
            resp_json = json_loads(resp_content)

            page_data = resp_json.get("results", [])

            for row in page_data:
                if row["content"] not in cache:
                    cache.add(row["content"])
                    priority += 1
                    results[priority] = row

        tasks = [_videos_page(0, 0)]
        if max_results:
            max_results = min(max_results, 400)
            tasks.extend(_videos_page(s, i) for i, s in enumerate(range(59, max_results, 59), start=1))
        await asyncio.gather(*tasks)

        return list(islice(filter(None, results), max_results))

    async def news(
        self,
        keywords: str,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """DuckDuckGo news search. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query.
            region: wt-wt, us-en, uk-en, ru-ru, etc. Defaults to "wt-wt".
            safesearch: on, moderate, off. Defaults to "moderate".
            timelimit: d, w, m. Defaults to None.
            max_results: max number of results. If None, returns results only from the first response. Defaults to None.

        Returns:
            List of dictionaries with news search results.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        vqd = await self._aget_vqd(keywords)

        safesearch_base = {"on": "1", "moderate": "-1", "off": "-2"}
        payload = {
            "l": region,
            "o": "json",
            "noamp": "1",
            "q": keywords,
            "vqd": vqd,
            "p": safesearch_base[safesearch.lower()],
        }
        if timelimit:
            payload["df"] = timelimit

        cache = set()
        results: List[Optional[Dict[str, str]]] = [None] * 700

        async def _news_page(s: int, page: int) -> None:
            priority = page * 100
            payload["s"] = f"{s}"
            resp_content = await self._aget_url("GET", "https://duckduckgo.com/news.js", params=payload)
            resp_json = json_loads(resp_content)
            page_data = resp_json.get("results", [])

            for row in page_data:
                if row["url"] not in cache:
                    cache.add(row["url"])
                    image_url = row.get("image", None)
                    priority += 1
                    result = {
                        "date": datetime.fromtimestamp(row["date"], timezone.utc).isoformat(),
                        "title": row["title"],
                        "body": _normalize(row["excerpt"]),
                        "url": _normalize_url(row["url"]),
                        "image": _normalize_url(image_url),
                        "source": row["source"],
                    }
                    results[priority] = result

        tasks = [_news_page(0, 0)]
        if max_results:
            max_results = min(max_results, 200)
            tasks.extend(_news_page(s, i) for i, s in enumerate(range(29, max_results, 29), start=1))
        await asyncio.gather(*tasks)

        return list(islice(filter(None, results), max_results))

    async def answers(self, keywords: str) -> List[Dict[str, str]]:
        """DuckDuckGo instant answers. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query,

        Returns:
            List of dictionaries with instant answers results.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        payload = {
            "q": f"what is {keywords}",
            "format": "json",
        }
        resp_content = await self._aget_url("GET", "https://api.duckduckgo.com/", params=payload)
        page_data = json_loads(resp_content)

        results = []
        answer = page_data.get("AbstractText")
        url = page_data.get("AbstractURL")
        if answer:
            results.append(
                {
                    "icon": None,
                    "text": answer,
                    "topic": None,
                    "url": url,
                }
            )

        # related
        payload = {
            "q": f"{keywords}",
            "format": "json",
        }
        resp_content = await self._aget_url("GET", "https://api.duckduckgo.com/", params=payload)
        resp_json = json_loads(resp_content)
        page_data = resp_json.get("RelatedTopics", [])

        for row in page_data:
            topic = row.get("Name")
            if not topic:
                icon = row["Icon"].get("URL")
                results.append(
                    {
                        "icon": f"https://duckduckgo.com{icon}" if icon else "",
                        "text": row["Text"],
                        "topic": None,
                        "url": row["FirstURL"],
                    }
                )
            else:
                for subrow in row["Topics"]:
                    icon = subrow["Icon"].get("URL")
                    results.append(
                        {
                            "icon": f"https://duckduckgo.com{icon}" if icon else "",
                            "text": subrow["Text"],
                            "topic": topic,
                            "url": subrow["FirstURL"],
                        }
                    )

        return results

    async def suggestions(self, keywords: str, region: str = "wt-wt") -> List[Dict[str, str]]:
        """DuckDuckGo suggestions. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query.
            region: wt-wt, us-en, uk-en, ru-ru, etc. Defaults to "wt-wt".

        Returns:
            List of dictionaries with suggestions results.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        payload = {
            "q": keywords,
            "kl": region,
        }
        resp_content = await self._aget_url("GET", "https://duckduckgo.com/ac", params=payload)
        page_data = json_loads(resp_content)
        return [r for r in page_data]

    async def maps(
        self,
        keywords: str,
        place: Optional[str] = None,
        street: Optional[str] = None,
        city: Optional[str] = None,
        county: Optional[str] = None,
        state: Optional[str] = None,
        country: Optional[str] = None,
        postalcode: Optional[str] = None,
        latitude: Optional[str] = None,
        longitude: Optional[str] = None,
        radius: int = 0,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """DuckDuckGo maps search. Query params: https://duckduckgo.com/params.

        Args:
            keywords: keywords for query
            place: if set, the other parameters are not used. Defaults to None.
            street: house number/street. Defaults to None.
            city: city of search. Defaults to None.
            county: county of search. Defaults to None.
            state: state of search. Defaults to None.
            country: country of search. Defaults to None.
            postalcode: postalcode of search. Defaults to None.
            latitude: geographic coordinate (north-south position). Defaults to None.
            longitude: geographic coordinate (east-west position); if latitude and
                longitude are set, the other parameters are not used. Defaults to None.
            radius: expand the search square by the distance in kilometers. Defaults to 0.
            max_results: max number of results. If None, returns results only from the first response. Defaults to None.

        Returns:
            List of dictionaries with maps search results, or None if there was an error.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        vqd = await self._aget_vqd(keywords)

        # if longitude and latitude are specified, skip the request about bbox to the nominatim api
        if latitude and longitude:
            lat_t = Decimal(latitude.replace(",", "."))
            lat_b = Decimal(latitude.replace(",", "."))
            lon_l = Decimal(longitude.replace(",", "."))
            lon_r = Decimal(longitude.replace(",", "."))
            if radius == 0:
                radius = 1
        # otherwise request about bbox to nominatim api
        else:
            if place:
                params = {
                    "q": place,
                    "polygon_geojson": "0",
                    "format": "jsonv2",
                }
            else:
                params = {
                    "polygon_geojson": "0",
                    "format": "jsonv2",
                }
                if street:
                    params["street"] = street
                if city:
                    params["city"] = city
                if county:
                    params["county"] = county
                if state:
                    params["state"] = state
                if country:
                    params["country"] = country
                if postalcode:
                    params["postalcode"] = postalcode
            # request nominatim api to get coordinates box
            resp_content = await self._aget_url(
                "GET",
                "https://nominatim.openstreetmap.org/search.php",
                params=params,
            )
            if resp_content == b"[]":
                raise DuckDuckGoSearchException("maps() Сoordinates are not found, check function parameters.")
            resp_json = json_loads(resp_content)
            coordinates = resp_json[0]["boundingbox"]
            lat_t, lon_l = Decimal(coordinates[1]), Decimal(coordinates[2])
            lat_b, lon_r = Decimal(coordinates[0]), Decimal(coordinates[3])

        # if a radius is specified, expand the search square
        lat_t += Decimal(radius) * Decimal(0.008983)
        lat_b -= Decimal(radius) * Decimal(0.008983)
        lon_l -= Decimal(radius) * Decimal(0.008983)
        lon_r += Decimal(radius) * Decimal(0.008983)
        logger.debug(f"bbox coordinates\n{lat_t} {lon_l}\n{lat_b} {lon_r}")

        cache = set()
        results: List[Dict[str, str]] = []

        async def _maps_page(
            bbox: Tuple[Decimal, Decimal, Decimal, Decimal],
        ) -> Optional[List[Dict[str, str]]]:
            if max_results and len(results) >= max_results:
                return None
            lat_t, lon_l, lat_b, lon_r = bbox
            params = {
                "q": keywords,
                "vqd": vqd,
                "tg": "maps_places",
                "rt": "D",
                "mkexp": "b",
                "wiki_info": "1",
                "is_requery": "1",
                "bbox_tl": f"{lat_t},{lon_l}",
                "bbox_br": f"{lat_b},{lon_r}",
                "strict_bbox": "1",
            }
            resp_content = await self._aget_url("GET", "https://duckduckgo.com/local.js", params=params)
            resp_json = json_loads(resp_content)
            page_data = resp_json.get("results", [])

            page_results = []
            for res in page_data:
                r_name = f'{res["name"]} {res["address"]}'
                if r_name in cache:
                    continue
                else:
                    cache.add(r_name)
                    result = {
                        "title": res["name"],
                        "address": res["address"],
                        "country_code": res["country_code"],
                        "url": _normalize_url(res["website"]),
                        "phone": res["phone"] or "",
                        "latitude": res["coordinates"]["latitude"],
                        "longitude": res["coordinates"]["longitude"],
                        "source": _normalize_url(res["url"]),
                        "image": x.get("image", "") if (x := res["embed"]) else "",
                        "desc": x.get("description", "") if (x := res["embed"]) else "",
                        "hours": res["hours"] or "",
                        "category": res["ddg_category"] or "",
                        "facebook": f"www.facebook.com/profile.php?id={x}" if (x := res["facebook_id"]) else "",
                        "instagram": f"https://www.instagram.com/{x}" if (x := res["instagram_id"]) else "",
                        "twitter": f"https://twitter.com/{x}" if (x := res["twitter_id"]) else "",
                    }
                    page_results.append(result)

            return page_results

        # search squares (bboxes)
        start_bbox = (lat_t, lon_l, lat_b, lon_r)
        work_bboxes = [start_bbox]
        while work_bboxes:
            queue_bboxes = []  # for next iteration, at the end of the iteration work_bboxes = queue_bboxes
            tasks = []
            for bbox in work_bboxes:
                tasks.append(asyncio.create_task(_maps_page(bbox)))
                # if distance between coordinates > 1, divide the square into 4 parts and save them in queue_bboxes
                if _calculate_distance(lat_t, lon_l, lat_b, lon_r) > 1:
                    lat_t, lon_l, lat_b, lon_r = bbox
                    lat_middle = (lat_t + lat_b) / 2
                    lon_middle = (lon_l + lon_r) / 2
                    bbox1 = (lat_t, lon_l, lat_middle, lon_middle)
                    bbox2 = (lat_t, lon_middle, lat_middle, lon_r)
                    bbox3 = (lat_middle, lon_l, lat_b, lon_middle)
                    bbox4 = (lat_middle, lon_middle, lat_b, lon_r)
                    queue_bboxes.extend([bbox1, bbox2, bbox3, bbox4])

            # gather tasks using asyncio.wait_for and timeout
            with suppress(Exception):
                work_bboxes_results = await asyncio.gather(*[asyncio.wait_for(task, timeout=10) for task in tasks])

            for x in work_bboxes_results:
                if isinstance(x, list):
                    results.extend(x)
                elif isinstance(x, dict):
                    results.append(x)

            work_bboxes = queue_bboxes
            if not max_results or len(results) >= max_results or len(work_bboxes_results) == 0:
                break

        return list(islice(results, max_results))

    async def translate(
        self, keywords: Union[List[str], str], from_: Optional[str] = None, to: str = "en"
    ) -> List[Dict[str, str]]:
        """DuckDuckGo translate.

        Args:
            keywords: string or list of strings to translate.
            from_: translate from (defaults automatically). Defaults to None.
            to: what language to translate. Defaults to "en".

        Returns:
            List od dictionaries with translated keywords.

        Raises:
            DuckDuckGoSearchException: Raised when there is a generic exception during the API request.
        """
        assert keywords, "keywords is mandatory"

        vqd = await self._aget_vqd("translate")

        payload = {
            "vqd": vqd,
            "query": "translate",
            "to": to,
        }
        if from_:
            payload["from"] = from_

        results = []

        async def _translate_keyword(keyword: str) -> None:
            resp_content = await self._aget_url(
                "POST",
                "https://duckduckgo.com/translation.js",
                params=payload,
                data=keyword.encode(),
            )
            page_data = json_loads(resp_content)
            page_data["original"] = keyword
            results.append(page_data)

        if isinstance(keywords, str):
            keywords = [keywords]
        tasks = [_translate_keyword(keyword) for keyword in keywords]
        await asyncio.gather(*tasks)

        return results
