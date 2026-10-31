import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.models.database import close_db, init_db
from app.rag.errors import RagError
from app.routes.rag import router as rag_router

import uvicorn as uv

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Do not crash if YugabyteDB is still starting; requests retry init lazily
    # and /rag/health reports the problem.
    try:
        await init_db()
    except RagError as exc:
        logger.error("Database not ready at startup: %s", exc)
    yield
    await close_db()


app = FastAPI(lifespan=lifespan)

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(RagError)
async def rag_error_handler(_: Request, exc: RagError):
    if exc.status_code >= 500:
        logger.error("%s: %s", type(exc).__name__, exc)
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


app.include_router(rag_router)


if __name__ == "__main__":
    uv.run("app.main:app", port=8000, reload=True)
