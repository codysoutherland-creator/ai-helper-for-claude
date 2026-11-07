-- Before this fix, approving a stage of a master plan marked the master's own later stages "replaced" (they share
-- ground by design). Undo those marks once; a plan replaced by another master's stage stays replaced.
if storage.plans then
  local function master_of(p) return p and (p.master or (p.future and p.future.master)) end
  for _, q in pairs(storage.plans) do
    local by = q.superseded and storage.plans[q.superseded]
    if by and master_of(q) and master_of(q) == master_of(by) then q.superseded = nil end
  end
end
