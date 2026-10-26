# One headless-run cost row from a Claude execution file (a JSON array of the
# CLI's messages). Used by action.yml; tested by action.test.py.
#
# The shape is stable, for readers that gather the headless-run artifact
# across repositories (mark-brannan/claude#219): a new field is added, never
# renamed or dropped. Caller fields ($fields) merge after the base keys.
#
# Args: $what, $repo, $pr (number or null), $model ("" = the init message's),
# $fields (object).
([.[] | select(.type == "result")] | last // {}) as $r
| {ts: (now | todate), what: $what, repo: $repo, pr: $pr,
   model: (if $model != "" then $model
           else ([.[] | select(.type == "system" and .subtype == "init") | .model] | first // null) end),
   total_cost_usd: $r.total_cost_usd, usage: $r.usage,
   outcome: (if ($r.is_error // false) and ($r.subtype // "success") == "success" then "error"
             else ($r.subtype // "no result") end)}
+ $fields
