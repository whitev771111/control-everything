declare namespace WorkspaceNewsTypes {
  type Stream = 'ai' | 'hn';
  type Status = 'pending' | 'available' | 'stale' | 'unavailable';
  interface Item {
    id: string; source_id: string; source_name: string; title: string; url: string;
    published_at: string | null; fetched_at: string; published_raw: string | null;
    date_reason: 'missing' | 'invalid' | 'timezone_unknown' | null;
    updated_at: string | null; submitted_at: string | null;
    content_origin: 'publisher_feed' | 'hn_submission'; summary: string | null;
    summary_kind: 'rss_description' | 'atom_summary' | null;
    summary_truncated: boolean; summary_reason: 'missing' | 'empty' | 'summary_too_long' | 'unsupported_type' | null;
    title_truncated: boolean;
  }
  interface Source {
    id: string; name: string; url: string; status: Status; reason: string | null;
    sampled_at: string | null; last_success_at: string | null;
    item_count: number; cache_age_ms: number; fresh_for_ms: number;
    scanned_count: number; valid_count: number; filtered_count: number; duplicate_count: number;
    returned_count: number; truncated: boolean; format: 'rss2' | 'atom1' | 'hn' | null;
  }
  interface News {
    schema: 1; stream: Stream; status: Status; reason: string | null; sampled_at: string | null;
    items: Item[]; sources: Source[];
    refresh: { state: 'idle' | 'pending' | 'blocked'; reason: string | null; retry_after_ms: number };
  }
  interface ReadOptions { signal: AbortSignal }
  interface ReadDependencies { fetchJson(path: string, options?: ReadOptions): Promise<unknown> }
  interface Dependencies extends ReadDependencies { postJson(path: string, body: { stream: Stream }, options?: ReadOptions): Promise<unknown> }
  interface PublicApi {
    startWorkspace(dependencies: Dependencies): void;
    startLegacy(dependencies: ReadDependencies): void;
    onView(view: string): void;
  }
}
interface Window { WorkspaceNews?: WorkspaceNewsTypes.PublicApi }
