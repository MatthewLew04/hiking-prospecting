# Authoritative zero-inventory evidence

`pipelines/build_zero_inventory_evidence.py` publishes the finding needed when
a reviewed national baseline truthfully contains no features for a state. The
current contract supports the national `faults` baseline; Delaware, Florida,
Maryland, North Dakota, and Nebraska are zero in the current reviewed snapshot.

The compiler reads the strict current manifest, requires the exact 49-state
count matrix and `states.<XX> == 0`, and rehashes the declared national PMTiles
artifact. It writes only canonical, content-addressed JSON below
`site/map-assets/releases/zero-inventory/`. It never creates geometry or edits
the manifest, registry, or release flags.

```sh
python3 pipelines/build_zero_inventory_evidence.py --state DE --layer faults
```

Copy the emitted object—including `evidence_artifact`, `sha256`, and exact
`bytes`—into the released state's `faults.zero_inventory` only
after review. Its state faults artifact must be a byte-identical immutable copy
of the checksummed national faults PMTiles, with `source_layers: ["faults"]`
and complete `layer_metadata.faults.n: 0`. The state descriptor therefore
advertises `n: 0` and an exact state filter. The release validator checks the
evidence filename/hash/bytes, current baseline path/bytes/hash, current state count,
and absence of any invented state feature. Missing evidence remains unknown;
it is not converted to zero.
