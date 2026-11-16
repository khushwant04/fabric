import type { FabricUser, Member, Membership } from "@/lib/fabric/types"
import { shortId } from "@/lib/format"

/** The name to show for an account a person belongs to, from `/v1/me`. */
export function accountLabel(membership: Pick<Membership, "account_id" | "account_name">) {
  return membership.account_name?.trim() || `Account ${shortId(membership.account_id)}`
}

/**
 * Name and secondary line for one row of the members list. The profile is what the
 * member's identity provider reported at their last sign-in; a member added by subject
 * who has not signed in yet may have only an email, or nothing at all.
 */
export function memberIdentity(member: Pick<Member, "user_id" | "display_name" | "email">, currentUser?: Pick<FabricUser, "id" | "display_name" | "email">) {
  const self = currentUser?.id === member.user_id
  const name = member.display_name?.trim() || (self ? currentUser?.display_name?.trim() : "") || ""
  const email = member.email?.trim() || (self ? currentUser?.email?.trim() : "") || ""
  if (name) return { name, detail: email || null, self, pending: false }
  if (email) return { name: email, detail: null, self, pending: false }
  return { name: "Not signed in yet", detail: null, self, pending: true }
}

export function initials(name: string) {
  const words = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean)
  return (words.length > 1 ? words[0][0] + words[words.length - 1][0] : (words[0] ?? "?").slice(0, 2)).toUpperCase()
}
