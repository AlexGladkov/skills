import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from server import BearerGate, build

CONFIG = json.loads(Path(__file__).parents[1].joinpath('config.example.json').read_text())

class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate(self):
        calls, output = [], []
        async def downstream(*args): calls.append(True)
        async def send(message): output.append(message)
        app = BearerGate(downstream, 'x' * 32)
        await app({'type': 'http', 'headers': []}, None, send)
        self.assertEqual(output[0]['status'], 401)
        self.assertFalse(calls)
        await app({'type': 'http', 'headers': [(b'authorization', b'Bearer ' + b'x' * 32)]}, None, send)
        self.assertTrue(calls)

    async def test_surface_redaction_validation(self):
        _, mcp = build(CONFIG, 'secret-key', 'x' * 32)
        tools = await mcp.list_tools()
        self.assertEqual({t.name for t in tools}, {'find_movie', 'add_movie', 'find_releases', 'preview_download', 'download_movie', 'get_status'})
        with patch('server.execute', return_value={'title': 'secret-key'}):
            result = await mcp.call_tool('get_status', {'tmdb': 1})
            self.assertNotIn('secret-key', str(result))
        with patch('server.execute') as upstream:
            await mcp.call_tool('download_movie', {'tmdb': 1, 'release_id': 'http://evil/'})
            upstream.assert_not_called()

    async def test_serialized_operations(self):
        import time
        _, mcp = build(CONFIG, 'secret-key', 'x' * 32)
        active = 0
        peak = 0
        def run(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(.03)
            active -= 1
            return {}
        with patch('server.execute', side_effect=run):
            await asyncio.gather(*(mcp.call_tool('add_movie', {'tmdb': i}) for i in range(1, 4)))
        self.assertEqual(peak, 1)

if __name__ == '__main__': unittest.main()
