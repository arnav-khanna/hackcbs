"""HTTP interface for incident classification."""
from fastapi import APIRouter

from app.classification_agent import classify_sos
from app.schemas import ClassificationRequest, ClassificationResponse

router = APIRouter(prefix="/classification", tags=["classification"])


@router.post("/classify", response_model=ClassificationResponse)
async def classify(body: ClassificationRequest):
    """Classify an SOS against ingested reports and return cited guidance and an alert."""
    return await classify_sos(body.text, body.top_k)
