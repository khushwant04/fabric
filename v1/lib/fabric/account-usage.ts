import type { AccountUsage } from "./types"

export function summarizeAccountUsage(rows: Iterable<AccountUsage>): AccountUsage {
  const total: AccountUsage = {
    events: 0,
    input_tokens: 0,
    output_tokens: 0,
    first_occurred_at: null,
    last_occurred_at: null,
  }
  for (const row of rows) {
    total.events += row.events
    total.input_tokens += row.input_tokens
    total.output_tokens += row.output_tokens
    if (row.first_occurred_at &&
        (!total.first_occurred_at || Date.parse(row.first_occurred_at) < Date.parse(total.first_occurred_at))) {
      total.first_occurred_at = row.first_occurred_at
    }
    if (row.last_occurred_at &&
        (!total.last_occurred_at || Date.parse(row.last_occurred_at) > Date.parse(total.last_occurred_at))) {
      total.last_occurred_at = row.last_occurred_at
    }
  }
  return total
}
