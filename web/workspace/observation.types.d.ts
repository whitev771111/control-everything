// Browser consumption views; native identity and authorization remain in the backend.
declare namespace WorkspaceObservationTypes {
  type ProjectLabel = { id: string; name: string };
  type ConfigPair = { config_revision: number | null; config_hash: string | null };
  type Coverage = { status: string };
  type RegistrationValidation = { registration_id: string; status: string; reason?: string | null };
  type ObservedRegistration = RegistrationValidation & { label?: string | null; category?: string | null; can_pick?: boolean | null };
  type Installation = { provider_id: string; status: string; version?: string | null };
  type Resource = { resource_id: string; rss_bytes: number | null; cpu_percent_one_core: number | null;
    threads: number | null; handles: number | null; metrics_interval_ms: number | null };
  type Grant = { action_id: string; issued_seq: number; expires_at_ms: number; scope: string };
  type NoAction = { action_id: null; status: string; reason: string | null };
  type Instance = { instance_id: string; kind: string; status: string; resource_id: string; pid: number;
    provider_id: string | null; project_id: string | null; classification?: string; registration_labels?: string[];
    return_action: Grant | NoAction | null };
  type Snapshot = ConfigPair & { status: string; reason: string | null; generation: string | null; sampled_at_ms: number | null;
    coverage: Coverage; installations: Installation[]; resources: Resource[]; instances: Instance[];
    project_refs: { project_id: string; instance_ids: string[] }[]; unassigned: string[];
    registrations?: ObservedRegistration[] | null };
  type RegistrationFields = { environment_id: 'windows:local'; label: string; path: string; enabled: boolean;
    binding_state: 'confirmed' | 'rebind_required' };
  type RegistrationLocation = { category: 'builtin'; provider_id: string; locator_kind: string }
    | { category: 'custom_exe'; provider_id: null; locator_kind: 'executable' };
  // Draft builtin select values are validated by the existing authoritative save API.
  type RegistrationWrite = RegistrationFields & RegistrationLocation & { id: string | null };
  type Registration = RegistrationFields & RegistrationLocation & { id: string };
  type EditorRow = { id?: string; category: 'builtin' | 'custom_exe'; provider_id: string | null;
    locator_kind: string; label: string; path: string; enabled: boolean };
  type Settings = { revision: number; hash: string; registrations: Registration[];
    validation?: RegistrationValidation[] | null; application?: { reason?: string | null } | null };
  type Candidate = { provider_id: string; locator_kind: string; path: string; version?: string | null };
  type Candidates = { candidates?: Candidate[] };
  type PickerSelection = ConfigPair & { selection_id: string; generation: string };
  type Choices = PickerSelection & { expires_at_ms: number; coverage: Coverage;
    choices: { title: string; action_id: string; issued_seq: number; expires_at_ms: number }[] };
  type Receipt = { status: string; kind?: string; reason?: string | null };
  type ReturnBody = { action_id: string; generation: string; seq: number };
  type DismissBody = { selection_id: string; generation: string };
  type PickBody = { registration_id: string };
  type SaveBody = { expected_revision: number; expected_hash: string; registrations: RegistrationWrite[] };
  type PostBody = ReturnBody | DismissBody | PickBody | SaveBody;
  interface Options {
    fetchJson(path: string): Promise<unknown>;
    postJson(path: string, body: PostBody): Promise<unknown>;
  }
  interface PublicApi {
    projects: readonly ProjectLabel[];
    start(options: Options): void;
    renderProjectRefs(): void;
  }
}
interface Window { WorkspaceObservation: WorkspaceObservationTypes.PublicApi; }
