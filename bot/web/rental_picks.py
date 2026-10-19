"""Anonymous rental selection reads only the validated published artifact."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse


def _published_selection():
    from bot.core.rental_picks import load_rental_picks, prepare_rental_picks

    return prepare_rental_picks(load_rental_picks())


def make_rental_picks_router(templates):
    router = APIRouter()

    @router.get("/best-rentals", response_class=HTMLResponse)
    async def best_rentals(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="rental_picks.html",
            context={"request": request, "snapshot": _published_selection()},
            headers={"Cache-Control": "no-cache", "Referrer-Policy": "same-origin"},
        )

    return router
