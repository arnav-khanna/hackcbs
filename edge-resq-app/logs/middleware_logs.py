import logging 
from fastapi import Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

async def log_requests(request:Request,call_next):
    logger.info(f'Incoming request: {request.method}{request.url}')
    response = await call_next(request)
    logger.info(f'Response status code: {response.status_code}')
    return response 