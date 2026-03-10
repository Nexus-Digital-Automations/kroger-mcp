"""Cart page route."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from kroger_mcp.tools.cart_tools import _load_cart_data, _load_order_history

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request):
    cart_data = _load_cart_data()
    history = _load_order_history()
    current_cart = cart_data.get("current_cart", [])
    return templates.TemplateResponse("cart.html", {
        "request": request,
        "active_page": "cart",
        "current_cart": current_cart,
        "cart_count": len(current_cart),
        "order_history": history[-10:] if history else [],
    })
