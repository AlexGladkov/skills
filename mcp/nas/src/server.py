"""NAS MCP: deliberately small API, no arbitrary upstream requests."""
import asyncio
import json
import re
import secrets
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from nasctl import Failure, Radarr, execute


class BearerGate:
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope['headers'])
            supplied = headers.get(b'authorization', b'')
            expected = ('Bearer ' + self.token).encode()
            if not secrets.compare_digest(supplied, expected):
                await send({'type': 'http.response.start', 'status': 401,
                            'headers': [(b'content-type', b'application/json'),
                                        (b'www-authenticate', b'Bearer')]})
                await send({'type': 'http.response.body', 'body': b'{"error":"Unauthorized"}'})
                return
        await self.app(scope, receive, send)


def build(config, key, token):
    if len(token) < 32 or not key:
        raise ValueError('Secrets missing or MCP token shorter than 32 characters')
    for name in ('resolution', 'max_size_bytes', 'min_free_bytes', 'max_queue_items', 'quality_profile_id'):
        if type(config[name]) is not int or config[name] <= 0:
            raise ValueError('Invalid numeric policy')
    re.compile(config['audio_title_pattern'])
    if not Path(config['root_folder']).is_absolute() or not config['allowed_sources']:
        raise ValueError('Invalid movie policy')
    hosts = config['allowed_hosts']
    if not hosts or '*' in hosts:
        raise ValueError('Explicit allowed_hosts required')
    mcp = FastMCP('nas', stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(
                      enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                      allowed_origins=config.get('allowed_origins', [])))
    lock = asyncio.Lock()

    async def operation(command, tmdb=None, **kwargs):
        if tmdb is not None and (type(tmdb) is not int or tmdb <= 0):
            return {'error': 'Positive TMDB ID required'}
        async with lock:
            try:
                # Fresh client per request; no shared urllib state across threads.
                result = await asyncio.to_thread(execute, Radarr(config, key),
                    SimpleNamespace(command=command, tmdb=tmdb, **kwargs))
                # Defense in depth for upstream-controlled titles and errors.
                encoded = json.dumps(result, ensure_ascii=False)
                for secret in (key, token):
                    encoded = encoded.replace(secret, '[redacted]')
                return json.loads(encoded)
            except Failure as error:
                message = str(error)
                for secret in (key, token):
                    message = message.replace(secret, '[redacted]')
                return {'error': message}
            except Exception:
                return {'error': 'Unexpected upstream response; check status before repeating a write'}

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

    @mcp.tool(annotations=read)
    async def find_movie(query: str) -> dict:
        """Look up movies by name. No download or registration."""
        if not 1 <= len(query.strip()) <= 200:
            return {'error': 'Query must contain 1–200 characters'}
        return {'movies': await operation('find', query=query.strip())}

    @mcp.tool(annotations=write)
    async def add_movie(tmdb: int) -> dict:
        """Register the selected movie using fixed policy; no automatic download."""
        return await operation('add', tmdb)

    @mcp.tool(annotations=read)
    async def find_releases(tmdb: int) -> dict:
        """Return only approved releases matching server quality/audio/size policy."""
        return {'releases': await operation('releases', tmdb)}

    @mcp.tool(annotations=read)
    async def preview_download(tmdb: int, release_id: str) -> dict:
        """Revalidate a release and limits without starting a download."""
        return await download(tmdb, release_id, False)

    async def download(tmdb, release_id, submit):
        if not re.fullmatch(r'[0-9a-f]{64}', release_id):
            return {'error': 'Use a release_id returned by find_releases'}
        return await operation('download', tmdb, release_id=release_id, execute=submit)

    @mcp.tool(annotations=write)
    async def download_movie(tmdb: int, release_id: str) -> dict:
        """Start the user-selected release after preview; recheck policy and queue."""
        return await download(tmdb, release_id, True)

    @mcp.tool(annotations=read)
    async def get_status(tmdb: int) -> dict:
        """Check Radarr file/queue status; does not verify Jellyfin or audio tracks."""
        return await operation('status', tmdb)

    return BearerGate(mcp.streamable_http_app(), token), mcp


def main():
    try:
        config = json.loads(Path('/config/config.json').read_text())
        key = Path('/run/secrets/radarr_api_key').read_text().strip()
        token = Path('/run/secrets/mcp_token').read_text().strip()
        app, _ = build(config, key, token)
    except Exception:
        raise SystemExit('Invalid configuration or unavailable secrets') from None
    uvicorn.run(app, host='0.0.0.0', port=8000, workers=1, access_log=False)


if __name__ == '__main__':
    main()
