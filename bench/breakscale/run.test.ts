import assert from 'node:assert/strict';
import test from 'node:test';
import { join } from 'node:path';

import {
  buildE3Topology,
  buildE4Topology,
  parseCsv,
  RUN_COLUMNS,
  csvCell,
  serializeRows,
} from './run.ts';

const ids = {
  client: 'client',
  gateway: 'gateway',
  uploadWorker: 'upload',
  index: 'index',
  chunkStore: 'store',
};

function baseline() {
  return {
    nodes: [
      { id: 'client', kind: 'client', label: 'Client', x: 0, y: 0, config: { rps: 10 } },
      { id: 'gateway', kind: 'lb', label: 'Gateway', x: 100, y: 0, config: { retries: 0, timeoutMs: 0 } },
      { id: 'upload', kind: 'worker', label: 'Upload Worker', x: 200, y: 0, config: { serviceMs: 10 } },
      { id: 'index', kind: 'db', label: 'Index', x: 300, y: 0, config: { serviceMs: 2 } },
      { id: 'store', kind: 'objectstore', label: 'Chunk Store', x: 300, y: 100, config: { serviceMs: 6 } },
    ],
    edges: [
      { id: 'client-gateway', from: 'client', to: 'gateway', weight: 1 },
      { id: 'gateway-upload', from: 'gateway', to: 'upload', weight: 1 },
      { id: 'upload-index', from: 'upload', to: 'index', weight: 1 },
      { id: 'upload-store', from: 'upload', to: 'store', weight: 1 },
    ],
  };
}

test('E3 retry and breaker topologies preserve baseline and wire breaker before index', () => {
  const source = baseline();
  const defaults = { capacity: 1, serviceMs: 0, errorThreshold: 0 };
  const retry = buildE3Topology(source, ids, 50, false, defaults);
  const breaker = buildE3Topology(source, ids, 50, true, defaults);

  assert.equal(source.nodes.find((node) => node.id === 'gateway')!.config.retries, 0);
  assert.equal(retry.nodes.find((node) => node.id === 'gateway')!.config.retries, 2);
  assert.equal(retry.nodes.find((node) => node.id === 'gateway')!.config.timeoutMs, 250);
  assert.equal(retry.nodes.find((node) => node.id === 'index')!.config.serviceMs, 50);
  assert.equal(retry.nodes.some((node) => node.kind === 'breaker'), false);
  const breakerNode = breaker.nodes.find((node) => node.kind === 'breaker')!;
  assert.equal(breakerNode.config.errorThreshold, 0.5);
  assert.equal(breakerNode.config.windowMs, 4000);
  assert.equal(breaker.edges.some((edge) => edge.from === 'upload' && edge.to === breakerNode.id), true);
  assert.equal(breaker.edges.some((edge) => edge.from === breakerNode.id && edge.to === 'index'), true);
  assert.equal(breaker.edges.some((edge) => edge.from === 'upload' && edge.to === 'store'), true);
});

test('E4 splits the combined offered stream at the shared gateway and shares index/store', () => {
  const topology = buildE4Topology(baseline(), ids, 7, 0.25, 16, 5, {
    capacity: 8,
    serviceMs: 25,
    serviceCv: 0.6,
  });
  const client = topology.nodes.find((node) => node.id === 'client')!;
  const uploadEdge = topology.edges.find((edge) => edge.from === 'gateway' && edge.to === 'upload')!;
  const restoreEdge = topology.edges.find((edge) => edge.from === 'gateway' && edge.to.startsWith('chunkwise-restore-worker'))!;
  const restoreWorker = topology.nodes.find((node) => node.id === restoreEdge.to)!;

  assert.equal(client.config.rps, 8.75);
  assert.equal(uploadEdge.weight + restoreEdge.weight, 1);
  assert.equal(uploadEdge.weight, 0.8);
  assert.equal(restoreEdge.weight, 0.2);
  assert.equal(restoreWorker.config.serviceMs, 80);
  assert.equal(topology.edges.some((edge) => edge.from === restoreWorker.id && edge.to === 'index'), true);
  assert.equal(topology.edges.some((edge) => edge.from === restoreWorker.id && edge.to === 'store'), true);
  assert.equal(topology.nodes.filter((node) => node.id === 'index').length, 1);
  assert.equal(topology.nodes.filter((node) => node.id === 'gateway').length, 1);
});

test('CSV output preserves the documented Breakscale result schema and escapes notes', () => {
  assert.deepEqual(RUN_COLUMNS, [
    'run_id', 'experiment', 'design_file', 'seed', 'cpu_ms_per_mib',
    'write_share', 'offered_load', 'p50_ms', 'p95_ms', 'p99_ms', 'goodput',
    'failed', 'notes',
  ]);
  assert.equal(csvCell('{"index":2,"breaker":"closed"}'), '"{""index"":2,""breaker"":""closed""}"');
  const csv = serializeRows([{
    run_id: 'e3-1', experiment: 'E3', design_file: 'design.breakscale', seed: 1,
    cpu_ms_per_mib: 1, write_share: 1, offered_load: 1, p50_ms: 2, p95_ms: 3,
    p99_ms: 4, goodput: 5, failed: 0, notes: 'index, breaker',
  }]);
  const parsed = parseCsv(csv);
  assert.equal(parsed[0].length, RUN_COLUMNS.length);
  assert.equal(parsed[0][RUN_COLUMNS.indexOf('notes')], 'index, breaker');
});

test('transformed topologies execute against the upstream engine', {
  skip: !process.env.BREAKSCALE_ROOT,
}, async () => {
  const root = process.env.BREAKSCALE_ROOT!;
  const { Engine } = await import(join(root, 'src/sim/engine.ts'));
  const { defaultConfig } = await import(join(root, 'src/sim/presets.ts'));
  const e3 = buildE3Topology(baseline(), ids, 10, true, defaultConfig('breaker'));
  const e4 = buildE4Topology(baseline(), ids, 7, 0.25, 16, 5, defaultConfig('service'));

  for (const topology of [e3, e4]) {
    const engine = new Engine(topology, 1);
    for (let frame = 0; frame < 120; frame++) engine.advance(1000 / 60);
    const snapshot = engine.snapshot();
    assert.equal(Number.isFinite(snapshot.system.offeredRps), true);
    assert.equal(Number.isFinite(snapshot.system.goodputRps), true);
    assert.equal(Number.isFinite(snapshot.system.p99), true);
  }
});