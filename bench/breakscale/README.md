# ChunkWise Breakscale E3/E4 Runner

The runner extends an exported Owner A E1 `.breakscale` topology. It does not
create an E1 baseline or infer its knee. E3/E4 require the E1 design, Owner A's
generated `results/breakscale_params.json`, the E1 sweep in
`breakscale-designs/runs.csv`, an explicitly selected E1 knee load present for
seeds 1, 2, and 3, and the E1 request size in MiB.

Breakscale is MIT-licensed: <https://github.com/xevrion/breakscale>. The run
metadata records the exact source commit. The simulator repository is not
vendored into ChunkWise.

## Topology mapping

| ChunkWise component | Breakscale component          | Path and reported measures                                                                                         |
| ------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Client              | `client`                      | Offers the E1 or combined upload/restore request rate.                                                             |
| API gateway         | `lb`                          | Shared ingress. E3 retries are configured here; E4 weighted routing splits upload and restore request classes.     |
| Upload worker       | E1 `worker` or `service` node | Calls both index and chunk store; E3 p99 is measured at the system/client.                                         |
| Index               | E1 `db` node                  | E3 service time is swept at 2, 10, and 50 ms; queue, utilization, and arrivals are recorded.                       |
| Chunk store         | E1 `db` or `objectstore` node | Shared by uploads/restores; its service configuration is retained from E1.                                         |
| Index breaker       | `breaker`                     | Inserted only between upload worker and index in E3-breaker; threshold 0.5 over 4 s, open 3 s, 3 half-open probes. |
| Restore worker      | `service`                     | Added for E4; four slots, CV 0.5, CPU service time from measured get cost times the supplied E1 request size.      |

E4 keeps one gateway, index, and chunk store. Because Breakscale's load balancer
selects one outgoing edge using edge weights, the runner supplies one combined
client stream at `upload_rps * (1 + restore_share)`, then splits it with gateway
weights `1/(1+restore_share)` and `restore_share/(1+restore_share)`. Worker
metrics identify the two paths. This is a simulator routing assumption, not a
claim that a production gateway can identify request classes without metadata.

The specified E1 knee, request size, and measured calibration are mandatory
inputs. The runner exits with a diagnostic and produces no results if they are
missing. It appends generated E3/E4 rows to the documented CSV schema and
refuses duplicate run IDs or schema mismatches, preserving pre-existing E1/E2
rows. Designs are saved as native `.breakscale` files.

## Run

Clone Breakscale separately and use a TypeScript runner such as `tsx` (the
simulator engine itself is imported from that checkout):

```sh
npx --yes tsx bench/breakscale/run.ts \
  --breakscale-root /path/to/breakscale \
  --design breakscale-designs/e1-upload-baseline.breakscale \
  --params results/breakscale_params.json \
  --e1-runs breakscale-designs/runs.csv \
  --e1-knee-rps OWNER_A_KNEE_RPS \
  --request-size-mib OWNER_A_REQUEST_SIZE_MIB \
  --client-id CLIENT_ID \
  --gateway-id GATEWAY_ID \
  --upload-worker-id UPLOAD_WORKER_ID \
  --index-id INDEX_ID \
  --chunk-store-id CHUNK_STORE_ID
```

Each E3 topology is run with/without the breaker at all three index latencies;
E4 is run at 10%, 25%, and 50% restore-to-upload load. Each case uses seeds 1,
2, and 3 for 10 simulated seconds. Result metrics are simulator output only;
they are not storage-savings measurements. `npx --yes tsx --test
bench/breakscale/run.test.ts` runs the topology transformation and CSV schema
tests without requiring measured E1 inputs.

## Report artifacts

`bench/breakscale/report_artifacts.py` generates Figures 5–8 and
`results/table7_assumptions.csv` only after validating complete seed coverage.
Figures 5 and 6 require Owner A's E1/E2 result rows and selected knee values.
Supply `breakscale-designs/e2-knees.csv` with exactly these columns:

```csv
cpu_ms_per_mib,write_share,seed,knee_load,source_run_id
```

Each knee selection must point to the corresponding executed E2 row's run ID,
seed, CPU cost, write share, and offered load. This is the explicit analysis
judgment required by the guide's definition of a knee; the generator does not
infer one with an undocumented threshold.

After E1/E2/E3/E4 rows and the E2 knee selections exist, run:

```sh
python bench/breakscale/report_artifacts.py --e1-knee-rps OWNER_A_KNEE_RPS
```

The tool writes chart/table artifacts under `results/`; it never synthesizes
placeholder measurements. No restore read-latency measurement exists in the
guide, so E4 retains the E1 chunk-store service time and labels it as an
assumption in the run metadata.
