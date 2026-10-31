"""FastAPI ingress, inbox long-polling, session control and local diagnostics."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import orjson
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .engine import Transport
from .links import SimulationLink


def create_app(config, link=None):
    transport = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal transport
        actual_link = link
        if actual_link is None:
            if config.backend == 'simulation':
                actual_link = SimulationLink(config)
            else:
                from .radio import RadioLink
                actual_link = RadioLink(config)
        transport = Transport(config, actual_link)
        app.state.transport = transport
        await transport.start()
        try:
            yield
        finally:
            await transport.close()

    app = FastAPI(title='Pluto JSON Transport', version='1.0.0', lifespan=lifespan)

    @app.get('/', include_in_schema=False)
    async def console():
        return FileResponse(Path(__file__).with_name('console.html'), headers={'Cache-Control': 'no-store'})

    @app.get('/health')
    async def health():
        return JSONResponse(transport.health(), status_code=200 if transport.running else 503)

    @app.post('/send')
    async def send(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > config.limits['max_message_bytes']:
                raise HTTPException(413, 'Request exceeds max_message_bytes')
            body.extend(chunk)
        try:
            obj = orjson.loads(body)
            if not isinstance(obj, dict):
                raise ValueError('Body must be a JSON object')
            return await transport.send(obj)
        except (ValueError, orjson.JSONDecodeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/receive')
    async def receive(request: Request, timeout_ms: int = Query(0, ge=0, le=60000)):
        if not timeout_ms:
            try:
                return transport.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return Response(status_code=204)
        # Cancel a pending inbox read when a browser closes or reloads, so an
        # abandoned long poll cannot consume a later message invisibly.
        pending = asyncio.create_task(transport.inbox.get())
        deadline = asyncio.get_running_loop().time() + timeout_ms/1000
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return Response(status_code=204)
                done, _ = await asyncio.wait({pending}, timeout=min(0.2, remaining))
                if done:
                    return pending.result()
                if await request.is_disconnected():
                    return Response(status_code=499)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    @app.get('/session')
    async def session_state():
        return {'enabled': config.session['enabled'], 'established': transport.session_ready.is_set(),
                'key_exchange': config.key_exchange['algorithm'],
                'key_distribution': config.key_distribution['enabled']}

    @app.post('/session')
    async def establish_session():
        try:
            return await transport.establish()
        except TimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc

    return app
