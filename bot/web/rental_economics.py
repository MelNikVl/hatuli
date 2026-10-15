"""One existing-access-policy endpoint for a ready asking-rent scenario."""
import re

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from bot.core.site_auth import get_user_tier

router = APIRouter()
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@router.get("/api/listing/{listing_id}/rental-economics")
async def listing_rental_economics(request: Request, listing_id: str):
    if await get_user_tier(request) not in ("admin", "subscriber"):
        return JSONResponse({"error": "access_required"}, status_code=403, headers=HEADERS)
    if not re.fullmatch(r"\d{5,20}", listing_id):
        return JSONResponse({"error": "not_found"}, status_code=404, headers=HEADERS)
    from bot.db.pg import fetchrow
    from bot.core.rental_economics import rental_economics_for_listing
    row = await fetchrow("SELECT * FROM apartment_listings WHERE id=$1", listing_id)
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404, headers=HEADERS)
    result = await rental_economics_for_listing(dict(row))
    if result is None:
        return Response(status_code=204, headers=HEADERS)
    result.pop("listing", None)
    return JSONResponse(jsonable_encoder(result), headers=HEADERS)
