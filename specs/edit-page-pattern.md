# Spec — `/edit` URL pattern for inline-editable pages

## Why
The recipe detail page lived in one Jinja template with a `data-recipe-mode` attribute toggling between read and edit. CSS rules scoped to one mode kept leaking into the other and breaking the page layout. Four consecutive commits chased the same bug class. Splitting view and edit into separate URLs eliminated the toggle entirely.

This spec captures the convention for any future page with non-trivial inline editing.

## Convention

### URLs
- `GET /{resource}/{id}` → read-only view.
- `GET /{resource}/{id}/edit` → edit mode.
- Saves go through existing API endpoints (e.g. `PUT /api/{resource}/{id}` and per-field endpoints). No form POST on the page route.

### Routes
Both handlers call one private helper (e.g. `_build_recipe_context`) that loads + shapes the entity and returns a context dict. View and edit are thin wrappers that pick a different template:

```python
@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
async def recipe_detail(request, recipe_id):
    context = _build_recipe_context(request, recipe_id)
    return templates.TemplateResponse(request, "recipe_view.html", context)


@router.get("/recipes/{recipe_id}/edit", response_class=HTMLResponse)
async def recipe_edit(request, recipe_id):
    context = _build_recipe_context(request, recipe_id)
    return templates.TemplateResponse(request, "recipe_edit.html", context)
```

### Templates
- `templates/{resource}_view.html` — read-only. No mode toggle. No edit chrome.
- `templates/{resource}_edit.html` — editing always-on. No mode toggle.
- Each template hardcodes its Alpine mode store (`editing: false` / `editing: true`) so any shared edit-only affordance still resolves cleanly.
- Top-right of view: a single `Edit recipe` link `<a href="/{resource}/{id}/edit">`.
- Top-right of edit: a single `Done` link `<a href="/{resource}/{id}">`.

### Saves
- Saves happen on field blur through the existing per-field API endpoints. The edit page does not POST a whole form.
- `Done` is a plain navigation back. The user can also close the tab or back-button — already-saved edits persist either way.

### Cache control
`Cache-Control: no-store` is stamped on every `text/html` response (see `app.py` middleware). Without it, browsers happily serve stale HTML after deploys.

## When to apply
Use this pattern for any page where:
- Editing involves multiple fields, drag-reorder, autocomplete, or any layout-affecting chrome.
- The view experience is meaningfully different from the edit experience (typography, density, visible affordances).

Skip this pattern when editing is a single inline numeric bump or boolean toggle — the per-row local `x-data={editing: false}` pattern used by `pantry.html` works fine for those.

## Reference implementation
- Routes: `src/kroger_mcp/web/routes/recipes.py` — `recipe_detail`, `recipe_edit`, `_build_recipe_context`.
- Templates: `src/kroger_mcp/web/templates/recipe_view.html`, `src/kroger_mcp/web/templates/recipe_edit.html`.
- Cache middleware: `src/kroger_mcp/web/app.py` — `_no_store_for_html`.
