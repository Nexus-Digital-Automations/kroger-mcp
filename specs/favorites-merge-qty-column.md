# Favorites: merge Ordered + Qty into one editable column

The favorites list detail table has two adjacent columns — a read-only
"Ordered" count and an editable "Qty" stepper. Merge them into a single
"Qty" column whose primary control is the editable stepper, with the
times-ordered shown as a small caption beneath it. The quantity must remain
editable and persist via the existing PATCH endpoint.

## Acceptance Criteria

- [x] The table has a single combined column (no separate "Ordered" header/cell); the "Ordered" `<th>` and its `<td>` are gone from `favorites_detail.html`.
  - verify: absent
- [x] The combined "Qty" column shows the editable stepper plus an "ordered N×" caption beneath it.
  - manual: src/kroger_mcp/web/templates/favorites_detail.html
- [x] Changing the quantity (typing or +/− steppers) persists to the server via PATCH and the value survives a reload.
  - verify: tests
- [x] Existing favorites quantity API tests still pass.
  - verify: tests

## Decisions

- [x] Merge into one column rather than deleting the ordered info — keep "ordered N×" as a caption under the editable stepper.
