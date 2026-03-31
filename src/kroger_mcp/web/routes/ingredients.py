"""Ingredient management page — redirects to /safety."""
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/ingredients")
async def ingredients_redirect():
    return RedirectResponse(url="/safety", status_code=301)
