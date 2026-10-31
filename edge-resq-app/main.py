from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes.home import router as home_router
from logs.middleware_logs import log_requests
import uvicorn as uv

app = FastAPI()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(log_requests)


app.include_router(home_router)


if __name__ == "__main__":
    uv.run("main:app", port=8000, reload=True)
