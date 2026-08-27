# NEU-1203 — Don't repeat movie posters in daily feed on the same day

## Problem

Since NEU-1199 split the grouped feed into two sections ("In the News" and "via TMDB"), a film-day can now appear in **both** sections with different event subsets. Both `FeedDayItem` entries carry the same `poster_path`. The frontend's `dayPosterLeads()` utility filters to items with posters, sorts news-first, and caps at 8, but **does not deduplicate by `film_ref`**. Result: the same poster appears twice in the horizontal poster strip above a day's sections.

## Scope

- **Frontend only.** No backend change needed — the backend correctly emits two `FeedDayItem` entries with the same `film_ref`; that's intentional per NEU-1199.
- Single file change: `src/lib/feed-groups.ts` (`dayPosterLeads`).
- The dedup should keep the **first** occurrence (which, after `splitByNewsBacked`, is the news-backed entry — higher signal value).

## Acceptance criteria

- A film-day present in both "In the News" and "via TMDB" sections shows its poster **once** in the poster strip.
- News-backed posters take priority when a film has entries in both sections.
- A film-day present only in one section is unaffected.
- The `MAX_DAY_POSTERS = 8` cap still applies after dedup.
- Films with `poster_path = null` are still excluded.
- Within-section alphabetical ordering is preserved for all films not removed by dedup.

## Implementation

### `dayPosterLeads(items, limit?)` in `feed-groups.ts`

Current flow:
1. Filter to items with `poster_path`
2. Partition into news-backed and TMDB-only via `splitByNewsBacked`
3. Concatenate (news-first), slice to limit

Add a dedup step after concatenation: **track seen `film_ref` values, keep only the first occurrence of each**. The news-first sort guarantees the news-backed entry survives when a film appears in both buckets.

Alternatively, dedup inline during the filter/build to avoid allocating an extra pass — but the simplest correct approach is:

```ts
export function dayPosterLeads(items: FeedDayItem[], limit = MAX_DAY_POSTERS): FeedDayItem[] {
  const withPosters = items.filter((i) => i.poster_path);
  const { newsBacked, tmdbOnly } = splitByNewsBacked(withPosters);
  const seen = new Set<string>();
  return [...newsBacked, ...tmdbOnly]
    .filter((item) => {
      if (seen.has(item.film_ref)) return false;
      seen.add(item.film_ref);
      return true;
    })
    .slice(0, limit);
}
```

### Tests

#### Modified

| Test | Current expectation | After |
|------|-------------------|-------|
| `dayPosterLeads handles dedup` (if exists) | — | — |

#### New

- `test_day_posters_dedup_same_film_in_both_sections` — 1 film appears in both news-backed and TMDB-only → 1 poster in output.
- `test_day_posters_dedup_preserves_news_first` — same film in both sections, news-backed entry is the one kept.
- `test_day_posters_dedup_does_not_affect_distinct_films` — 8 distinct films → 8 posters (unchanged).
- `test_day_posters_dedup_respects_cap` — 9 films after dedup → capped at 8.
