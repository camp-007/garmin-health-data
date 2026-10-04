# Pool workout definitions (schema 2)

Pool swimming uses `schema_version: 2` and `sport: swimming`. Schema-1 running and
cycling definitions remain unchanged. Existing validate, render and preview commands
accept the new version without provider calls. Publishing still requires the user's
approval of the exact workout/date and the existing account binding.

```json
{
  "schema_version": 2,
  "workout": {
    "key": "pool-example-v1", "family": "pool-example", "version": 1,
    "name": "Pool example", "sport": "swimming",
    "pool": {"length": 25, "unit": "yards"},
    "description": "Easy technique practice.",
    "steps": [{"type": "repeat", "count": 3, "steps": [
      {"type": "interval", "duration": {"type": "distance", "value": 100, "unit": "yards"},
       "swim": {"stroke": "freestyle"}, "notes": "Long strokes."},
      {"type": "rest", "duration": {"type": "time", "seconds": 20}}
    ]}]
  }
}
```

Pool/distance units are `yards` or `meters`; distances must be whole pool lengths.
Garmin receives the original distance value with its unit descriptor. Summary meters
are calculated separately. Fixed rest (`time`) follows the swim. A `send_off` duration
in seconds means the total repetition interval, and is only valid on a rest immediately
following a distance swim inside a repeat. Repeats include their final rest. Send-off
cycles are reported separately and are not added to fixed-rest/timed totals.

Other durations: `lap_button`. Step types: warmup, interval, recovery, cooldown, rest,
other and repeat. Limits: four repeat levels, 100 structural/expanded steps. Strokes:
freestyle, backstroke, breaststroke, butterfly, choice, mixed, individual_medley.
Optional drills: kick, pull, drill. Optional equipment: fins, kickboard, paddles,
pull_buoy. Rest steps cannot have swimming attributes. Overall description has a
512-character limit; step notes have 200. Step names are local labels; notes are
watch-facing instructions.

Only open targets are supported. Keep effort, breathing and pace instructions in
notes. Native pace/CSS alerts, CSS rest offsets, round-dependent medleys, snorkel and
open water are deferred; unknown structured fields are rejected. Rendering has
synthetic tests and read-only provider examples; live execution of generated
workouts on a watch is not yet verified. No personal templates are included here.

Training Compass owns hosted approval, consent and deep provider readback before
scheduling; this package owns provider rendering. Consumers must support schema 2
before new records are written, and must retain a schema-2-compatible rollback.
This release makes no extractor database schema changes.
