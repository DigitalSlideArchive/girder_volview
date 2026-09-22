import * as fs from 'fs';
import * as path from 'path';

// State shared by the harness's Playwright invocations: baseline capture,
// current verification, and current-only behavior. Capture writes it and the
// orchestrator's final phase deletes it unless COMPAT_KEEP=1.

export type RulerRecord = {
  // The rendered measurement, e.g. "40.00mm" — world coordinates live in the
  // session zip, so a faithful restore reproduces this text exactly.
  lengthText: string;
};

// Semantic summary of a session zip's manifest.json — migration-tolerant
// (counts and presence, not raw JSON equality).
export type ZipSummary = {
  rulerCount: number;
  // One mask carries one segment's voxels on one image. A 6.x segment group
  // holding N segment descriptors is N masks once it is read.
  maskCount: number;
  // Size of the largest labelmap archive entry: painted voxels make it
  // decidedly non-trivial, an empty labelmap does not.
  maskDataBytes: number;
  // Segment names, which is the identity that survives both generations: 6.x
  // group names do not exist in 7.0.0.
  segmentNames: string[];
  hasLayers: boolean;
  version?: string;
};

export type GestureId =
  | 'checked-nrrd'
  | 'filtered-dicom'
  | 'study-layered'
  | 'single-item'
  | 'study-drilldown';

export type FixtureId =
  | GestureId
  | 'lifecycle-single'
  | 'lifecycle-checked'
  | 'lifecycle-filter'
  | 'lifecycle-restart'
  | 'lifecycle-bare'
  | 'lifecycle-older'
  | 'lifecycle-session-row'
  | 'lifecycle-url-contract'
  | 'jobs-baseline'
  | 'jobs-comeback'
  | 'jobs-live'
  | 'jobs-staged'
  | 'jobs-failure'
  | 'jobs-annotations'
  | 'jobs-roi-rulers'
  | 'jobs-annotations-blocked'
  | 'jobs-overlap';

export type FixtureFolder = {
  folderId: string;
  itemIds: string[];
  itemNames: string[];
};

export type LaunchDescriptor =
  | { via: 'checked-items'; itemIds: string[] }
  // Grouped rows matched by the conjunction of cell texts, optionally after
  // narrowing with the filter box.
  | { via: 'checked-rows'; rows: string[][]; filterText?: string }
  | { via: 'item-page'; itemId: string }
  | { via: 'row-nav'; rowTexts: string[] };

export type CapturedGesture = {
  id: GestureId;
  folderId: string;
  launch: LaunchDescriptor;
  // Folder-scoped saves mint a session item (absent for single-item saves,
  // where the zip lands inside the launched item).
  sessionItemId?: string;
  sessionItemName?: string;
  expected: {
    datasetNames: string[];
    rulers: RulerRecord[];
    segmentNames: string[];
    petLayer: boolean;
    zip: ZipSummary;
  };
};

export type CapturedJob = {
  jobId: string;
  segmentNames: string[];
};

export type CompatState = {
  createdAt: string;
  sourceGirderSha: string;
  sourceVolviewSha: string;
  runRootFolderId: string;
  // Every scenario owns its folder. Saves and checkbox state from one test can
  // therefore never alter another test's launch semantics.
  fixtures: Record<FixtureId, FixtureFolder>;
  token: string;
  provisioned: boolean;
  dicomSeeded: boolean;
  gestures: CapturedGesture[];
  // The labelmap job the baseline client submitted in the 'jobs-baseline'
  // fixture, with the segment names its result applied. The verify phase loads
  // the same result through the branch.
  baselineJob?: CapturedJob;
};

export const COMPAT_STATE_PATH = path.resolve(__dirname, '..', '.compat-state.json');

export function writeCompatState(state: CompatState): void {
  fs.writeFileSync(COMPAT_STATE_PATH, JSON.stringify(state, null, 2), 'utf8');
}

export function readCompatState(): CompatState | undefined {
  try {
    return JSON.parse(fs.readFileSync(COMPAT_STATE_PATH, 'utf8')) as CompatState;
  } catch {
    return undefined;
  }
}

export function clearCompatState(): void {
  try {
    fs.unlinkSync(COMPAT_STATE_PATH);
  } catch {
    /* already gone */
  }
}

export function requireFixture(state: CompatState, id: FixtureId): FixtureFolder {
  const fixture = state.fixtures[id];
  if (!fixture) throw new Error(`[compat] fixture '${id}' was not provisioned`);
  return fixture;
}

// Capture specs append gestures one test at a time; the harness uses one worker,
// so read-modify-write keeps the file the source of truth across the phase.
export function appendGesture(gesture: CapturedGesture): void {
  const state = readCompatState();
  if (!state) throw new Error('[compat] no state file — did capture setup run?');
  state.gestures = [...state.gestures.filter((g) => g.id !== gesture.id), gesture];
  writeCompatState(state);
}

export function recordBaselineJob(job: CapturedJob): void {
  const state = readCompatState();
  if (!state) throw new Error('[compat] no state file. Did capture setup run?');
  writeCompatState({ ...state, baselineJob: job });
}
