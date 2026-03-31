"""Deals page route — redirects to Products page (deals mode is merged there)."""
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/deals")
async def deals_redirect():
    return RedirectResponse(url="/products", status_code=302)
