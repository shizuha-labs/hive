import { formatDistanceToNow, parseISO } from 'date-fns'

/** Relative "2 minutes ago" from an ISO string; em-dash when null. */
export function relativeTime(iso) {
  if (!iso) return '—'
  try {
    const timestamp = String(iso).trim()
    // Hive runs USE_TZ=False but stores lifecycle clocks as naive UTC. DRF and
    // runtime-fleet can therefore emit `2026-07-15T02:07:38` without a zone;
    // parseISO treats that as browser-local time and paints an artificial
    // UTC-offset-sized idle age. Explicit offsets remain authoritative.
    const utcTimestamp = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp)
      ? timestamp
      : `${timestamp}Z`
    return formatDistanceToNow(parseISO(utcTimestamp), { addSuffix: true })
  } catch {
    return '—'
  }
}

/** Unwrap a list response: DRF pagination, bare arrays, or Hive named envelopes. */
export function unwrapList(data) {
  if (Array.isArray(data)) return data
  if (data && Array.isArray(data.results)) return data.results
  if (data && Array.isArray(data.agents)) return data.agents
  if (data && Array.isArray(data.claws)) return data.claws
  if (data && Array.isArray(data.events)) return data.events
  return []
}

/** Best-effort error message extraction from an axios error. */
export function errorMessage(err, fallback = 'Something went wrong') {
  const d = err?.response?.data
  if (!d) return err?.message || fallback
  if (typeof d === 'string') return d
  if (d.detail) return d.detail
  if (d.error) return d.error
  // DRF field errors → first message
  const first = Object.values(d)[0]
  if (Array.isArray(first)) return first[0]
  if (typeof first === 'string') return first
  return fallback
}

/** Title-case a snake/kebab token for display. */
export function humanize(s) {
  if (!s) return ''
  return String(s)
    .replace(/[_-]/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/**
 * Organization-qualified team label used anywhere a team identity is shown.
 * Team names are only unique inside an organization, so a bare "Accounting"
 * label is ambiguous whenever staff can see more than one org. Legacy payloads
 * without organization metadata keep their existing plain label.
 */
export function teamDisplayLabel(team, fallback = '') {
  const nestedOrg = team?.organization && typeof team.organization === 'object'
    ? team.organization
    : {}
  const orgName = String(
    team?.organization_name
      || team?.org_name
      || team?.orgName
      || team?.tenant_org_name
      || nestedOrg.display_name
      || nestedOrg.name
      || team?.organization_slug
      || team?.org_slug
      || team?.orgSlug
      || team?.tenant_org_slug
      || nestedOrg.slug
      || '',
  ).trim()
  const teamName = String(
    team?.teamDisplay
      || team?.display_name
      || fallback
      || team?.team_name
      || team?.name
      || team?.slug
      || team?.teamKey
      || '',
  ).trim()
  if (!teamName) return ''
  return orgName ? `${orgName} › ${teamName}` : teamName
}
