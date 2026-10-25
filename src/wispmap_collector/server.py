"""HTTP client for the server's collector API, /api/collector/v1/ (spec doc 03 §3–4)."""
import httpx

from .common import VERSION


class ServerClient:
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 15.0):
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/api/collector/v1",
            headers={"Authorization": f"Bearer {token}",
                     "X-Wispmap-Collector": VERSION,
                     "User-Agent": f"wispmap-collector/{VERSION}"},
            timeout=timeout,
            transport=transport,          # tests pass httpx.MockTransport: no real network needed
        )

    def __repr__(self) -> str:            # never show the token in logs or tracebacks
        return f"ServerClient({self.http.base_url})"

    async def hello(self, payload: dict) -> dict:
        r = await self.http.post("/hello", json=payload)
        r.raise_for_status()
        return r.json()

    async def config(self, etag: str | None = None) -> tuple[dict | None, str | None]:
        """Returns (body, etag). body is None when the server says 304: nothing changed."""
        r = await self.http.get("/config", headers={"If-None-Match": etag} if etag else {})
        if r.status_code == 304:
            return None, etag
        r.raise_for_status()
        return r.json(), r.headers.get("ETag")

    async def ingest(self, gz_body: bytes) -> httpx.Response:
        """POST one gzipped batch. Returns the response: the uplink decides what each status means."""
        return await self.http.post("/ingest", content=gz_body,
                                    headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})

    async def aclose(self) -> None:
        await self.http.aclose()
