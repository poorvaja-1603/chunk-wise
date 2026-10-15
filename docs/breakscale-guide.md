# Breakscale Guide: ChunkWise Capacity Experiments

Companion to `plan.md`. This guide covers only the Breakscale part of the project: how to model the ChunkWise service, which numbers come from measurement, which are assumptions, which experiments to run, and how to report them.

Read the rules in section 1 first. They decide what you are allowed to claim.

---

## 1. What Breakscale is used for, and what it is not used for

Breakscale is an open-source system design simulator (MIT licence, source at https://github.com/xevrion/breakscale, browser version at https://breakscale.vercel.app). You place components on a canvas, connect them, and raise the traffic load. The engine runs a discrete-event simulation of individual requests, so latency percentiles, queue depths, failures and retry effects come from the simulated requests, not from drawn curves. The simulator is deterministic: the same seed and topology replay identically.

For this project, Breakscale answers capacity questions:

- At what offered load does the upload path saturate, and how does tail latency (p95, p99) behave before that point?
- How much does the share of new bytes change the load on the chunk store, and therefore the capacity?
- What happens to goodput when a dependency slows down and clients retry?
- Does a circuit breaker stop the failure?

Breakscale does not answer storage questions. Dedup ratio and storage savings come only from the prototype (`results/results.json`). Breakscale takes one input from that data: how much of the uploaded data has to be written to the chunk store. Do not present a Breakscale result as a storage saving, and do not present a dedup ratio as a capacity result. This matches Section 13 of the feasibility report, which says Breakscale results are reported separately.

Breakscale is a teaching tool. Its component models are simplified, and it is not a validated model of ChunkWise. Report the results as relative behaviour under stated assumptions, not as predictions of production latency.

---

## 2. Setup

You need Bun (https://bun.sh). Breakscale's README uses it for development.

```bash
git clone https://github.com/xevrion/breakscale.git
cd breakscale
bun install
bun dev
```

Open http://localhost:5173. If Vite reports the port is in use, it will pick another port and print the URL. You can also use the hosted site at https://breakscale.vercel.app for the canvas work, but the scripted runs in section 6 need the local clone.

Record the commit you used for the report:

```bash
git rev-parse HEAD
```

---

## 3. Calibration: which numbers are measured and which are assumed

Every input to the Breakscale model has one of three sources. Label each one in the topology and in the report. Use the table below as the source of truth.

| Input | Value to use | Source | Label in report |
|---|---|---|---|
| Upload CPU service time | 675.7 ms per MiB (`cpu_service_ms_per_mb_put`) | Measured, Python prototype | Measured |
| Restore CPU service time | 0.005 s per MiB (from `results.csv`, `get_s_per_mb`) | Measured, Python prototype | Measured |
| Share of bytes written to chunk store, first backup | 1.0 | Definition: nothing is deduplicated yet | Scenario |
| Share of bytes written to chunk store, edited version | 0.0014 (`store_write_fraction_edited_text`) | Measured, D3 second version | Measured |
| Share of bytes written to chunk store, partial change | 0.5 | Scenario, not measured | Assumption |
| Gateway service time | 1 ms | Assumption | Assumption |
| Index lookup and commit service time | 2 ms per request, batched | Assumption | Assumption |
| Object store write service time | 5 ms base plus 1 ms per MiB | Assumption | Assumption |
| Concurrency per worker | 4 | Assumption | Assumption |
| Service-time variability (coefficient of variation) | 0.5 | Assumption | Assumption |

Two points need explanation in the report.

First, the measured upload CPU cost is 675.7 ms per MiB. That is pure Python, which is slow, so the value is pessimistic for any production implementation. Do not hide this. Run the experiments at three CPU costs: the measured value (675.7 ms/MiB), one tenth of it (67.6 ms/MiB), and one hundredth of it (6.8 ms/MiB). The question is at which CPU cost the bottleneck moves from the worker to the index or the chunk store. That point is the result, not the absolute numbers.

Second, the restore cost measured in the prototype is small compared with the put cost, because restore only reads, hashes, and concatenates. In the topology, restore latency is therefore dominated by the assumed index and object-store service times. Say so in the report.

Measured dedup on the edited-text workload is 1.9974. The first version is fully written, and the second version writes only 0.0014 of its bytes to the chunk store, which is why the edit scenario has a much lower write share. The duplicate-upload scenario (D2) has a dedup ratio of 4.0, which is the same as writing 25% of the bytes. Use the share values above as the load mix in experiment E2.

---

## 4. Mapping ChunkWise onto the Breakscale palette

Breakscale has about 33 components. Names in the palette may differ slightly from the names below. Pick the closest kind, and write the mapping in the report as a table.

| ChunkWise element | Closest Breakscale kind | Service time source | Notes |
|---|---|---|---|
| API gateway (auth stub, request validation) | Load balancer | Assumption, 1 ms | Front door for both upload and restore |
| Upload worker (chunking, SHA-256, manifest) | Worker or service, capacity = concurrency per worker | Measured: `cpu_service_ms_per_mb_put` times the request size in MiB | The main CPU cost |
| Index (SQLite in the prototype, KV store in the design) | Database | Assumption, 2 ms per request | Sweep this; it is the usual bottleneck in the design |
| Chunk store (object storage in the design) | Database or storage-like component | Assumption, 5 ms plus 1 ms per MiB, times the write share | Load falls as the write share falls |
| Restore worker | Service | Assumption for fetches, measured small CPU cost | Reads manifest, fetches chunks |
| GC and recovery worker | Worker, background | Not modelled in the first topology | Optional in experiment E4 |

Two model choices to state in the report:

- A request is one file. A 16 MiB upload is one request with a size, not 2048 separate requests. The upload worker's service time scales with the request size.
- Index operations are batched per request. In the design, a file produces one lookup per chunk. Batching is an assumption that makes the first model tractable. Experiment E3 tests what happens without batching by raising the index service time.

---

## 5. Building the topology

Work in the browser or the local app. Build one topology per experiment, and save each one as a design file (the app's save and export actions write a `.breakscale` file). Name the files after the experiment, for example `breakscale-designs/e1-upload-baseline.breakscale`.

Build order:

1. Place the gateway, the upload worker, the index, and the chunk store. Wire them: gateway to upload worker, upload worker to index, upload worker to chunk store.
2. Set each component's parameters from section 3. Record every value you set in the table at the end of this guide.
3. Add a restore path: gateway to restore worker, restore worker to index (manifest read), restore worker to chunk store (chunk fetches). Restore traffic is set separately from upload traffic.
4. Check that each component's label matches its role. Use the component's built-in explanation text, which the app provides for every metric and unit.

A design file holds the topology in a JSON object with `app`, `version`, `savedAt`, `name`, and `topology`. To see the exact node and edge fields, export a preset (for example, Single Server) and open the file in an editor. Use the same field names when you edit a file by script. Do not guess the field names.

---

## 6. Experiments

Run each experiment with three seeds (1, 2, 3) and report the spread. Breakscale is deterministic for a given seed, so the seeds show sensitivity to the random sampling, not run-to-run noise.

### E1: Upload capacity baseline (Figure 5)

- Hypothesis: the upload path saturates at a load set by the CPU cost. Tail latency rises sharply before saturation.
- Topology: `e1-upload-baseline`. Upload path only. Measured CPU cost. Write share 1.0 (first backup).
- Procedure: raise the upload load in steps. At each step, run 10 simulated seconds and record the metrics. Stop when goodput flattens or failures appear.
- Record per step: offered load, p50, p95, p99, goodput, failed requests, worker utilisation.
- Expected pattern: the same shape as the Single Server example in Breakscale's README, where latency climbs as the database fills up. If your curve does not show this, check the concurrency and the service-time values before you accept the result.
- Report: the load at the knee (the point where p99 rises sharply), and the bottleneck component at that point.

### E2: Effect of the share of new bytes on capacity (Figure 6)

- Hypothesis: lowering the share of bytes written to the chunk store raises the capacity, because the chunk store becomes less of a bottleneck. The gain is largest when the chunk store is the bottleneck.
- Topology: `e1-upload-baseline` copied three times, with the write share set to 0.0014 (edited version, measured), 0.5 (partial change, assumption), and 1.0 (first backup).
- Procedure: run the E1 load sweep for each copy. Plot the knee load against the write share.
- Also run the sweep at CPU costs 675.7, 67.6 and 6.8 ms/MiB. The figure should show where the knee moves.
- Report: this is the link between the dedup result and capacity. Say explicitly that the write share for the edited version is measured from the prototype, and that the capacity numbers are Breakscale's model output.

### E3: Index slowdown, retries, and the retry storm (Figure 7)

- Hypothesis: when the index slows down, client retries add load to the already slow index, and goodput falls even though the offered load did not change. A circuit breaker on the upload worker's index call stops the collapse.
- Topology: `e3-retry-storm` (the E1 topology plus a retry policy on the gateway-to-worker call), then `e3-breaker` (same, with a circuit breaker on the worker-to-index call).
- Procedure: hold the load at about 80% of the E1 knee. Raise the index service time in steps (2 ms, 10 ms, 50 ms). At each step, record goodput and the index utilisation. Repeat with the breaker.
- Expected pattern: the Retry Storm example in Breakscale's README shows goodput falling to zero while the database stays busy. Check that your retry storm shows the same pattern before you use it.
- Report: a ChunkWise failure mode, linked to the feasibility report's "index outage" risk and its mitigation (replication and cache). The breaker is a model of one mitigation, not a full design.

### E4: Restore spike (Figure 8)

- Hypothesis: a burst of restore traffic raises upload tail latency if both share the same index and gateway.
- Topology: `e1-upload-baseline` with the restore path added.
- Procedure: hold upload load at 70% of the E1 knee. Add restore load in steps (10%, 25%, 50% of upload load). Record upload p99 and restore p99 at each step.
- Report: whether restore and upload should be isolated, and what the numbers say about isolation.

### E5 (optional): Scaling lag

- Use the Autoscaling Service example from the README as the template. Only do this if E1 to E4 are complete.

---

## 7. Scripted runs (optional, for three seeds and sweeps)

The Breakscale engine has no UI dependency. The README shows this usage:

```ts
import { Engine } from './src/sim/engine';
import { PRESETS } from './src/sim/presets';

const engine = new Engine(PRESETS[0].topology, 42);
for (let i = 0; i < 600; i += 1) engine.advance(1000 / 60);
console.log(engine.snapshot().system);
```

Here 600 steps of 1000/60 ms is 10 simulated seconds. To run a saved design instead of a preset, read its `topology` field. A script for the E1 sweep looks like this. Place it at the root of your Breakscale clone and run it with `bun run sweep.ts`:

```ts
import { readFileSync } from 'node:fs';
import { Engine } from './src/sim/engine';

const file = JSON.parse(readFileSync('breakscale-designs/e1-upload-baseline.breakscale', 'utf8'));
const topology = file.topology;

for (const seed of [1, 2, 3]) {
  const engine = new Engine(topology, seed);
  for (let i = 0; i < 600; i += 1) engine.advance(1000 / 60);
  console.log(JSON.stringify({ seed, system: engine.snapshot().system }));
}
```

Before you write the sweep:

1. Print `engine.snapshot().system` once and list its field names. Build your results table from those names, not from memory.
2. Find the offered-load field in the topology JSON. The UI slider sets it. Change that field in a loop to run the sweep. If you cannot find it, set the load in the UI for each step and copy the numbers by hand.
3. If TypeScript complains about the JSON's type, cast it (`file.topology as any`). Do not change the engine code.

---

## 8. Recording results

Every run goes in one table. Keep it in `breakscale-designs/runs.csv`, and do not edit it by hand after the run.

| run_id | experiment | design_file | seed | cpu_ms_per_mib | write_share | offered_load | p50_ms | p95_ms | p99_ms | goodput | failed | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

Also record the Breakscale commit (`git rev-parse HEAD`) and the date at the top of the table.

---

## 9. Reading the results

- The knee is the offered load where p99 rises sharply and goodput stops growing. Report it as a range across the three seeds.
- Compare runs by their relative position: which curve saturates first, and by how much. Do not compare absolute milliseconds with real systems.
- Name the bottleneck at each knee. A result without a named bottleneck is not finished.
- If a result contradicts the README's teaching example for the same pattern, do not report it as a finding until you have checked the topology and the parameters.
- Do not claim that Breakscale predicts ChunkWise's latency in production. Claim that the model shows which component saturates first under the stated assumptions.

---

## 10. Writing it up

Map the results into the feasibility report as follows:

- Figure 5: E1 throughput and p99 against offered load, with the knee marked.
- Figure 6: E2 knee load against write share, one line per CPU cost.
- Figure 7: E3 goodput against index service time, with and without the breaker.
- Figure 8: E4 upload and restore p99 against restore load.
- Table 7: the assumptions table from section 3, with the label column.
- Section 11 of the report (acceptance criteria and evaluation plan): state the Breakscale version (commit hash) and cite the repository (MIT, https://github.com/xevrion/breakscale).

Figure 6 is the one that connects the project's two halves. The write share for the edited version is measured by the prototype. The capacity effect is Breakscale's output under stated assumptions. Keep these two statements separate in the text.

---

## 11. Team split

- Owner A (storage and chunking) owns section 3 (calibration), reruns the benchmark if the parameters change, and keeps the assumptions table current.
- Owner B (index, engine, CLI) owns sections 4 and 5 (topology and design files) and runs the sweeps in section 6.
- Both run E3 and check each other's topology against section 4.
- Both review the figures and the section 9 wording before the report is submitted.

---

## 12. Troubleshooting

- `bun: command not found`: install Bun from https://bun.sh, then open a new terminal.
- `bun install` fails: check the Bun version against the repository's `package.json`, and delete `node_modules` before retrying.
- The app loads but the canvas is empty: pick a preset from the left panel first, then edit it.
- A sweep script fails on the JSON structure: export a preset and compare the field names with your script.
- Results differ between two runs with the same seed: check that the topology file and the parameters are the same, and that you did not change the engine code. Breakscale is deterministic for a fixed seed and topology.

---

## 13. Parameter log (fill in during the runs)

| Component | Parameter | Value used | Label (Measured / Assumption / Scenario) |
|---|---|---|---|
| Gateway | service time | 1 ms | Assumption |
| Upload worker | service time per MiB | 675.7 ms | Measured |
| Upload worker | concurrency | 4 | Assumption |
| Index | service time per request | 2 ms | Assumption |
| Chunk store | base service time | 5 ms | Assumption |
| Chunk store | per MiB | 1 ms | Assumption |
| All stages | coefficient of variation | 0.5 | Assumption |
| Upload mix | write share | 0.0014 / 0.5 / 1.0 | Measured / Assumption / Scenario |
