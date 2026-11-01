"""HTTP access to the MCP safety tools, combined with SOS classification.

POST /safety/assess          SOS text (+ optional location) -> assessment + guidance
POST /safety/places          nearest safe places
POST /safety/hazards         earthquakes / rain / wind nearby
POST /safety/geocode         place name -> coordinates
GET  /safety/emergency-numbers/{country_code}

The tools are imported directly from app.mcp_tools (same code the MCP server
exposes), so there is one implementation and no extra network hop.
"""
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.classification_agent import classify_incident, classify_sos
from app.mcp_tools import (
    DISASTER_PROFILES,
    calm_safety_plan,
    check_nearby_hazards,
    find_safe_locations,
    geocode_place,
    get_emergency_numbers,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/safety", tags=["safety"])


# ---------------------------------------------------------------- schemas ---
class AssessRequest(BaseModel):
    text: str = Field(..., max_length=2000, description="The SOS message.")
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    place_name: str | None = Field(
        default=None, max_length=200,
        description="Used only when latitude/longitude are not given.",
    )
    disaster_type: str | None = Field(
        default=None, description="Optional override of the classified incident type."
    )
    country_code: str | None = Field(default=None, max_length=2, description="e.g. IN, US")
    top_k: int | None = Field(default=None, ge=1, le=20)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be blank")
        return v

    @model_validator(mode="after")
    def _coordinate_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be given together.")
        return self


class PlacesRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    disaster_type: str = "general"
    radius_m: int = Field(default=3000, ge=200, le=10000)
    limit: int = Field(default=5, ge=1, le=10)


class HazardsRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    radius_km: int = Field(default=300, ge=10, le=1000)


class GeocodeRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=200)


# ---------------------------------------------------------------- helpers ---
def _tool_or_http_error(result: dict, status: int = 502) -> dict:
    if not result.get("ok", False):
        raise HTTPException(status_code=status, detail=result.get("error", "Tool failed."))
    return result


# ----------------------------------------------------------------- routes ---
@router.post("/assess")
async def assess(body: AssessRequest) -> dict[str, Any]:
    """Classify the SOS, then build a calm, ordered safety plan for that disaster type."""
    # 1. Location: GPS wins; otherwise geocode the place name.
    if body.latitude is not None and body.longitude is not None:
        lat, lon = body.latitude, body.longitude
        location = {"latitude": lat, "longitude": lon, "source": "gps", "resolved_name": None}
    elif body.place_name and body.place_name.strip():
        geo = await geocode_place(body.place_name.strip())
        if not geo.get("ok"):
            raise HTTPException(status_code=422, detail=geo.get("error", "Could not find that place."))
        top = geo["results"][0]
        lat, lon = top["latitude"], top["longitude"]
        location = {"latitude": lat, "longitude": lon, "source": "geocoded",
                    "resolved_name": top["name"]}
    else:
        location = None

    # 2. Classification. If the RAG stack (DB / Ollama) is down, fall back to keyword rules
    #    so the person still gets a plan. The fallback is flagged in the response.
    fallback_reason: str | None = None
    try:
        classification = await classify_sos(body.text, body.top_k)
    except Exception as exc:  # RagError, connection errors, model errors
        logger.warning("classify_sos failed, using keyword rules: %s", exc)
        fallback_reason = f"{type(exc).__name__}: {exc}"
        classification = classify_incident(body.text)

    # 3. Pick the disaster type for the safety plan. "unknown" never means safe.
    if body.disaster_type:
        dtype = body.disaster_type.lower()
        if dtype not in DISASTER_PROFILES:
            raise HTTPException(
                status_code=422,
                detail=f"disaster_type must be one of {sorted(DISASTER_PROFILES)}",
            )
    else:
        dtype = classification.incident_type if classification.incident_type in DISASTER_PROFILES else "general"

    # 4. Safety plan from the MCP tools.
    plan = None
    if location is not None:
        plan = await calm_safety_plan(lat, lon, dtype, body.country_code)
        _tool_or_http_error(plan)

    # 5. One ordered list for the UI: breathing cue + call prompt, the classifier's headline,
    #    then the place to go and the disaster-specific steps.
    guidance = classification.emergency_guidance
    if plan is not None:
        steps = list(plan["steps"])
        if guidance and guidance.headline:
            steps.insert(min(2, len(steps)), guidance.headline)
    else:
        # A text-only assessment can provide local guidance without inventing
        # coordinates or calling nearby-resource services for the wrong place.
        steps = ([guidance.headline, *guidance.do_now, *guidance.next_steps]
                 if guidance else [])

    return {
        "location": location,
        "classification": classification.model_dump(),
        "classification_fallback": fallback_reason is not None,
        "classification_fallback_reason": fallback_reason,
        "disaster_type_used": dtype,
        "alert": classification.alert,
        "calm_steps": steps,
        "safety_plan": plan,
        "location_required": location is None,
        "location_prompt": (
            "For nearby resources, include a line like 'Location: your town or coordinates' in your incident message."
            if location is None else None
        ),
    }


@router.post("/places")
async def places(body: PlacesRequest) -> dict[str, Any]:
    result = await find_safe_locations(
        body.latitude, body.longitude, body.disaster_type, body.radius_m, body.limit
    )
    return _tool_or_http_error(result)


@router.post("/hazards")
async def hazards(body: HazardsRequest) -> dict[str, Any]:
    return _tool_or_http_error(
        await check_nearby_hazards(body.latitude, body.longitude, body.radius_km)
    )


@router.post("/geocode")
async def geocode(body: GeocodeRequest) -> dict[str, Any]:
    return _tool_or_http_error(await geocode_place(body.query), status=404)


@router.get("/emergency-numbers/{country_code}")
async def emergency_numbers(country_code: str) -> dict[str, Any]:
    return _tool_or_http_error(get_emergency_numbers(country_code), status=404)
