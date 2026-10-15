import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { basename, dirname, join, relative, resolve } from 'node:path';

type Node = {
  id: string;
  kind: string;
  label: string;
  x: number;
  y: number;
  config: Record<string, unknown>;
};

type Edge = {
  id: string;
  from: string;
  to: string;
  weight: number;
  control?: boolean;
  [key: string]: unknown;
};

type Topology = {
  nodes: Node[];
  edges: Edge[];
  [key: string]: unknown;
};

type Design = {
  app: string;
  version: number;
  savedAt: string;
  name?: string;
  topology: Topology;
};

type ComponentIds = {
  client: string;
  gateway: string;
  uploadWorker: string;
  index: string;
  chunkStore: string;
};

type RunRow = {
  run_id: string;
  experiment: string;
  design_file: string;
  seed: number;
  cpu_ms_per_mib: number;
  write_share: number;
  offered_load: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  goodput: number;
  failed: number;
  notes: string;
};

export const RUN_COLUMNS = [
  'run_id', 'experiment', 'design_file', 'seed', 'cpu_ms_per_mib',
  'write_share', 'offered_load', 'p50_ms', 'p95_ms', 'p99_ms', 'goodput',
  'failed', 'notes',
] as const;

const SEEDS = [1, 2, 3] as const;
const INDEX_SERVICE_MS = [2, 10, 50] as const;
const RESTORE_SHARES = [0.1, 0.25, 0.5] as const;
const RETRY_TIMEOUT_MS = 250;
const RETRIES = 2;
const BREAKER_CONFIG = {
  errorThreshold: 0.5,
  windowMs: 4000,
  openMs: 3000,
  halfOpenProbes: 3,
};

function clone<T>(value: T): T {
  return structuredClone(value);
}

function nodeById(topology: Topology, id: string, role: string): Node {
  const node = topology.nodes.find((candidate) => candidate.id === id);
  if (!node) throw new Error(`E1 design has no ${role} node with id ${JSON.stringify(id)}`);
  return node;
}

function assertEdge(topology: Topology, from: string, to: string, role: string): Edge {
  const matches = topology.edges.filter(
    (edge) => !edge.control && edge.from === from && edge.to === to,
  );
  if (matches.length !== 1) {
    throw new Error(`E1 design must have exactly one ${role} edge ${from} -> ${to}`);
  }
  return matches[0];
}

function assertSingleGatewayRoute(topology: Topology, ids: ComponentIds): void {
  assertEdge(topology, ids.client, ids.gateway, 'client-to-gateway');
  const outgoing = topology.edges.filter((edge) => !edge.control && edge.from === ids.gateway);
  if (outgoing.length !== 1 || outgoing[0].to !== ids.uploadWorker) {
    throw new Error('E1 baseline must have one gateway route, directed to the upload worker');
  }
}

function uniqueId(topology: Topology, base: string): string {
  const ids = new Set(topology.nodes.map((node) => node.id));
  if (!ids.has(base)) return base;
  let suffix = 2;
  while (ids.has(`${base}-${suffix}`)) suffix++;
  return `${base}-${suffix}`;
}

function uniqueEdgeId(topology: Topology, base: string): string {
  const ids = new Set(topology.edges.map((edge) => edge.id));
  if (!ids.has(base)) return base;
  let suffix = 2;
  while (ids.has(`${base}-${suffix}`)) suffix++;
  return `${base}-${suffix}`;
}

export function buildE3Topology(
  baseline: Topology,
  ids: ComponentIds,
  indexServiceMs: number,
  withBreaker: boolean,
  breakerDefaults: Record<string, unknown>,
): Topology {
  if (!INDEX_SERVICE_MS.includes(indexServiceMs as (typeof INDEX_SERVICE_MS)[number])) {
    throw new Error(`unsupported E3 index service time: ${indexServiceMs} ms`);
  }
  const topology = clone(baseline);
  const client = nodeById(topology, ids.client, 'client');
  const gateway = nodeById(topology, ids.gateway, 'gateway');
  const worker = nodeById(topology, ids.uploadWorker, 'upload worker');
  const index = nodeById(topology, ids.index, 'index');
  if (client.kind !== 'client' || gateway.kind !== 'lb') {
    throw new Error('E3 requires a client and a Load Balancer gateway');
  }
  assertSingleGatewayRoute(topology, ids);
  assertEdge(topology, ids.gateway, ids.uploadWorker, 'gateway-to-upload-worker');
  const workerIndexEdge = assertEdge(
    topology, ids.uploadWorker, ids.index, 'upload-worker-to-index',
  );

  gateway.config.timeoutMs = RETRY_TIMEOUT_MS;
  gateway.config.retries = RETRIES;
  index.config.serviceMs = indexServiceMs;

  if (withBreaker) {
    const breakerId = uniqueId(topology, 'chunkwise-index-breaker');
    const breakerNode: Node = {
      id: breakerId,
      kind: 'breaker',
      label: 'Index Circuit Breaker',
      x: (worker.x + index.x) / 2,
      y: (worker.y + index.y) / 2,
      config: {
        ...clone(breakerDefaults),
        ...BREAKER_CONFIG,
      },
    };
    topology.nodes.push(breakerNode);
    topology.edges = topology.edges.filter((edge) => edge.id !== workerIndexEdge.id);
    topology.edges.push(
      {
        ...workerIndexEdge,
        id: uniqueEdgeId(topology, 'chunkwise-worker-breaker-edge'),
        to: breakerId,
      },
      {
        id: uniqueEdgeId(topology, 'chunkwise-breaker-index-edge'),
        from: breakerId,
        to: ids.index,
        weight: 1,
      },
    );
  }
  return topology;
}

export function buildE4Topology(
  baseline: Topology,
  ids: ComponentIds,
  uploadRps: number,
  restoreShare: number,
  requestSizeMiB: number,
  restoreCpuMsPerMiB: number,
  serviceDefaults: Record<string, unknown>,
): Topology {
  if (!RESTORE_SHARES.includes(restoreShare as (typeof RESTORE_SHARES)[number])) {
    throw new Error(`unsupported E4 restore share: ${restoreShare}`);
  }
  if (!(uploadRps > 0) || !(requestSizeMiB > 0) || !(restoreCpuMsPerMiB > 0)) {
    throw new Error('E4 upload load, request size, and measured restore CPU must be positive');
  }
  const topology = clone(baseline);
  const client = nodeById(topology, ids.client, 'client');
  const gateway = nodeById(topology, ids.gateway, 'gateway');
  nodeById(topology, ids.uploadWorker, 'upload worker');
  nodeById(topology, ids.index, 'index');
  nodeById(topology, ids.chunkStore, 'chunk store');
  if (client.kind !== 'client' || gateway.kind !== 'lb') {
    throw new Error('E4 requires a client and a Load Balancer gateway');
  }
  assertSingleGatewayRoute(topology, ids);
  const uploadEdge = assertEdge(topology, ids.gateway, ids.uploadWorker, 'gateway-to-upload-worker');
  assertEdge(topology, ids.uploadWorker, ids.index, 'upload-worker-to-index');
  assertEdge(topology, ids.uploadWorker, ids.chunkStore, 'upload-worker-to-chunk-store');

  const restoreId = uniqueId(topology, 'chunkwise-restore-worker');
  const totalRps = uploadRps * (1 + restoreShare);
  client.config.rps = totalRps;
  uploadEdge.weight = 1 / (1 + restoreShare);
  const restoreWorker: Node = {
    id: restoreId,
    kind: 'service',
    label: 'Restore Worker',
      x: gateway.x + 180,
    y: nodeById(topology, ids.uploadWorker, 'upload worker').y + 130,
    config: {
      ...clone(serviceDefaults),
      capacity: 4,
      instances: 1,
      serviceMs: requestSizeMiB * restoreCpuMsPerMiB,
      serviceCv: 0.5,
    },
  };
  topology.nodes.push(restoreWorker);
  topology.edges.push(
    {
      id: uniqueEdgeId(topology, 'chunkwise-gateway-restore-edge'),
      from: ids.gateway,
      to: restoreId,
      weight: restoreShare / (1 + restoreShare),
    },
    {
      id: uniqueEdgeId(topology, 'chunkwise-restore-index-edge'),
      from: restoreId,
      to: ids.index,
      weight: 1,
    },
    {
      id: uniqueEdgeId(topology, 'chunkwise-restore-store-edge'),
      from: restoreId,
      to: ids.chunkStore,
      weight: 1,
    },
  );
  return topology;
}

export function csvCell(value: string | number): string {
  const text = String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function serializeRows(rows: RunRow[]): string {
  return rows.map((row) => RUN_COLUMNS.map((column) => csvCell(row[column])).join(',')).join('\n');
}

function parseArgs(argv: string[]): Record<string, string> {
  const values: Record<string, string> = {};
  for (let index = 0; index < argv.length; index++) {
    const key = argv[index];
    if (!key.startsWith('--') || index + 1 >= argv.length) {
      throw new Error(`expected --option value, got ${JSON.stringify(key)}`);
    }
    values[key.slice(2)] = argv[++index];
  }
  const required = [
    'breakscale-root', 'e1-knee-rps', 'request-size-mib', 'client-id',
    'gateway-id', 'upload-worker-id', 'index-id', 'chunk-store-id',
  ];
  const missing = required.filter((key) => !values[key]);
  if (missing.length) throw new Error(`missing required options: ${missing.map((x) => `--${x}`).join(', ')}`);
  return values;
}

export function parseCsv(text: string): string[][] {
  const lines = text.trimEnd().split(/\r?\n/);
  return lines.map((line) => {
    const fields: string[] = [];
    let field = '';
    let quoted = false;
    for (let index = 0; index < line.length; index++) {
      const char = line[index];
      if (quoted && char === '"' && line[index + 1] === '"') {
        field += '"';
        index++;
      } else if (char === '"') {
        quoted = !quoted;
      } else if (char === ',' && !quoted) {
        fields.push(field);
        field = '';
      } else {
        field += char;
      }
    }
    if (quoted) throw new Error('invalid CSV: unterminated quoted field');
    fields.push(field);
    return fields;
  });
}

async function requiredFile(path: string, label: string): Promise<string> {
  try {
    return await readFile(path, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      throw new Error(`${label} is missing: ${path}`);
    }
    throw error;
  }
}

async function readInputs(options: Record<string, string>) {
  const designPath = resolve(options.design ?? 'breakscale-designs/e1-upload-baseline.breakscale');
  const paramsPath = resolve(options.params ?? 'results/breakscale_params.json');
  const e1RunsPath = resolve(options['e1-runs'] ?? 'breakscale-designs/runs.csv');
  const [designText, paramsText, e1RunsText] = await Promise.all([
    requiredFile(designPath, 'Owner A E1 Breakscale design'),
    requiredFile(paramsPath, 'measured calibration file'),
    requiredFile(e1RunsPath, 'Owner A E1 sweep results'),
  ]);

  const design = JSON.parse(designText) as Design;
  if (design.app !== 'breakscale' || !design.topology?.nodes || !design.topology?.edges) {
    throw new Error('E1 design is not a native Breakscale design file with a topology');
  }
  const params = JSON.parse(paramsText) as Record<string, unknown>;
  const putCpu = Number(params.cpu_service_ms_per_mb_put);
  const getCpu = Number(params.cpu_service_ms_per_mb_get);
  const writeShare = Number(params.store_write_fraction_edited_text);
  const calibrationSource = typeof params.source === 'string' ? resolve(params.source) : '';
  if (![putCpu, getCpu, writeShare].every(Number.isFinite) || putCpu <= 0 || getCpu <= 0 || writeShare < 0) {
    throw new Error('calibration file lacks valid measured put/get CPU and edited-text write-share fields');
  }
  if (!calibrationSource) throw new Error('calibration file must identify its generated results.json source');
  const calibrationResult = JSON.parse(
    await requiredFile(calibrationSource, 'calibration source results.json'),
  ) as { rows?: unknown };
  if (!Array.isArray(calibrationResult.rows)) {
    throw new Error('calibration source is not a generated benchmark results.json file');
  }

  const records = parseCsv(e1RunsText);
  const header = records.shift();
  if (!header || !RUN_COLUMNS.every((column) => header.includes(column))) {
    throw new Error('E1 runs CSV does not have the Breakscale guide columns');
  }
  const columnIndex = new Map(header.map((column, index) => [column, index]));
  const kneeRps = Number(options['e1-knee-rps']);
  if (!Number.isFinite(kneeRps) || kneeRps <= 0) throw new Error('--e1-knee-rps must be positive');
  const e1Names = new Set(['E1', 'e1', 'e1-upload-baseline']);
  const matchingRows = records
    .filter((record) => e1Names.has(record[columnIndex.get('experiment')!]))
    .filter((record) => Math.abs(Number(record[columnIndex.get('offered_load')!]) - kneeRps) < 1e-9)
    .filter((record) => Math.abs(Number(record[columnIndex.get('cpu_ms_per_mib')!]) - putCpu) <= Math.max(0.01, putCpu * 0.01))
    .filter((record) => Math.abs(Number(record[columnIndex.get('write_share')!]) - 1) < 1e-9)
    .filter((record) => basename(record[columnIndex.get('design_file')!]) === basename(designPath));
  const matchingSeeds = new Set(matchingRows.map((record) => Number(record[columnIndex.get('seed')!])));
  if (![...SEEDS].every((seed) => matchingSeeds.has(seed))) {
    throw new Error(`E1 runs CSV must contain the selected knee ${kneeRps} rps for seeds 1, 2, and 3`);
  }

  const requestSizeMiB = Number(options['request-size-mib']);
  if (!Number.isFinite(requestSizeMiB) || requestSizeMiB <= 0) {
    throw new Error('--request-size-mib must be positive and taken from the E1 workload configuration');
  }
  const ids: ComponentIds = {
    client: options['client-id'],
    gateway: options['gateway-id'],
    uploadWorker: options['upload-worker-id'],
    index: options['index-id'],
    chunkStore: options['chunk-store-id'],
  };
  for (const [id, role] of Object.entries({
    [ids.client]: 'client', [ids.gateway]: 'gateway', [ids.uploadWorker]: 'upload worker',
    [ids.index]: 'index', [ids.chunkStore]: 'chunk store',
  })) {
    if (!id) throw new Error(`component ID for ${role} must not be empty`);
  }
  return {
    designPath,
    paramsPath,
    e1RunsPath,
    design,
    params,
    ids,
    kneeRps,
    requestSizeMiB,
    putCpu,
    getCpu,
    editedWriteShare: writeShare,
    calibrationSource,
  };
}

async function saveDesign(
  path: string,
  name: string,
  topology: Topology,
  buildDesignFile: (topology: Topology, name: string) => string,
): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const expected = JSON.parse(buildDesignFile(topology, name)) as Design;
  try {
    const existing = JSON.parse(await readFile(path, 'utf8')) as Design;
    if (JSON.stringify(existing.topology) !== JSON.stringify(expected.topology)) {
      throw new Error(`refusing to overwrite a different generated design at ${path}`);
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    await writeFile(path, JSON.stringify(expected, null, 2) + '\n', { flag: 'wx' });
  }
}

async function simulate(
  topology: Topology,
  seed: number,
  seconds: number,
  Engine: new (topology: Topology, seed: number) => { advance(ms: number): void; snapshot(): any },
) {
  const engine = new Engine(clone(topology), seed);
  const frames = Math.round(seconds * 60);
  for (let frame = 0; frame < frames; frame++) engine.advance(1000 / 60);
  return engine.snapshot();
}

function runRow(
  experiment: string,
  designFile: string,
  seed: number,
  cpuMsPerMiB: number,
  writeShare: number,
  offeredLoad: number,
  p: { p50: number; p95: number; p99: number },
  goodput: number,
  failed: number,
  notes: Record<string, unknown>,
): RunRow {
  return {
    run_id: `${experiment}-${basename(designFile).replaceAll(/[^a-zA-Z0-9]+/g, '-')}-s${seed}`,
    experiment,
    design_file: designFile,
    seed,
    cpu_ms_per_mib: cpuMsPerMiB,
    write_share: writeShare,
    offered_load: offeredLoad,
    p50_ms: p.p50,
    p95_ms: p.p95,
    p99_ms: p.p99,
    goodput,
    failed,
    notes: JSON.stringify(notes),
  };
}

async function appendRunRows(path: string, rows: RunRow[]): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  let existingText = '';
  try {
    existingText = await readFile(path, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  if (existingText) {
    const records = parseCsv(existingText);
    const header = records.shift();
    if (JSON.stringify(header) !== JSON.stringify(RUN_COLUMNS)) {
      throw new Error(`refusing to append: ${path} has an unexpected run schema`);
    }
    const runIndex = RUN_COLUMNS.indexOf('run_id');
    const knownIds = new Set(records.map((record) => record[runIndex]));
    const duplicate = rows.find((row) => knownIds.has(row.run_id));
    if (duplicate) throw new Error(`run ${duplicate.run_id} already exists in ${path}`);
  }
  const prefix = existingText && !existingText.endsWith('\n') ? '\n' : '';
  const header = existingText ? '' : RUN_COLUMNS.join(',') + '\n';
  await appendFile(path, prefix + header + serializeRows(rows) + '\n', { flag: 'a' });
}

async function main(): Promise<void> {
  const options = parseArgs(process.argv.slice(2));
  const inputs = await readInputs(options);
  const breakscaleRoot = resolve(options['breakscale-root']);
  const { Engine } = await import(join(breakscaleRoot, 'src/sim/engine.ts'));
  const { defaultConfig } = await import(join(breakscaleRoot, 'src/sim/presets.ts'));
  const { buildDesignFile } = await import(join(breakscaleRoot, 'src/designFile.ts'));
  const outputDirectory = resolve(options['output-dir'] ?? 'breakscale-designs');
  const metadataPath = resolve(options['metadata-output'] ?? 'breakscale-designs/owner-b-run-metadata.json');
  try {
    await readFile(metadataPath, 'utf8');
    throw new Error(`refusing to append another run set because ${metadataPath} already exists`);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const seconds = Number(options.seconds ?? 10);
  if (!Number.isFinite(seconds) || seconds !== 10) {
    throw new Error('the guide specifies exactly 10 simulated seconds per run');
  }
  const uploadWorker = nodeById(inputs.design.topology, inputs.ids.uploadWorker, 'upload worker');
  const configuredUploadMs = Number(uploadWorker.config.serviceMs);
  const expectedUploadMs = inputs.putCpu * inputs.requestSizeMiB;
  if (
    !Number.isFinite(configuredUploadMs) ||
    Math.abs(configuredUploadMs - expectedUploadMs) > Math.max(0.01, expectedUploadMs * 0.01)
  ) {
    throw new Error(
      `E1 upload worker serviceMs (${configuredUploadMs}) does not match measured CPU ` +
      `(${inputs.putCpu} ms/MiB) times request size (${inputs.requestSizeMiB} MiB); ` +
      'ask Owner A to regenerate or clarify the E1 design inputs',
    );
  }
  const gateway = nodeById(inputs.design.topology, inputs.ids.gateway, 'gateway');
  const index = nodeById(inputs.design.topology, inputs.ids.index, 'index');
  const chunkStore = nodeById(inputs.design.topology, inputs.ids.chunkStore, 'chunk store');
  if (gateway.kind !== 'lb' || Math.abs(Number(gateway.config.serviceMs) - 1) > 1e-9) {
    throw new Error('E1 gateway must be a Load Balancer with the guide assumption of 1 ms service time');
  }
  if (uploadWorker.kind !== 'worker' && uploadWorker.kind !== 'service') {
    throw new Error('E1 upload worker must use Breakscale kind worker or service');
  }
  if (Number(uploadWorker.config.capacity) !== 4 || Math.abs(Number(uploadWorker.config.serviceCv) - 0.5) > 1e-9) {
    throw new Error('E1 upload worker must use the guide assumptions: capacity 4 and service CV 0.5');
  }
  if (index.kind !== 'db' || Math.abs(Number(index.config.serviceMs) - 2) > 1e-9) {
    throw new Error('E1 index must use Breakscale db kind and the guide assumption of 2 ms');
  }
  if (chunkStore.kind !== 'db' && chunkStore.kind !== 'objectstore') {
    throw new Error('E1 chunk store must use Breakscale db or objectstore kind');
  }
  if (Math.abs(Number(chunkStore.config.serviceMs) - (5 + inputs.requestSizeMiB)) > 0.01) {
    throw new Error('E1 chunk-store serviceMs must equal the guide assumption 5 ms + 1 ms/MiB at write share 1.0');
  }

  const rows: RunRow[] = [];
  const e3Load = 0.8 * inputs.kneeRps;
  for (const indexMs of INDEX_SERVICE_MS) {
    for (const withBreaker of [false, true]) {
      const experiment = withBreaker ? 'E3-breaker' : 'E3-retry-storm';
      const name = `e3-${withBreaker ? 'breaker' : 'retry-storm'}-${indexMs}ms`;
      const topology = buildE3Topology(
        inputs.design.topology,
        inputs.ids,
        indexMs,
        withBreaker,
        defaultConfig('breaker'),
      );
      const designFile = `${name}.breakscale`;
      const generatedDesign = join(outputDirectory, designFile);
      const designReference = relative(process.cwd(), generatedDesign);
      await saveDesign(generatedDesign, name, topology, buildDesignFile);
      for (const seed of SEEDS) {
        const snapshot = await simulate(topology, seed, seconds, Engine);
        const system = snapshot.system;
        const indexStats = snapshot.nodes[inputs.ids.index];
        const breakerNode = topology.nodes.find((node) => node.kind === 'breaker');
        const breakerStats = breakerNode ? snapshot.nodes[breakerNode.id] : undefined;
        rows.push(runRow(
          experiment,
          designReference,
          seed,
          inputs.putCpu,
          1.0,
          e3Load,
          { p50: system.p50, p95: system.p95, p99: system.p99 },
          system.goodputRps,
          system.totalFailed,
          {
            index_service_ms_assumption: indexMs,
            index_utilization: indexStats?.utilization,
            index_queue_depth: indexStats?.queued,
            index_arrival_rps_including_retries: indexStats?.arrivalRate,
            breaker_state: breakerStats?.breakerState ?? null,
            breaker_trips: breakerStats?.breakerTrips ?? 0,
            breaker_rejected_rps: breakerStats?.breakerRejectedRate ?? 0,
            retries_per_parent: RETRIES,
            retry_timeout_ms: RETRY_TIMEOUT_MS,
            load_fraction_of_e1_knee: 0.8,
            e1_knee_rps: inputs.kneeRps,
            calibration_file: inputs.paramsPath,
          },
        ));
      }
    }
  }

  const e4UploadLoad = 0.7 * inputs.kneeRps;
  for (const restoreShare of RESTORE_SHARES) {
    const name = `e4-restore-spike-${Math.round(restoreShare * 100)}pct`;
    const topology = buildE4Topology(
      inputs.design.topology,
      inputs.ids,
      e4UploadLoad,
      restoreShare,
      inputs.requestSizeMiB,
      inputs.getCpu,
      defaultConfig('service'),
    );
    const designFile = `${name}.breakscale`;
    const generatedDesign = join(outputDirectory, designFile);
    const designReference = relative(process.cwd(), generatedDesign);
    await saveDesign(generatedDesign, name, topology, buildDesignFile);
    const restoreWorker = topology.nodes.find((node) => node.id.startsWith('chunkwise-restore-worker'))!;
    const gateway = topology.nodes.find((node) => node.id === inputs.ids.gateway)!;
    const indexStatsNeeded = inputs.ids.index;
    const storeStatsNeeded = inputs.ids.chunkStore;
    for (const seed of SEEDS) {
      const snapshot = await simulate(topology, seed, seconds, Engine);
      const system = snapshot.system;
      const uploadStats = snapshot.nodes[inputs.ids.uploadWorker];
      const restoreStats = snapshot.nodes[restoreWorker.id];
      const indexStats = snapshot.nodes[indexStatsNeeded];
      const storeStats = snapshot.nodes[storeStatsNeeded];
      rows.push(runRow(
        'E4-restore-spike',
        designReference,
        seed,
        inputs.putCpu,
        1.0,
        e4UploadLoad * (1 + restoreShare),
        { p50: uploadStats?.p50 ?? system.p50, p95: uploadStats?.p95 ?? system.p95, p99: uploadStats?.p99 ?? system.p99 },
        system.goodputRps,
        system.totalFailed,
        {
          upload_target_rps: e4UploadLoad,
          restore_target_rps: e4UploadLoad * restoreShare,
          restore_share_of_upload: restoreShare,
          upload_worker_p99_ms: uploadStats?.p99,
          restore_worker_p99_ms: restoreStats?.p99,
          upload_worker_goodput_rps: uploadStats?.throughput,
          restore_worker_goodput_rps: restoreStats?.throughput,
          gateway_queue_depth: snapshot.nodes[inputs.ids.gateway]?.queued,
          gateway_id: gateway.id,
          index_utilization: indexStats?.utilization,
          index_queue_depth: indexStats?.queued,
          chunk_store_utilization: storeStats?.utilization,
          chunk_store_queue_depth: storeStats?.queued,
          request_size_mib: inputs.requestSizeMiB,
          restore_cpu_ms_per_mib_measured: inputs.getCpu,
          e1_knee_rps: inputs.kneeRps,
          upload_load_fraction_of_e1_knee: 0.7,
          calibration_file: inputs.paramsPath,
        },
      ));
    }
  }

  const runsPath = resolve(options['runs-output'] ?? 'breakscale-designs/runs.csv');
  await appendRunRows(runsPath, rows);
  const git = await import('node:child_process');
  const commit = git.execFileSync('git', ['-C', breakscaleRoot, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  const metadata = {
    experiments: ['E3-retry-storm', 'E3-breaker', 'E4-restore-spike'],
    generated_at: new Date().toISOString(),
    breakscale_commit: commit,
    e1_design: inputs.designPath,
    e1_runs: inputs.e1RunsPath,
    selected_e1_knee_rps: inputs.kneeRps,
    request_size_mib: inputs.requestSizeMiB,
    calibration_file: inputs.paramsPath,
    measured_inputs: {
      cpu_service_ms_per_mb_put: inputs.putCpu,
      cpu_service_ms_per_mb_get: inputs.getCpu,
      store_write_fraction_edited_text: inputs.editedWriteShare,
    },
    assumptions: {
      seeds: SEEDS,
      simulated_seconds: seconds,
      retry_timeout_ms: RETRY_TIMEOUT_MS,
      retries: RETRIES,
      breaker: BREAKER_CONFIG,
      index_service_ms_sweep: INDEX_SERVICE_MS,
      restore_share_sweep: RESTORE_SHARES,
      gateway_lb_weight_split: 'weighted split over one combined Poisson-style offered stream',
      restore_store_service_time: 'retained from E1 topology; no separate read service-time measurement exists',
    },
    outputs: {
      rows: rows.length,
      csv: runsPath,
      designs: rows.map((row) => row.design_file).filter((path, index, all) => all.indexOf(path) === index),
    },
  };
  await mkdir(dirname(metadataPath), { recursive: true });
  await writeFile(metadataPath, JSON.stringify(metadata, null, 2) + '\n', { flag: 'wx' });
  console.log(JSON.stringify(metadata, null, 2));
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(new URL(import.meta.url).pathname)) {
  main().catch((error: unknown) => {
    console.error(`Breakscale experiment error: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  });
}